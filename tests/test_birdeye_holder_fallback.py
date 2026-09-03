"""
tests/test_birdeye_holder_fallback.py

Phase 3.2 regression coverage for domain/intelligence/_birdeye_holder_fallback.py.

Sync tests (_extract_accounts parsing) are plain pytest-discoverable
functions. Async tests (fetch_token_holders / install()) follow the same
manual-runner convention as tests/test_locks.py rather than adding a
pytest-asyncio dependency this repo doesn't otherwise use -- run this file
directly with `python tests/test_birdeye_holder_fallback.py`, or import
just the sync tests under plain pytest.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domain.intelligence import holders  # noqa: E402
from domain.intelligence import _birdeye_holder_fallback as birdeye  # noqa: E402
from domain.intelligence._birdeye_holder_fallback import _extract_accounts  # noqa: E402


# ---------------------------------------------------------------------
# Response parsing (sync, pytest-discoverable)
# ---------------------------------------------------------------------
def test_birdeye_extracts_positive_wallet_balances_wrapped_in_data():
    body = {
        "success": True,
        "data": {
            "items": [
                {"owner": "wallet-a", "amount": "100", "ui_amount": 100.0},
                {"owner": "wallet-b", "amount": "50", "ui_amount": 50.0},
                {"owner": "wallet-zero", "amount": "0", "ui_amount": 0.0},
            ],
            "holder": 3,
            "top10HoldPercent": 42.0,
        },
    }
    accounts, total, has_more = _extract_accounts(body)
    assert accounts == [
        {"owner": "wallet-a", "amount": "100"},
        {"owner": "wallet-b", "amount": "50"},
    ]
    assert total == 3
    assert has_more is False  # fewer than MAX_PAGE_SIZE items


def test_birdeye_accepts_unwrapped_top_level_items():
    body = {"items": [{"owner": "wallet-a", "amount": "10"}]}
    accounts, _total, _has_more = _extract_accounts(body)
    assert accounts == [{"owner": "wallet-a", "amount": "10"}]


def test_birdeye_falls_back_to_ui_amount_when_amount_missing():
    body = {"data": {"items": [{"owner": "wallet-a", "ui_amount": 7.5}]}}
    accounts, _total, _has_more = _extract_accounts(body)
    assert accounts == [{"owner": "wallet-a", "amount": "7.5"}]


def test_birdeye_rejects_error_or_malformed_payloads():
    assert _extract_accounts({"success": False, "message": "bad key"}) == ([], None, False)
    assert _extract_accounts({"data": {"items": "bad"}}) == ([], None, False)
    assert _extract_accounts(None) == ([], None, False)
    assert _extract_accounts("not a dict") == ([], None, False)
    assert _extract_accounts({}) == ([], None, False)


def test_birdeye_wallet_alias_normalizes_to_owner():
    body = {"data": {"items": [{"wallet": "wallet-a", "amount": "5"}]}}
    accounts, _total, _has_more = _extract_accounts(body)
    assert accounts == [{"owner": "wallet-a", "amount": "5"}]


# ---------------------------------------------------------------------
# fetch_token_holders / install(): async behavior
# ---------------------------------------------------------------------
async def test_fetch_returns_none_when_not_configured():
    os.environ.pop("BIRDEYE_API_KEY", None)
    assert await birdeye.fetch_token_holders("MintAddr") is None


async def test_fetch_handles_401():
    os.environ["BIRDEYE_API_KEY"] = "test-key"
    with patch.object(birdeye, "_get_page", AsyncMock(return_value=(401, None))):
        assert await birdeye.fetch_token_holders("MintAddr") is None


async def test_fetch_handles_403():
    os.environ["BIRDEYE_API_KEY"] = "test-key"
    with patch.object(birdeye, "_get_page", AsyncMock(return_value=(403, None))):
        assert await birdeye.fetch_token_holders("MintAddr") is None


async def test_fetch_handles_429():
    os.environ["BIRDEYE_API_KEY"] = "test-key"
    with patch.object(birdeye, "_get_page", AsyncMock(return_value=(429, None))):
        assert await birdeye.fetch_token_holders("MintAddr") is None


async def test_fetch_handles_5xx():
    os.environ["BIRDEYE_API_KEY"] = "test-key"
    with patch.object(birdeye, "_get_page", AsyncMock(return_value=(503, None))):
        assert await birdeye.fetch_token_holders("MintAddr") is None


async def test_fetch_handles_timeout_or_connection_failure():
    # _get_page returns (0, None) on any request exception internally.
    os.environ["BIRDEYE_API_KEY"] = "test-key"
    with patch.object(birdeye, "_get_page", AsyncMock(return_value=(0, None))):
        assert await birdeye.fetch_token_holders("MintAddr") is None


async def test_fetch_handles_malformed_response():
    os.environ["BIRDEYE_API_KEY"] = "test-key"
    with patch.object(birdeye, "_get_page", AsyncMock(return_value=(200, "not-a-dict"))):
        assert await birdeye.fetch_token_holders("MintAddr") is None


async def test_fetch_handles_empty_response_as_genuine_zero():
    os.environ["BIRDEYE_API_KEY"] = "test-key"
    body = {"success": True, "data": {"items": [], "holder": 0}}
    with patch.object(birdeye, "_get_page", AsyncMock(return_value=(200, body))):
        result = await birdeye.fetch_token_holders("MintAddr")
    assert result == []  # empty list, NOT None -- provider succeeded


async def test_fetch_handles_successful_response():
    os.environ["BIRDEYE_API_KEY"] = "test-key"
    body = {
        "success": True,
        "data": {
            "items": [
                {"owner": "wallet-a", "amount": "100"},
                {"owner": "wallet-b", "amount": "50"},
            ],
            "holder": 2,
        },
    }
    with patch.object(birdeye, "_get_page", AsyncMock(return_value=(200, body))):
        result = await birdeye.fetch_token_holders("MintAddr")
    assert result == [
        {"owner": "wallet-a", "amount": "100"},
        {"owner": "wallet-b", "amount": "50"},
    ]


async def test_api_key_is_never_logged(caplog=None):
    os.environ["BIRDEYE_API_KEY"] = "super-secret-key-value"
    body = {"success": True, "data": {"items": [{"owner": "w", "amount": "1"}], "holder": 1}}

    import logging

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture()
    birdeye.logger.addHandler(handler)
    try:
        with patch.object(birdeye, "_get_page", AsyncMock(return_value=(200, body))):
            await birdeye.fetch_token_holders("MintAddr")
    finally:
        birdeye.logger.removeHandler(handler)

    for message in records:
        assert "super-secret-key-value" not in message


async def test_birdeye_not_called_when_upstream_result_has_accounts():
    os.environ["BIRDEYE_API_KEY"] = "test-key"

    async def _upstream(contract_address, priority=holders.PRIORITY_LOW):
        return holders._HolderAccountsResult(
            accounts=[{"owner": "w", "amount": "1"}], truncated=False, raw_account_count=1
        )

    original = holders._fetch_token_accounts
    holders._fetch_token_accounts = _upstream
    try:
        birdeye.install()
        with patch.object(
            birdeye, "fetch_token_holders", AsyncMock(side_effect=AssertionError("should not be called"))
        ):
            result = await holders._fetch_token_accounts("MintAddr")
        assert result.accounts == [{"owner": "w", "amount": "1"}]
    finally:
        holders._fetch_token_accounts = original


async def test_birdeye_called_when_upstream_result_is_empty():
    os.environ["BIRDEYE_API_KEY"] = "test-key"

    async def _upstream(contract_address, priority=holders.PRIORITY_LOW):
        return holders._HolderAccountsResult(accounts=[], truncated=False, raw_account_count=0)

    original = holders._fetch_token_accounts
    holders._fetch_token_accounts = _upstream
    try:
        birdeye.install()
        with patch.object(
            birdeye, "fetch_token_holders", AsyncMock(return_value=[{"owner": "w", "amount": "1"}])
        ):
            result = await holders._fetch_token_accounts("MintAddr")
        assert result.accounts == [{"owner": "w", "amount": "1"}]
    finally:
        holders._fetch_token_accounts = original


async def test_solana_tracker_success_prevents_birdeye_call():
    """Chained install: Tracker succeeding must short-circuit before Birdeye runs."""
    os.environ["BIRDEYE_API_KEY"] = "test-key"
    os.environ["SOLANA_TRACKER_API_KEY"] = "test-key"

    from domain.intelligence import _solana_tracker_holder_fallback as tracker

    async def _upstream(contract_address, priority=holders.PRIORITY_LOW):
        return holders._HolderAccountsResult(accounts=[], truncated=False, raw_account_count=0)

    original = holders._fetch_token_accounts
    holders._fetch_token_accounts = _upstream
    try:
        tracker.install()
        birdeye.install()
        with patch.object(
            tracker, "fetch_token_holders", AsyncMock(return_value=[{"owner": "tracker-w", "amount": "9"}])
        ), patch.object(
            birdeye, "fetch_token_holders", AsyncMock(side_effect=AssertionError("birdeye should not be called"))
        ):
            result = await holders._fetch_token_accounts("MintAddr")
        assert result.accounts == [{"owner": "tracker-w", "amount": "9"}]
    finally:
        holders._fetch_token_accounts = original


async def test_provider_failure_not_interpreted_as_zero_holders():
    """Birdeye failing (None) must preserve the ORIGINAL upstream result,
    not collapse into a fabricated zero-holder result."""
    os.environ["BIRDEYE_API_KEY"] = "test-key"

    upstream_result = holders._HolderAccountsResult(accounts=[], truncated=False, raw_account_count=0)

    async def _upstream(contract_address, priority=holders.PRIORITY_LOW):
        return upstream_result

    original = holders._fetch_token_accounts
    holders._fetch_token_accounts = _upstream
    try:
        birdeye.install()
        with patch.object(birdeye, "fetch_token_holders", AsyncMock(return_value=None)):
            result = await holders._fetch_token_accounts("MintAddr")
        assert result is upstream_result
    finally:
        holders._fetch_token_accounts = original


async def test_all_providers_failing_produces_safe_failure():
    os.environ["BIRDEYE_API_KEY"] = "test-key"

    async def _upstream(contract_address, priority=holders.PRIORITY_LOW):
        return None  # every earlier provider in the chain failed

    original = holders._fetch_token_accounts
    holders._fetch_token_accounts = _upstream
    try:
        birdeye.install()
        with patch.object(birdeye, "fetch_token_holders", AsyncMock(return_value=None)):
            result = await holders._fetch_token_accounts("MintAddr")
        assert result is None
    finally:
        holders._fetch_token_accounts = original


if __name__ == "__main__":
    async_tests = [
        test_fetch_returns_none_when_not_configured,
        test_fetch_handles_401,
        test_fetch_handles_403,
        test_fetch_handles_429,
        test_fetch_handles_5xx,
        test_fetch_handles_timeout_or_connection_failure,
        test_fetch_handles_malformed_response,
        test_fetch_handles_empty_response_as_genuine_zero,
        test_fetch_handles_successful_response,
        test_api_key_is_never_logged,
        test_birdeye_not_called_when_upstream_result_has_accounts,
        test_birdeye_called_when_upstream_result_is_empty,
        test_solana_tracker_success_prevents_birdeye_call,
        test_provider_failure_not_interpreted_as_zero_holders,
        test_all_providers_failing_produces_safe_failure,
    ]
    sync_tests = [
        test_birdeye_extracts_positive_wallet_balances_wrapped_in_data,
        test_birdeye_accepts_unwrapped_top_level_items,
        test_birdeye_falls_back_to_ui_amount_when_amount_missing,
        test_birdeye_rejects_error_or_malformed_payloads,
        test_birdeye_wallet_alias_normalizes_to_owner,
    ]

    async def run_all():
        passed = 0
        total = len(sync_tests) + len(async_tests)
        for t in sync_tests:
            t()
            passed += 1
            print(f"PASS  {t.__name__}")
        for t in async_tests:
            await t()
            passed += 1
            print(f"PASS  {t.__name__}")
        print(f"\n{passed}/{total} tests passed")

    asyncio.run(run_all())
