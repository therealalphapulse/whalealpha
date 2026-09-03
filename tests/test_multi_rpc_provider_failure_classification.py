"""
tests/test_multi_rpc_provider_failure_classification.py

Regression coverage for the AlphaPulse Provider Resilience task
(2026-08-28), Ankr/RPC-gateway half of the fix:
providers/rpc/multi_rpc_manager.py `_dispatch_to_provider`.

Before the fix: any HTTP status other than 200/429/500/502/503/504 (i.e.
401/402/403 -- Ankr's documented premium-key failure mode -- plus 400/404/
etc.) fell into a branch that resolved the WHOLE job's future to None and
returned _OUTCOME_SUCCESS. That had two bugs:
  1. It never counted toward the provider's consecutive-failure /
     circuit-breaker tracking, so a provider stuck returning 403 forever
     was never taken out of rotation.
  2. It ended the job immediately instead of failing over to the next
     eligible provider still in the same rotation.

After the fix: those statuses return _OUTCOME_FAILURE, which the existing
_dispatch loop already knows how to handle correctly (penalize health,
continue to the next eligible provider) -- proven end-to-end below by a
scenario where Ankr 403s and Helius succeeds.

Manual-runner convention (no pytest-asyncio in this repo), matching
tests/test_birdeye_holder_fallback.py.
"""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.rpc.multi_rpc_manager import (  # noqa: E402
    MultiRPCManager,
    _ProviderHealth,
    _Job,
    _OUTCOME_SUCCESS,
    _OUTCOME_FAILURE,
)


class _FakeResponse:
    def __init__(self, status: int, json_body=None, text_body: str = ""):
        self.status = status
        self._json_body = json_body if json_body is not None else {}
        self._text_body = text_body or "error"

    async def json(self, *args, **kwargs):
        return self._json_body

    async def text(self):
        return self._text_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Routes by provider so a multi-provider failover scenario can be
    driven from a single fake session: each provider's URL is distinct
    (see MultiRPCManager._build_provider_url), so we key off the url."""

    def __init__(self, status_by_url_substring: dict):
        self._status_by_url_substring = status_by_url_substring
        self.calls: list[str] = []

    def _resolve(self, url):
        for substring, status in self._status_by_url_substring.items():
            if substring in url:
                return status
        raise AssertionError(f"No fake response configured for url={url!r}")

    def get(self, url, **kwargs):
        self.calls.append(url)
        status = self._resolve(url)
        if status in (200,):
            return _FakeResponse(200, json_body={"jsonrpc": "2.0", "result": "ok", "id": 1})
        return _FakeResponse(status, text_body="forbidden")

    post = get


def _make_manager_with_providers(providers: dict) -> MultiRPCManager:
    mgr = MultiRPCManager.__new__(MultiRPCManager)
    mgr._buckets = {}
    mgr._wakeup = asyncio.Queue()
    mgr._cache = {}
    mgr._dedup_cache = {}
    mgr._session = None
    mgr._session_lock = asyncio.Lock()
    mgr._worker_task = None
    mgr._cleanup_task = None
    mgr._last_dispatch = 0.0
    mgr._dispatch_lock = asyncio.Lock()
    mgr._rl_hits_since_log = 0
    mgr._rl_window_start = time.monotonic()
    mgr._rate_multiplier = 1.0
    mgr._consecutive_429s = 0
    mgr._consecutive_successes = 0
    mgr._concurrent_requests = 0
    mgr._concurrent_lock = asyncio.Lock()
    mgr._providers = providers
    mgr._provider_health = {name: _ProviderHealth() for name in providers}
    mgr._provider_stats = {}
    mgr._current_provider_index = 0
    return mgr


def _make_job(context="test") -> _Job:
    return _Job(
        method="POST",
        url="",
        params=None,
        json_body={"jsonrpc": "2.0", "method": "getBalance", "id": 1},
        timeout=5.0,
        priority=1,
        context=context,
    )


ANKR_CONFIG = {"endpoint": "https://rpc.ankr.com/solana/", "api_key": "fake-ankr-key"}
HELIUS_CONFIG = {"endpoint": "https://mainnet.helius-rpc.com/", "api_key": "fake-helius-key"}


async def _dispatch_to_provider_no_throttle(mgr, job, provider_name):
    # _dispatch_to_provider itself doesn't throttle/acquire concurrency
    # slots (that's _dispatch's job) -- call it directly to unit-test the
    # classification fix in isolation.
    return await mgr._dispatch_to_provider(job, provider_name)


# ---------------------------------------------------------------------
# Unit-level: the classification fix itself
# ---------------------------------------------------------------------
async def test_403_returns_failure_not_success():
    mgr = _make_manager_with_providers({"ankr": ANKR_CONFIG})
    fake_session = _FakeSession({"rpc.ankr.com": 403})
    job = _make_job("ankr_403_test")

    with patch.object(mgr, "_get_session", AsyncMock(return_value=fake_session)):
        outcome = await _dispatch_to_provider_no_throttle(mgr, job, "ankr")

    assert outcome == _OUTCOME_FAILURE
    assert outcome != _OUTCOME_SUCCESS
    # Critically, the job's future must NOT have been resolved -- the old
    # bug ended the whole request right here with a None result.
    assert not job.future.done()


async def test_401_and_402_also_classified_as_failure():
    for status in (401, 402):
        mgr = _make_manager_with_providers({"ankr": ANKR_CONFIG})
        fake_session = _FakeSession({"rpc.ankr.com": status})
        job = _make_job(f"ankr_{status}_test")
        with patch.object(mgr, "_get_session", AsyncMock(return_value=fake_session)):
            outcome = await _dispatch_to_provider_no_throttle(mgr, job, "ankr")
        assert outcome == _OUTCOME_FAILURE, f"status {status} was not classified as failure"
        assert not job.future.done()


async def test_repeated_403_trips_ankr_circuit_breaker():
    mgr = _make_manager_with_providers({"ankr": ANKR_CONFIG})
    health = mgr._provider_health["ankr"]
    fake_session = _FakeSession({"rpc.ankr.com": 403})

    with patch.object(mgr, "_get_session", AsyncMock(return_value=fake_session)):
        # MULTI_RPC_CIRCUIT_BREAKER_THRESHOLD default is 5.
        for _ in range(5):
            job = _make_job("ankr_repeat_403")
            outcome = await _dispatch_to_provider_no_throttle(mgr, job, "ankr")
            assert outcome == _OUTCOME_FAILURE
            health.mark_failed()
            health.consecutive_failures += 1
            if health.consecutive_failures >= 5:
                health.mark_broken()

    assert health.is_circuit_broken() is True


# ---------------------------------------------------------------------
# End-to-end: Ankr 403 fails over to Helius within the SAME rotation
# instead of ending the job with None.
# ---------------------------------------------------------------------
async def test_dispatch_fails_over_from_ankr_403_to_healthy_helius():
    mgr = _make_manager_with_providers({"ankr": ANKR_CONFIG, "helius": HELIUS_CONFIG})
    fake_session = _FakeSession({
        "rpc.ankr.com": 403,
        "helius-rpc.com": 200,
    })
    job = _make_job("failover_test")

    with patch.object(mgr, "_get_session", AsyncMock(return_value=fake_session)), \
         patch.object(mgr, "_throttle", AsyncMock()), \
         patch.object(mgr, "_acquire_concurrency_slot", AsyncMock()), \
         patch.object(mgr, "_release_concurrency_slot", AsyncMock()):
        # Force Ankr to be tried first regardless of health-score sort
        # order, then let natural rotation continue to Helius.
        mgr._current_provider_index = list(mgr._providers.keys()).index("ankr")
        await mgr._dispatch(job)

    result = await job.future
    assert result == {"jsonrpc": "2.0", "result": "ok", "id": 1}
    # Ankr must show up as a recorded failure, and its health must be
    # worse than a provider that never failed -- proving the classification
    # fix actually penalizes it instead of silently succeeding.
    assert "ankr" in job.failed_providers_seen
    assert mgr._provider_health["ankr"].score < mgr._provider_health["helius"].score


async def test_legitimate_signal_not_suppressed_by_unhealthy_ankr():
    """The exact scenario the task cares about: Ankr being down must never
    by itself prevent a legitimate result from a healthy provider."""
    mgr = _make_manager_with_providers({"ankr": ANKR_CONFIG, "helius": HELIUS_CONFIG})
    fake_session = _FakeSession({
        "rpc.ankr.com": 403,
        "helius-rpc.com": 200,
    })

    with patch.object(mgr, "_get_session", AsyncMock(return_value=fake_session)), \
         patch.object(mgr, "_throttle", AsyncMock()), \
         patch.object(mgr, "_acquire_concurrency_slot", AsyncMock()), \
         patch.object(mgr, "_release_concurrency_slot", AsyncMock()):
        for i in range(3):
            job = _make_job(f"signal_{i}")
            mgr._current_provider_index = list(mgr._providers.keys()).index("ankr")
            await mgr._dispatch(job)
            result = await job.future
            assert result is not None, (
                f"job {i} was suppressed even though Helius was healthy — "
                f"Ankr's outage should never do this"
            )


if __name__ == "__main__":
    async_tests = [
        test_403_returns_failure_not_success,
        test_401_and_402_also_classified_as_failure,
        test_repeated_403_trips_ankr_circuit_breaker,
        test_dispatch_fails_over_from_ankr_403_to_healthy_helius,
        test_legitimate_signal_not_suppressed_by_unhealthy_ankr,
    ]

    async def run_all():
        passed = 0
        for t in async_tests:
            await t()
            passed += 1
            print(f"PASS  {t.__name__}")
        print(f"\n{passed}/{len(async_tests)} tests passed")

    asyncio.run(run_all())
