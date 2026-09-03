"""
tests/test_provider_circuit_breaker.py

Regression coverage for providers/marketdata/_provider_circuit_breaker.py
(AlphaPulse Provider Resilience task, 2026-08-28).

Plain pytest-discoverable sync functions -- the breaker itself has no I/O,
so no async runner is needed here (contrast with
tests/test_solana_tracker_resilience.py, which follows the manual-runner
convention from tests/test_birdeye_holder_fallback.py for the async
provider-call tests).
"""

import time

from providers.marketdata import _provider_circuit_breaker as breaker


def setup_function(_fn):
    """Each test gets a clean, isolated provider key so failure counts from
    one test never leak into another."""
    breaker.reset("test-provider")
    breaker.reset("test-provider-2")


def test_closed_circuit_allows_requests():
    assert breaker.allow_request("test-provider") is True
    assert breaker.is_open("test-provider") is False


def test_single_failure_does_not_trip_default_threshold():
    breaker.record_failure("test-provider", breaker.FAILURE_TRANSIENT)
    assert breaker.is_open("test-provider") is False
    assert breaker.allow_request("test-provider") is True


def test_transient_failures_trip_circuit_at_threshold():
    # Default SOLANA_TRACKER_CIRCUIT_BREAKER_THRESHOLD is 3.
    breaker.record_failure("test-provider", breaker.FAILURE_TRANSIENT)
    breaker.record_failure("test-provider", breaker.FAILURE_TRANSIENT)
    assert breaker.is_open("test-provider") is False
    breaker.record_failure("test-provider", breaker.FAILURE_TRANSIENT)
    assert breaker.is_open("test-provider") is True


def test_auth_or_credit_failure_trips_immediately():
    """403 / 401 / 402 must not get the same tolerance as a transient
    network blip -- see module docstring: retrying an auth/credits failure
    cannot succeed until a human intervenes."""
    breaker.record_failure("test-provider", breaker.FAILURE_AUTH_OR_CREDITS)
    assert breaker.is_open("test-provider") is True


def test_open_circuit_suppresses_calls_before_cooldown():
    breaker.record_failure("test-provider", breaker.FAILURE_AUTH_OR_CREDITS)
    assert breaker.is_open("test-provider") is True
    # Cooldown default is 120s -- immediately after tripping, no call should
    # be allowed through at all (this is the "stop being repeatedly called"
    # requirement).
    assert breaker.allow_request("test-provider") is False
    assert breaker.allow_request("test-provider") is False


def test_success_resets_and_closes_circuit():
    breaker.record_failure("test-provider", breaker.FAILURE_TRANSIENT)
    breaker.record_failure("test-provider", breaker.FAILURE_TRANSIENT)
    breaker.record_success("test-provider")
    assert breaker.is_open("test-provider") is False
    # Confirm the failure count actually reset, not just the open flag --
    # two more failures should NOT trip it immediately afterward.
    breaker.record_failure("test-provider", breaker.FAILURE_TRANSIENT)
    assert breaker.is_open("test-provider") is False


def test_recovery_probe_allowed_after_cooldown_elapses():
    breaker.record_failure("test-provider", breaker.FAILURE_AUTH_OR_CREDITS)
    assert breaker.allow_request("test-provider") is False

    state = breaker._get("test-provider")
    # Simulate the cooldown having elapsed without sleeping in a test.
    state.opened_at = time.monotonic() - 999999

    assert breaker.allow_request("test-provider") is True  # the recovery probe


def test_concurrent_callers_during_half_open_only_get_one_probe():
    breaker.record_failure("test-provider", breaker.FAILURE_AUTH_OR_CREDITS)
    state = breaker._get("test-provider")
    state.opened_at = time.monotonic() - 999999

    first = breaker.allow_request("test-provider")
    second = breaker.allow_request("test-provider")
    assert first is True
    assert second is False  # a second concurrent caller must not also probe


def test_failed_recovery_probe_reopens_circuit():
    breaker.record_failure("test-provider", breaker.FAILURE_AUTH_OR_CREDITS)
    state = breaker._get("test-provider")
    state.opened_at = time.monotonic() - 999999
    assert breaker.allow_request("test-provider") is True  # claims the probe

    breaker.record_failure("test-provider", breaker.FAILURE_AUTH_OR_CREDITS)
    assert breaker.is_open("test-provider") is True
    assert breaker.allow_request("test-provider") is False  # still suppressed


def test_successful_recovery_probe_closes_circuit_and_resumes_calls():
    breaker.record_failure("test-provider", breaker.FAILURE_AUTH_OR_CREDITS)
    state = breaker._get("test-provider")
    state.opened_at = time.monotonic() - 999999
    assert breaker.allow_request("test-provider") is True  # claims the probe

    breaker.record_success("test-provider")
    assert breaker.is_open("test-provider") is False
    # Normal calls resume without needing another cooldown wait.
    assert breaker.allow_request("test-provider") is True
    assert breaker.allow_request("test-provider") is True


def test_providers_are_tracked_independently():
    breaker.record_failure("test-provider", breaker.FAILURE_AUTH_OR_CREDITS)
    assert breaker.is_open("test-provider") is True
    assert breaker.is_open("test-provider-2") is False
    assert breaker.allow_request("test-provider-2") is True


if __name__ == "__main__":
    import sys

    test_functions = [
        obj
        for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = 0
    for fn in test_functions:
        setup_function(fn)
        fn()
        passed += 1
        print(f"PASS  {fn.__name__}")
    print(f"\n{passed}/{len(test_functions)} tests passed")
    sys.exit(0)
