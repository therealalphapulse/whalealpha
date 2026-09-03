"""Regression coverage for provider error-detail logging (Aug 15 2026 incident).

Both Birdeye and Solana Tracker holder fallbacks were swallowing the
response body on non-200 status, leaving production logs with only an
HTTP status code and no way to tell a request-shape bug apart from a
real plan/quota restriction. _error_detail() must reliably surface
whatever message the provider actually sent.
"""
from domain.intelligence import _birdeye_holder_fallback as birdeye
from domain.intelligence import _solana_tracker_holder_fallback as tracker


def test_birdeye_error_detail_extracts_message():
    assert birdeye._error_detail({"success": False, "message": "Invalid API key"}) == "Invalid API key"


def test_birdeye_error_detail_tries_alternate_keys():
    assert birdeye._error_detail({"error": "plan does not include this endpoint"}) == "plan does not include this endpoint"


def test_birdeye_error_detail_handles_missing_or_bad_body():
    assert birdeye._error_detail(None) is None
    assert birdeye._error_detail("not a dict") is None
    assert birdeye._error_detail({}) is None


def test_birdeye_error_detail_truncates_long_messages():
    long_msg = "x" * 500
    result = birdeye._error_detail({"message": long_msg})
    assert result is not None and len(result) <= 200


def test_solana_tracker_error_detail_extracts_message():
    assert tracker._error_detail({"error": "quota exceeded"}) == "quota exceeded"


def test_solana_tracker_error_detail_handles_missing_body():
    assert tracker._error_detail(None) is None
