"""Tests for the rate-limiting/retry helper in integrations/solana_connection.py
— added after production logs showed a burst of 429s from Solana RPC
(mainnet.helius-rpc.com) when the discovery engine resolved holders for
~20 trending tokens in a row with no pacing between calls.
"""

from __future__ import annotations

import time

import pytest

from whale_alpha.integrations.solana_connection import (
    _is_rate_limited_error,
    _rate_limited_rpc_call,
)


class _FakeHTTPStatusError(Exception):
    """Stand-in for whatever HTTP client solana-py vendors internally
    (historically shipped under a different import name — see
    _is_rate_limited_error's docstring) — has a `.response.status_code`
    like a real httpx.HTTPStatusError."""

    def __init__(self, status_code: int):
        super().__init__(f"HTTP error {status_code}")

        class _Response:
            pass

        self.response = _Response()
        self.response.status_code = status_code


# --------------------------------------------------------------------------
# _is_rate_limited_error — pure
# --------------------------------------------------------------------------


def test_detects_429_via_response_status_code():
    assert _is_rate_limited_error(_FakeHTTPStatusError(429)) is True


def test_does_not_flag_other_status_codes_via_response():
    assert _is_rate_limited_error(_FakeHTTPStatusError(500)) is False
    assert _is_rate_limited_error(_FakeHTTPStatusError(404)) is False


def test_falls_back_to_string_matching_when_no_response_attribute():
    assert _is_rate_limited_error(Exception("429 Too Many Requests")) is True
    assert _is_rate_limited_error(Exception("Too Many Requests")) is True


def test_does_not_flag_an_unrelated_error():
    assert _is_rate_limited_error(Exception("connection reset")) is False
    assert _is_rate_limited_error(ValueError("bad input")) is False


# --------------------------------------------------------------------------
# _rate_limited_rpc_call — pacing + retry-on-429
# --------------------------------------------------------------------------


async def test_paces_consecutive_calls_at_least_min_interval_apart():
    call_times: list[float] = []

    async def fn():
        call_times.append(time.monotonic())
        return "ok"

    await _rate_limited_rpc_call(fn, min_interval_seconds=0.05, max_retries=0)
    await _rate_limited_rpc_call(fn, min_interval_seconds=0.05, max_retries=0)

    assert len(call_times) == 2
    assert call_times[1] - call_times[0] >= 0.04  # small tolerance for scheduling jitter


async def test_retries_on_429_and_eventually_succeeds():
    attempts = 0

    async def fn():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _FakeHTTPStatusError(429)
        return "ok"

    result = await _rate_limited_rpc_call(fn, min_interval_seconds=0.01, max_retries=5)
    assert result == "ok"
    assert attempts == 3


async def test_gives_up_after_max_retries_on_persistent_429():
    async def fn():
        raise _FakeHTTPStatusError(429)

    with pytest.raises(_FakeHTTPStatusError):
        await _rate_limited_rpc_call(fn, min_interval_seconds=0.01, max_retries=2)


async def test_non_429_error_is_not_retried():
    attempts = 0

    async def fn():
        nonlocal attempts
        attempts += 1
        raise ValueError("not a rate limit")

    with pytest.raises(ValueError):
        await _rate_limited_rpc_call(fn, min_interval_seconds=0.01, max_retries=5)
    assert attempts == 1  # failed fast, no retry burned on a non-429 error
