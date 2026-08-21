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


# --------------------------------------------------------------------------
# _resolve_rpc_provider_specs — endpoint resolution, roles, dedup
# --------------------------------------------------------------------------


def _make_env(**overrides):
    """Builds a minimal Env for these tests without touching real env vars
    or requiring the full .env — only the fields solana_connection.py reads
    matter here."""
    from whale_alpha.config import Env

    defaults = dict(
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="postgresql://a:b@localhost/db",
        ENCRYPTION_KEY="0" * 64,
        JWT_SECRET="x" * 16,
        SOLANA_RPC_URL="https://primary.example.com",
    )
    defaults.update(overrides)
    return Env(**defaults)


def test_single_endpoint_when_nothing_else_configured():
    from whale_alpha.integrations.solana_connection import _resolve_rpc_provider_specs

    # devnet so the mainnet-only public safety nets aren't auto-added —
    # isolates "nothing else configured" from that separate behavior,
    # which test_no_public_safety_net_off_mainnet already covers directly.
    specs = _resolve_rpc_provider_specs(_make_env(SOLANA_CLUSTER="devnet"))
    assert [s.url for s in specs] == ["https://primary.example.com"]
    assert specs[0].role == "primary"


def test_keyed_providers_are_tagged_secondary_and_public_nets_appended_on_mainnet():
    from whale_alpha.integrations.solana_connection import _resolve_rpc_provider_specs

    specs = _resolve_rpc_provider_specs(_make_env(ALCHEMY_API_KEY="k1", ANKR_API_KEY="k2"))
    by_name = {s.name: s for s in specs}

    assert by_name["primary"].role == "primary"
    assert by_name["alchemy"].role == "secondary"
    assert by_name["alchemy"].url == "https://solana-mainnet.g.alchemy.com/v2/k1"
    assert by_name["ankr"].role == "secondary"
    # free public safety nets present on mainnet-beta even without being configured
    assert by_name["drpc-public"].role == "public"
    assert by_name["ankr-public"].role == "public"


def test_no_public_safety_net_off_mainnet():
    from whale_alpha.integrations.solana_connection import _resolve_rpc_provider_specs

    specs = _resolve_rpc_provider_specs(_make_env(SOLANA_CLUSTER="devnet"))
    assert [s.role for s in specs] == ["primary"]


def test_fallback_urls_and_duplicates_are_deduped_preserving_first_occurrence():
    from whale_alpha.integrations.solana_connection import _resolve_rpc_provider_specs

    env = _make_env(
        SOLANA_CLUSTER="devnet",  # isolate from the mainnet-only public safety nets
        SOLANA_RPC_FALLBACK_URLS="https://a.example.com, https://primary.example.com, https://b.example.com",
    )
    specs = _resolve_rpc_provider_specs(env)
    urls = [s.url for s in specs]
    # primary.example.com only appears once (first occurrence, as "primary")
    assert urls.count("https://primary.example.com") == 1
    assert urls == [
        "https://primary.example.com",
        "https://a.example.com",
        "https://b.example.com",
    ]
    assert [s.name for s in specs] == ["primary", "fallback-0", "fallback-2"]


# --------------------------------------------------------------------------
# _redact_endpoint
# --------------------------------------------------------------------------


def test_redact_endpoint_strips_key_from_path_and_query():
    from whale_alpha.integrations.solana_connection import _redact_endpoint

    assert _redact_endpoint("https://solana-mainnet.g.alchemy.com/v2/SECRET") == (
        "https://solana-mainnet.g.alchemy.com/v2/***"
    )
    assert _redact_endpoint("https://solana.drpc.org?dkey=SECRET") == "https://solana.drpc.org?***"
    assert _redact_endpoint("https://api.mainnet-beta.solana.com") == "https://api.mainnet-beta.solana.com"


# --------------------------------------------------------------------------
# resolve_websocket_url
# --------------------------------------------------------------------------


def test_resolve_websocket_url_prefers_explicit_override():
    from whale_alpha.integrations.solana_connection import resolve_websocket_url

    env = _make_env(SOLANA_WS_URL="wss://custom.example.com", ALCHEMY_API_KEY="k1")
    assert resolve_websocket_url(env) == "wss://custom.example.com"


def test_resolve_websocket_url_falls_back_to_alchemy_then_drpc_then_none():
    from whale_alpha.integrations.solana_connection import resolve_websocket_url

    assert (
        resolve_websocket_url(_make_env(ALCHEMY_API_KEY="k1")) == "wss://solana-mainnet.g.alchemy.com/v2/k1"
    )
    assert resolve_websocket_url(_make_env(DRPC_API_KEY="k2")) == "wss://solana.drpc.org?dkey=k2"
    assert resolve_websocket_url(_make_env()) is None


# --------------------------------------------------------------------------
# _FailoverAsyncClient — workload-aware routing + per-workload failover
# --------------------------------------------------------------------------


def _build_failover_client(env, *, routing_strategy="balanced", max_attempts=4):
    from whale_alpha.integrations.solana_connection import _FailoverAsyncClient, _resolve_rpc_provider_specs

    specs = _resolve_rpc_provider_specs(env)
    return _FailoverAsyncClient(specs, max_attempts=max_attempts, routing_strategy=routing_strategy)


def test_balanced_routing_prefers_secondary_for_bulk_reads_and_primary_for_tx():
    fc = _build_failover_client(_make_env(ALCHEMY_API_KEY="k1"))

    assert [fc._names[i] for i in fc._routes["account_lookup"]][0] == "alchemy"
    assert [fc._names[i] for i in fc._routes["token_metadata"]][0] == "alchemy"
    assert [fc._names[i] for i in fc._routes["tx_submission"]][0] == "primary"
    assert [fc._names[i] for i in fc._routes["tx_history"]][0] == "primary"

    # sanity: initial active provider matches the route's first entry
    assert fc._names[fc._active["account_lookup"]] == "alchemy"
    assert fc._names[fc._active["tx_submission"]] == "primary"


def test_primary_first_strategy_ignores_workload():
    fc = _build_failover_client(_make_env(ALCHEMY_API_KEY="k1"), routing_strategy="primary_first")

    for category in ("account_lookup", "token_metadata", "tx_history", "tx_submission", "general"):
        assert fc._names[fc._routes[category][0]] == "primary"


class _FakeRpcClient:
    def __init__(self, name: str, fails: int = 0):
        self.name = name
        self.fails = fails
        self.calls = 0

    async def get_balance(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError(f"{self.name} unavailable")
        return f"balance-from-{self.name}"

    async def get_signatures_for_address(self, *args, **kwargs):
        self.calls += 1
        return f"sigs-from-{self.name}"


def _swap_in_fake_clients(fc, fakes: dict[str, "_FakeRpcClient"]):
    name_to_index = {name: i for i, name in enumerate(fc._names)}
    for name, fake in fakes.items():
        fc._clients[name_to_index[name]] = fake


async def test_failover_moves_to_next_provider_in_workload_route_on_error():
    fc = _build_failover_client(_make_env(ALCHEMY_API_KEY="k1"))
    _swap_in_fake_clients(
        fc,
        {
            "alchemy": _FakeRpcClient("alchemy", fails=99),  # always fails get_balance
            "primary": _FakeRpcClient("primary"),
        },
    )

    result = await fc.get_balance("addr")

    assert result == "balance-from-primary"
    assert fc._names[fc._active["account_lookup"]] == "primary"


async def test_failover_is_sticky_and_does_not_retry_the_dead_provider_again():
    fc = _build_failover_client(_make_env(ALCHEMY_API_KEY="k1"))
    alchemy = _FakeRpcClient("alchemy", fails=99)
    primary = _FakeRpcClient("primary")
    _swap_in_fake_clients(fc, {"alchemy": alchemy, "primary": primary})

    await fc.get_balance("addr1")
    await fc.get_balance("addr2")

    assert alchemy.calls == 1  # only tried once, ever
    assert primary.calls == 2  # every call since the switch went straight here


async def test_different_workloads_track_active_provider_independently():
    fc = _build_failover_client(_make_env(ALCHEMY_API_KEY="k1"))
    alchemy = _FakeRpcClient("alchemy", fails=99)  # only get_balance is made to fail
    primary = _FakeRpcClient("primary")
    _swap_in_fake_clients(fc, {"alchemy": alchemy, "primary": primary})

    balance = await fc.get_balance("addr")  # account_lookup: alchemy fails -> switches to primary
    sigs = await fc.get_signatures_for_address("addr")  # tx_history: unaffected, still primary

    assert balance == "balance-from-primary"
    assert sigs == "sigs-from-primary"
    assert fc._names[fc._active["account_lookup"]] == "primary"
    assert fc._names[fc._active["tx_history"]] == "primary"
    # tx_history's primary was never touched by account_lookup's failover
    assert alchemy.calls == 1


async def test_raises_last_error_when_every_provider_in_route_fails():
    fc = _build_failover_client(_make_env(ALCHEMY_API_KEY="k1"), max_attempts=10)
    _swap_in_fake_clients(
        fc,
        {
            "alchemy": _FakeRpcClient("alchemy", fails=99),
            "primary": _FakeRpcClient("primary", fails=99),
            "drpc-public": _FakeRpcClient("drpc-public", fails=99),
            "ankr-public": _FakeRpcClient("ankr-public", fails=99),
        },
    )

    with pytest.raises(RuntimeError, match="unavailable"):
        await fc.get_balance("addr")


async def test_unmapped_method_routes_as_general_primary_first():
    fc = _build_failover_client(_make_env(ALCHEMY_API_KEY="k1"))

    class _Fake:
        async def some_future_rpc_method(self):
            return "ok"

    _swap_in_fake_clients(fc, {"primary": _Fake()})
    assert await fc.some_future_rpc_method() == "ok"


async def test_close_closes_every_underlying_client_even_if_one_raises():
    fc = _build_failover_client(_make_env(ALCHEMY_API_KEY="k1"))

    closed = []

    class _Closeable:
        def __init__(self, name, raise_on_close=False):
            self.name = name
            self.raise_on_close = raise_on_close

        async def close(self):
            if self.raise_on_close:
                raise RuntimeError("already closed")
            closed.append(self.name)

    _swap_in_fake_clients(
        fc,
        {
            "primary": _Closeable("primary", raise_on_close=True),
            "alchemy": _Closeable("alchemy"),
            "drpc-public": _Closeable("drpc-public"),
            "ankr-public": _Closeable("ankr-public"),
        },
    )

    await fc.close()  # must not raise even though "primary" raises internally
    assert sorted(closed) == ["alchemy", "ankr-public", "drpc-public"]


def test_create_connection_returns_plain_client_for_single_endpoint():
    from solana.rpc.async_api import AsyncClient
    from whale_alpha.integrations.solana_connection import create_connection

    connection = create_connection(_make_env(SOLANA_CLUSTER="devnet"))
    assert isinstance(connection, AsyncClient)


def test_create_connection_returns_failover_client_for_multiple_endpoints():
    from whale_alpha.integrations.solana_connection import _FailoverAsyncClient, create_connection

    connection = create_connection(_make_env(ALCHEMY_API_KEY="k1"))
    assert isinstance(connection, _FailoverAsyncClient)
