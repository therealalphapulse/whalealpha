"""
tests/test_solana_tracker_resilience.py

Regression coverage for the AlphaPulse Provider Resilience task
(2026-08-28): providers/marketdata/_resilience.py's opt-in circuit-breaker
integration, and providers/marketdata/solanatracker.py's two Solana Tracker
lookups (get_pool_liquidity_usd, get_bundle_risk_pct) actually using it.

Follows the manual-runner convention from tests/test_birdeye_holder_fallback.py
(this repo has no pytest-asyncio dependency): sync tests are plain
pytest-discoverable functions; async tests are collected and run via
asyncio.run() under `if __name__ == "__main__":`, and can also be run
individually with any asyncio-aware runner.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.marketdata import _provider_circuit_breaker as breaker  # noqa: E402
from providers.marketdata import _resilience  # noqa: E402
from providers.marketdata import solanatracker  # noqa: E402


def setup_function(_fn):
    breaker.reset("solana_tracker")
    os.environ["SOLANA_TRACKER_API_KEY"] = "test-key"


class _FakeResponse:
    def __init__(self, status: int, json_body=None):
        self.status = status
        self._json_body = json_body if json_body is not None else {}

    async def json(self, *args, **kwargs):
        return self._json_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Queue of responses returned in order, one per session.get() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    def get(self, *args, **kwargs):
        self.call_count += 1
        if not self._responses:
            raise AssertionError("FakeSession.get() called more times than expected")
        return self._responses.pop(0)


class _RaisingSession:
    """A session whose .get() must never be called -- used to prove the
    circuit breaker skipped the network entirely."""

    def get(self, *args, **kwargs):
        raise AssertionError("Network call made while circuit breaker was open")


# ---------------------------------------------------------------------
# get_json() breaker wiring
# ---------------------------------------------------------------------
async def test_get_json_without_provider_name_is_unaffected_by_breaker():
    """Every OTHER market-data caller (coingecko, dexscreener, geckoterminal,
    goplus, rugcheck) omits provider_name -- confirm the breaker is never
    consulted for them, even if some unrelated provider key is broken."""
    breaker.record_failure("unrelated-provider", breaker.FAILURE_AUTH_OR_CREDITS)
    fake_session = _FakeSession([_FakeResponse(200, {"ok": True})])
    with patch.object(_resilience, "_get_session", AsyncMock(return_value=fake_session)), \
         patch.object(_resilience, "get_cache", AsyncMock(return_value=_NullCache())):
        result = await _resilience.get_json("https://example.test/x")
    assert result == {"ok": True}
    assert fake_session.call_count == 1


class _NullCache:
    async def get(self, key):
        return None

    async def set(self, key, value, ttl):
        return None


async def test_get_json_403_recorded_as_auth_failure_and_trips_breaker():
    fake_session = _FakeSession([_FakeResponse(403, {"error": "insufficient credits"})])
    with patch.object(_resilience, "_get_session", AsyncMock(return_value=fake_session)), \
         patch.object(_resilience, "get_cache", AsyncMock(return_value=_NullCache())):
        result = await _resilience.get_json(
            "https://example.test/x", provider_name="solana_tracker"
        )
    assert result is None
    assert breaker.is_open("solana_tracker") is True  # auth threshold is 1


async def test_get_json_skips_network_when_breaker_open():
    breaker.record_failure("solana_tracker", breaker.FAILURE_AUTH_OR_CREDITS)
    assert breaker.is_open("solana_tracker") is True

    raising_session = _RaisingSession()
    with patch.object(_resilience, "_get_session", AsyncMock(return_value=raising_session)), \
         patch.object(_resilience, "get_cache", AsyncMock(return_value=_NullCache())):
        result = await _resilience.get_json(
            "https://example.test/x", provider_name="solana_tracker"
        )
    assert result is None  # returned immediately, no AssertionError raised above


async def test_get_json_recovers_after_cooldown_and_successful_probe():
    import time

    breaker.record_failure("solana_tracker", breaker.FAILURE_AUTH_OR_CREDITS)
    state = breaker._get("solana_tracker")
    state.opened_at = time.monotonic() - 999999  # force cooldown elapsed

    fake_session = _FakeSession([_FakeResponse(200, {"data": [{"liquidityUsd": 1234}]})])
    with patch.object(_resilience, "_get_session", AsyncMock(return_value=fake_session)), \
         patch.object(_resilience, "get_cache", AsyncMock(return_value=_NullCache())):
        result = await _resilience.get_json(
            "https://example.test/x", provider_name="solana_tracker"
        )
    assert result == {"data": [{"liquidityUsd": 1234}]}
    assert breaker.is_open("solana_tracker") is False
    assert fake_session.call_count == 1  # the probe was allowed through


async def test_get_json_transient_5xx_uses_transient_threshold_not_auth():
    # Three consecutive 5xx responses (max_retries=0 so no in-call retry
    # masks this) should take three separate get_json() calls to trip,
    # not one -- proving 5xx uses FAILURE_TRANSIENT (threshold 3), not the
    # fast auth/credits threshold (1).
    with patch.object(_resilience, "get_cache", AsyncMock(return_value=_NullCache())):
        for i in range(2):
            fake_session = _FakeSession([_FakeResponse(503)])
            with patch.object(_resilience, "_get_session", AsyncMock(return_value=fake_session)):
                await _resilience.get_json(
                    "https://example.test/x", provider_name="solana_tracker", max_retries=0
                )
            assert breaker.is_open("solana_tracker") is False, f"tripped too early on call {i}"

        fake_session = _FakeSession([_FakeResponse(503)])
        with patch.object(_resilience, "_get_session", AsyncMock(return_value=fake_session)):
            await _resilience.get_json(
                "https://example.test/x", provider_name="solana_tracker", max_retries=0
            )
        assert breaker.is_open("solana_tracker") is True


# ---------------------------------------------------------------------
# solanatracker.py actually wires provider_name="solana_tracker" through
# ---------------------------------------------------------------------
async def test_get_pool_liquidity_usd_stops_calling_when_breaker_open():
    breaker.record_failure("solana_tracker", breaker.FAILURE_AUTH_OR_CREDITS)

    with patch.object(solanatracker, "get_json", AsyncMock(return_value={"data": []})) as mock_get_json:
        # get_json itself is mocked here (unit-testing the *wiring*, not
        # _resilience's internals again) -- but because provider_name is
        # forwarded, a real get_json would have short-circuited. Confirm
        # the kwarg is actually passed so that short-circuit is real in
        # production.
        await solanatracker.get_pool_liquidity_usd("MintAddr111111111111111111111111111111111")
        _, kwargs = mock_get_json.call_args
        assert kwargs.get("provider_name") == "solana_tracker"


async def test_get_bundle_risk_pct_passes_provider_name():
    with patch.object(solanatracker, "get_json", AsyncMock(return_value=None)) as mock_get_json:
        await solanatracker.get_bundle_risk_pct("MintAddr111111111111111111111111111111111")
        _, kwargs = mock_get_json.call_args
        assert kwargs.get("provider_name") == "solana_tracker"


async def test_get_pool_liquidity_usd_not_configured_returns_none_without_breaker_interaction():
    os.environ.pop("SOLANA_TRACKER_API_KEY", None)
    with patch.object(solanatracker, "get_json", AsyncMock()) as mock_get_json:
        result = await solanatracker.get_pool_liquidity_usd("Mint")
    assert result is None
    mock_get_json.assert_not_called()  # unconfigured is a config state, not a health state
    os.environ["SOLANA_TRACKER_API_KEY"] = "test-key"


if __name__ == "__main__":
    async_tests = [
        test_get_json_without_provider_name_is_unaffected_by_breaker,
        test_get_json_403_recorded_as_auth_failure_and_trips_breaker,
        test_get_json_skips_network_when_breaker_open,
        test_get_json_recovers_after_cooldown_and_successful_probe,
        test_get_json_transient_5xx_uses_transient_threshold_not_auth,
        test_get_pool_liquidity_usd_stops_calling_when_breaker_open,
        test_get_bundle_risk_pct_passes_provider_name,
        test_get_pool_liquidity_usd_not_configured_returns_none_without_breaker_interaction,
    ]

    async def run_all():
        passed = 0
        for t in async_tests:
            setup_function(t)
            await t()
            passed += 1
            print(f"PASS  {t.__name__}")
        print(f"\n{passed}/{len(async_tests)} tests passed")

    asyncio.run(run_all())
