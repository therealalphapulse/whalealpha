"""
tests/test_solana_tracker_holder_circuit_breaker.py

Regression coverage for the AlphaPulse Provider Resilience task
(2026-08-28), holder-data half of the fix:
domain/intelligence/_solana_tracker_holder_fallback.py.

Proves, against the REAL holders.py / install() wiring (not a rewritten
stand-in):
  1. Repeated provider failures trip the circuit.
  2. Calls are suppressed (network never touched) during cooldown.
  3. Recovery works once the cooldown elapses and a probe succeeds.
  4. A later-page failure preserves the earlier page's real data instead
     of discarding it, and marks the result truncated.
  5. A genuinely empty successful response is still [] (not confused
     with "unavailable"), per this module's existing contract.
  6. When the breaker is open, install()'s wrapper still falls through to
     whatever the ORIGINAL upstream result was — i.e. a legitimate signal
     is never suppressed just because Solana Tracker is unavailable; the
     next provider in workers/holder_runtime_bootstrap.py's chain
     (Birdeye) gets its turn exactly as it would for any other kind of
     Solana Tracker failure.

Follows the manual-runner convention already used in this repo
(tests/test_birdeye_holder_fallback.py) since there is no pytest-asyncio
dependency.
"""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SOLANA_TRACKER_API_KEY", "test-key")

from domain.intelligence import holders  # noqa: E402
from domain.intelligence import _solana_tracker_holder_fallback as tracker  # noqa: E402
from providers.marketdata import _provider_circuit_breaker as breaker  # noqa: E402


def setup_function(_fn):
    breaker.reset(tracker._BREAKER_KEY)
    os.environ["SOLANA_TRACKER_API_KEY"] = "test-key"
    # install() is idempotent-guarded (_alphapulse_solana_tracker_fallback
    # flag) and monkeypatches a MODULE-LEVEL function, so every test gets a
    # completely fresh, un-monkeypatched holders._fetch_token_accounts to
    # avoid tests wiring on top of each other.
    holders._fetch_token_accounts = holders._fetch_token_accounts_original \
        if hasattr(holders, "_fetch_token_accounts_original") \
        else holders._fetch_token_accounts
    if not hasattr(holders, "_fetch_token_accounts_original"):
        holders._fetch_token_accounts_original = holders._fetch_token_accounts


def _page(status, accounts=None, total=None, has_more=False, cursor=None):
    body = {
        "total": total if total is not None else len(accounts or []),
        "accounts": [{"owner": a[0], "amount": str(a[1])} for a in (accounts or [])],
        "hasMore": has_more,
    }
    if cursor:
        body["cursor"] = cursor
    return (status, body if status == 200 else {"error": "boom"})


# ---------------------------------------------------------------------
# 1 + 2: repeated failures trip the circuit; suppressed during cooldown
# ---------------------------------------------------------------------
async def test_repeated_403_trips_circuit_and_suppresses_further_calls():
    call_count = {"n": 0}

    async def fake_get_page(*args, **kwargs):
        call_count["n"] += 1
        return (403, {"error": "insufficient credits"})

    with patch.object(tracker, "_get_page", fake_get_page):
        result1 = await tracker.fetch_token_holders("Mint1111111111111111111111111111111111111")
        assert result1 is None
        assert breaker.is_open(tracker._BREAKER_KEY) is True  # auth threshold is 1
        assert call_count["n"] == 1

        # Second call for a DIFFERENT token must not touch the network at
        # all -- this is the "stop being repeatedly called" requirement.
        result2 = await tracker.fetch_token_holders("Mint2222222222222222222222222222222222222")
        assert result2 is None
        assert call_count["n"] == 1  # unchanged -- _get_page was not called again


# ---------------------------------------------------------------------
# 3: recovery works
# ---------------------------------------------------------------------
async def test_circuit_recovers_after_cooldown_and_successful_probe():
    async def failing_get_page(*args, **kwargs):
        return (403, {"error": "insufficient credits"})

    with patch.object(tracker, "_get_page", failing_get_page):
        await tracker.fetch_token_holders("Mint1111111111111111111111111111111111111")
    assert breaker.is_open(tracker._BREAKER_KEY) is True

    state = breaker._get(tracker._BREAKER_KEY)
    state.opened_at = time.monotonic() - 999999  # force cooldown elapsed

    async def healthy_get_page(*args, **kwargs):
        return _page(200, accounts=[("wallet-a", 100.0)], has_more=False)

    with patch.object(tracker, "_get_page", healthy_get_page):
        result = await tracker.fetch_token_holders("Mint3333333333333333333333333333333333333")

    assert result == [{"owner": "wallet-a", "amount": "100.0"}]
    assert breaker.is_open(tracker._BREAKER_KEY) is False  # probe succeeded, circuit closed


# ---------------------------------------------------------------------
# 4: partial-page preservation (the discard-good-data bug)
# ---------------------------------------------------------------------
async def test_partial_fetch_preserves_first_page_and_marks_truncated():
    pages = [
        _page(200, accounts=[("wallet-a", 500.0), ("wallet-b", 300.0)], has_more=True, cursor="page2"),
        (403, {"error": "insufficient credits"}),
    ]
    call_log = []

    async def fake_get_page(*args, **kwargs):
        call_log.append(kwargs.get("cursor"))
        return pages[len(call_log) - 1]

    with patch.object(tracker, "_get_page", fake_get_page):
        accounts, is_partial = await tracker._fetch_paginated_holders(
            "Mint4444444444444444444444444444444444444"
        )

    # The old behavior discarded this and returned None. The fix preserves
    # the two real accounts fetched on page 1 and flags the result partial.
    assert accounts is not None
    assert len(accounts) == 2
    assert {a["owner"] for a in accounts} == {"wallet-a", "wallet-b"}
    assert is_partial is True
    assert len(call_log) == 2  # page 2 was actually attempted


async def test_full_fetch_with_no_page_failures_is_not_marked_partial():
    async def fake_get_page(*args, **kwargs):
        return _page(200, accounts=[("wallet-a", 100.0)], has_more=False)

    with patch.object(tracker, "_get_page", fake_get_page):
        accounts, is_partial = await tracker._fetch_paginated_holders(
            "Mint5555555555555555555555555555555555555"
        )
    assert accounts == [{"owner": "wallet-a", "amount": "100.0"}]
    assert is_partial is False


# ---------------------------------------------------------------------
# 5: genuine empty result is still [] (not confused with unavailable)
# ---------------------------------------------------------------------
async def test_genuine_empty_holder_set_returns_empty_list_not_none():
    async def fake_get_page(*args, **kwargs):
        return _page(200, accounts=[], has_more=False)

    with patch.object(tracker, "_get_page", fake_get_page):
        result = await tracker.fetch_token_holders("Mint6666666666666666666666666666666666666")
    assert result == []
    assert result is not None


# ---------------------------------------------------------------------
# 6: circuit-open does not suppress a legitimate signal -- install()'s
# wrapper falls through to whatever the ORIGINAL upstream result was,
# exactly as it already does for any other Solana Tracker failure.
# ---------------------------------------------------------------------
async def test_install_falls_through_to_upstream_result_when_circuit_open():
    breaker.record_failure(tracker._BREAKER_KEY, breaker.FAILURE_AUTH_OR_CREDITS)
    assert breaker.is_open(tracker._BREAKER_KEY) is True

    upstream_result = holders._HolderAccountsResult(
        accounts=[{"owner": "wallet-primary", "amount": "999"}],
        truncated=False,
        raw_account_count=1,
    )

    async def fake_original_fetch(contract_address, priority=None):
        return upstream_result

    holders._fetch_token_accounts = fake_original_fetch
    tracker.install.__wrapped__ = None  # no-op guard reset not needed; re-call below

    # install() is idempotent-guarded via an attribute on the CURRENT
    # holders._fetch_token_accounts, which we just replaced above, so
    # calling install() here genuinely re-wraps our fake.
    tracker.install()

    with patch.object(tracker, "_get_page", AsyncMock(side_effect=AssertionError(
        "network must not be touched while breaker is open"
    ))):
        result = await holders._fetch_token_accounts(
            "Mint7777777777777777777777777777777777777", priority=holders.PRIORITY_LOW
        )

    # A legitimate primary-path result should never be suppressed just
    # because Solana Tracker's circuit is open.
    assert result is upstream_result
    assert result.accounts == [{"owner": "wallet-primary", "amount": "999"}]


async def test_install_falls_through_to_none_when_both_primary_and_tracker_have_nothing():
    """When the primary path found nothing AND Solana Tracker's circuit is
    open, the wrapper must return the same 'unavailable' shape the primary
    path already returns -- not fabricate a fake empty/positive result."""
    breaker.record_failure(tracker._BREAKER_KEY, breaker.FAILURE_AUTH_OR_CREDITS)

    async def fake_original_fetch(contract_address, priority=None):
        return None  # primary RPC path genuinely unavailable

    holders._fetch_token_accounts = fake_original_fetch
    tracker.install()

    with patch.object(tracker, "_get_page", AsyncMock(side_effect=AssertionError(
        "network must not be touched while breaker is open"
    ))):
        result = await holders._fetch_token_accounts(
            "Mint8888888888888888888888888888888888888", priority=holders.PRIORITY_LOW
        )

    assert result is None  # unavailable stays unavailable; never fabricated


if __name__ == "__main__":
    async_tests = [
        test_repeated_403_trips_circuit_and_suppresses_further_calls,
        test_circuit_recovers_after_cooldown_and_successful_probe,
        test_partial_fetch_preserves_first_page_and_marks_truncated,
        test_full_fetch_with_no_page_failures_is_not_marked_partial,
        test_genuine_empty_holder_set_returns_empty_list_not_none,
        test_install_falls_through_to_upstream_result_when_circuit_open,
        test_install_falls_through_to_none_when_both_primary_and_tracker_have_nothing,
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
