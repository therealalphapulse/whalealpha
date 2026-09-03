"""
tests/test_rugcheck_fallback.py

Regression coverage for providers/marketdata/rugcheck.py (AlphaPulse
Provider Integration Task, 2026-08-19) and its integration point in
providers/marketdata/goplus.py::check_token_security().

Sync tests (_normalize_rugcheck_security / helpers) are plain
pytest-discoverable functions. Async tests follow the same manual-runner
convention as tests/test_birdeye_holder_fallback.py rather than adding a
pytest-asyncio dependency this repo doesn't otherwise use -- run this file
directly with `python tests/test_rugcheck_fallback.py`, or import just the
sync tests under plain pytest.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.marketdata import rugcheck  # noqa: E402
from providers.marketdata import goplus  # noqa: E402
from providers.marketdata.rugcheck import _normalize_rugcheck_security  # noqa: E402


# ---------------------------------------------------------------------
# _normalize_rugcheck_security: response mapping (sync, pytest-discoverable)
# ---------------------------------------------------------------------
def test_rugcheck_normalizes_full_report_to_goplus_shape():
    payload = {
        "token": {"mintAuthority": "SomeAuthorityAddr", "freezeAuthority": None, "supply": 1_000_000},
        "topHolders": [
            {"pct": 12.5},
            {"pct": 8.0},
            {"pct": 3.0},
        ],
        "totalHolders": 421,
        "creator": "CreatorAddr111",
        "verification": {"jup_verified": True},
        "rugged": False,
    }
    data = _normalize_rugcheck_security(payload)

    assert data["mintable"] == "1"          # mintAuthority present
    assert data["freezable"] == "0"         # freezeAuthority null
    assert data["holder_count"] == "421"
    assert data["top_holder_percent"] == 12.5
    assert data["top_10_holder_percent"] == 23.5
    assert data["creator_address"] == "CreatorAddr111"
    assert data["total_supply"] == "1000000"
    assert data["trusted_token"] == "1"
    assert data["is_honeypot"] == "0"
    # Fields RugCheck doesn't report stay "unknown", never fabricated.
    assert data["metadata_mutable"] == "unknown"
    assert data["closable"] == "unknown"


def test_rugcheck_marks_rugged_token_as_honeypot():
    payload = {"token": {}, "rugged": True}
    data = _normalize_rugcheck_security(payload)
    assert data["is_honeypot"] == "1"


def test_rugcheck_rejects_malformed_payloads():
    assert _normalize_rugcheck_security(None) is None
    assert _normalize_rugcheck_security({}) is None
    assert _normalize_rugcheck_security({"token": "not-a-dict"}) is None
    assert _normalize_rugcheck_security("not-a-dict") is None


def test_rugcheck_handles_missing_holder_data_without_crashing():
    payload = {"token": {}, "topHolders": None}
    data = _normalize_rugcheck_security(payload)
    assert data["top_holder_percent"] is None
    assert data["top_10_holder_percent"] is None


def test_rugcheck_output_has_the_same_keys_goplus_produces():
    """The whole point of the fallback is that callers of
    check_token_security() can't tell which provider answered -- so the
    key set must match exactly."""
    from providers.marketdata.goplus import _normalize_token_security

    goplus_keys = set(
        _normalize_token_security(
            {"holders": [], "mintable": "0", "freezable": "0"}
        ).keys()
    )
    rugcheck_keys = set(_normalize_rugcheck_security({"token": {}}).keys())
    assert rugcheck_keys == goplus_keys


# ---------------------------------------------------------------------
# rugcheck.check_token_security: async behavior
# ---------------------------------------------------------------------
async def test_rugcheck_returns_none_when_not_configured():
    with patch.object(rugcheck, "RUGCHECK_API_KEY", None):
        assert await rugcheck.check_token_security("MintAddr") is None


async def test_rugcheck_returns_none_on_fetch_failure():
    with patch.object(rugcheck, "RUGCHECK_API_KEY", "test-key"), patch.object(
        rugcheck, "get_json", AsyncMock(return_value=None)
    ):
        assert await rugcheck.check_token_security("MintAddr") is None


async def test_rugcheck_returns_normalized_data_on_success():
    body = {"token": {"mintAuthority": None, "freezeAuthority": None}, "totalHolders": 5}
    with patch.object(rugcheck, "RUGCHECK_API_KEY", "test-key"), patch.object(
        rugcheck, "get_json", AsyncMock(return_value=body)
    ):
        result = await rugcheck.check_token_security("MintAddr")
    assert result is not None
    assert result["holder_count"] == "5"


async def test_rugcheck_never_raises_on_transport_exception():
    with patch.object(rugcheck, "RUGCHECK_API_KEY", "test-key"), patch.object(
        rugcheck, "get_json", AsyncMock(side_effect=RuntimeError("boom"))
    ):
        assert await rugcheck.check_token_security("MintAddr") is None


# ---------------------------------------------------------------------
# goplus.check_token_security: fallback wiring integration
# ---------------------------------------------------------------------
async def test_goplus_success_never_calls_rugcheck_fallback():
    """GoPlus succeeding must short-circuit before RugCheck is ever
    imported/called -- the fallback is additive, not a replacement."""
    good_payload = {"result": {"mintable": "0", "holders": []}}
    with patch.object(goplus, "get_json", AsyncMock(return_value=good_payload)):
        with patch(
            "providers.marketdata.rugcheck.check_token_security",
            AsyncMock(side_effect=AssertionError("RugCheck should not be called")),
        ):
            result = await goplus.check_token_security("MintAddr")
    assert result is not None


async def test_goplus_failure_falls_back_to_rugcheck():
    """Both GoPlus endpoint attempts failing must trigger the RugCheck
    fallback, and its normalized data must be returned unchanged."""
    fallback_data = {"holder_count": "99", "mintable": "0"}
    with patch.object(goplus, "get_json", AsyncMock(return_value=None)):
        with patch(
            "providers.marketdata.rugcheck.check_token_security",
            AsyncMock(return_value=fallback_data),
        ):
            result = await goplus.check_token_security("MintAddr")
    assert result == fallback_data


async def test_goplus_and_rugcheck_both_failing_returns_none():
    """Existing provider behavior preserved: both providers unavailable
    still yields None, exactly as GoPlus alone did before this change."""
    with patch.object(goplus, "get_json", AsyncMock(return_value=None)):
        with patch(
            "providers.marketdata.rugcheck.check_token_security",
            AsyncMock(return_value=None),
        ):
            result = await goplus.check_token_security("MintAddr")
    assert result is None


async def test_rugcheck_fallback_exception_does_not_propagate():
    """A RugCheck-side exception must not break check_token_security()'s
    existing None-on-failure contract for callers."""
    with patch.object(goplus, "get_json", AsyncMock(return_value=None)):
        with patch(
            "providers.marketdata.rugcheck.check_token_security",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await goplus.check_token_security("MintAddr")
    assert result is None


if __name__ == "__main__":
    sync_tests = [
        test_rugcheck_normalizes_full_report_to_goplus_shape,
        test_rugcheck_marks_rugged_token_as_honeypot,
        test_rugcheck_rejects_malformed_payloads,
        test_rugcheck_handles_missing_holder_data_without_crashing,
        test_rugcheck_output_has_the_same_keys_goplus_produces,
    ]
    async_tests = [
        test_rugcheck_returns_none_when_not_configured,
        test_rugcheck_returns_none_on_fetch_failure,
        test_rugcheck_returns_normalized_data_on_success,
        test_rugcheck_never_raises_on_transport_exception,
        test_goplus_success_never_calls_rugcheck_fallback,
        test_goplus_failure_falls_back_to_rugcheck,
        test_goplus_and_rugcheck_both_failing_returns_none,
        test_rugcheck_fallback_exception_does_not_propagate,
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
