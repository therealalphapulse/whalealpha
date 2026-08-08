"""Unit tests for integrations/wallet_discovery_source.py's RPC-fallback
swap parser (`_extract_swap_from_rpc_transaction`) — pure dict-in,
dataclass-out, no DB/network. See scripts/validation/validate_rpc_swap_parser.py
for a dependency-free version of these same scenarios that can run without
`pip install -e .` (useful in an offline sandbox).
"""

from __future__ import annotations

from whale_alpha.integrations.wallet_discovery_source import (
    WalletHistoryFetch,
    _extract_swap_from_rpc_transaction,
)

WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
OTHER = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_PRICE = 150.0


def _make_tx(*, sol_delta_lamports, pre_token, post_token, err=None, block_time=1_725_000_000):
    return {
        "blockTime": block_time,
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}, {"pubkey": OTHER}]}},
        "meta": {
            "err": err,
            "preBalances": [5_000_000_000, 1_000_000_000],
            "postBalances": [5_000_000_000 + sol_delta_lamports, 1_000_000_000],
            "preTokenBalances": pre_token,
            "postTokenBalances": post_token,
        },
    }


def test_buy_reconstructed_from_raw_rpc_transaction():
    tx = _make_tx(
        sol_delta_lamports=-2_000_000_000,
        pre_token=[],
        post_token=[{"accountIndex": 2, "owner": WALLET, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": 300.0}}],
    )
    swap = _extract_swap_from_rpc_transaction(tx, WALLET, SOL_PRICE)
    assert swap is not None
    assert swap.side == "BUY"
    assert swap.token_mint == USDC_MINT
    assert abs(swap.amount_usd - (2.0 * SOL_PRICE)) < 0.01


def test_sell_reconstructed_when_token_account_closes():
    """A fully-sold token account vanishes from postTokenBalances entirely
    rather than appearing with a zero balance — must still be detected."""
    tx = _make_tx(
        sol_delta_lamports=1_500_000_000,
        pre_token=[{"accountIndex": 2, "owner": WALLET, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": 300.0}}],
        post_token=[],
    )
    swap = _extract_swap_from_rpc_transaction(tx, WALLET, SOL_PRICE)
    assert swap is not None
    assert swap.side == "SELL"
    assert swap.token_mint == USDC_MINT
    assert abs(swap.amount_usd - (1.5 * SOL_PRICE)) < 0.01


def test_failed_transaction_is_never_counted_as_a_swap():
    """Never fabricate: a transaction with meta.err set didn't actually
    execute, regardless of what its balance deltas would suggest."""
    tx = _make_tx(
        sol_delta_lamports=-2_000_000_000,
        pre_token=[],
        post_token=[{"accountIndex": 2, "owner": WALLET, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": 300.0}}],
        err={"InstructionError": [0, "Custom"]},
    )
    assert _extract_swap_from_rpc_transaction(tx, WALLET, SOL_PRICE) is None


def test_plain_token_transfer_without_sol_delta_is_not_a_swap():
    """Token balance moved but no corresponding SOL-side delta — a plain
    transfer, not a swap. Must not be fabricated as one."""
    tx = _make_tx(
        sol_delta_lamports=0,
        pre_token=[],
        post_token=[{"accountIndex": 2, "owner": WALLET, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": 50.0}}],
    )
    assert _extract_swap_from_rpc_transaction(tx, WALLET, SOL_PRICE) is None


def test_wallet_absent_from_transaction_returns_none_not_a_crash():
    tx = {
        "blockTime": 1_725_000_000,
        "transaction": {"message": {"accountKeys": [{"pubkey": OTHER}]}},
        "meta": {"err": None, "preBalances": [1], "postBalances": [1], "preTokenBalances": [], "postTokenBalances": []},
    }
    assert _extract_swap_from_rpc_transaction(tx, WALLET, SOL_PRICE) is None


def test_missing_block_time_returns_none():
    tx = _make_tx(sol_delta_lamports=-1, pre_token=[], post_token=[])
    tx["blockTime"] = None
    assert _extract_swap_from_rpc_transaction(tx, WALLET, SOL_PRICE) is None


def test_wallet_history_fetch_partial_flag_matches_source():
    primary = WalletHistoryFetch(swaps=None, transient=True, source="HELIUS")
    stale = WalletHistoryFetch(swaps=[], transient=False, source="CACHE_STALE", partial=True)
    rpc_fallback = WalletHistoryFetch(swaps=[], transient=False, source="RPC_FALLBACK", partial=True)

    assert primary.partial is False
    assert stale.partial is True
    assert rpc_fallback.partial is True


# --------------------------------------------------------------------------
# fetch_wallet_swap_history — full PRIMARY -> stale cache -> RPC fallback ->
# retry queue orchestration. Uses a stub Env (a plain namespace — this repo's
# real Env is a pydantic-settings class with no test fixture of its own) and
# monkeypatches the module's HTTP/RPC call points directly, mirroring
# scripts/validation/validate_fallback_chain.py's dependency-free version of
# these same scenarios.
# --------------------------------------------------------------------------

from unittest.mock import AsyncMock, patch

import pytest

from whale_alpha.integrations import wallet_discovery_source as wds
from whale_alpha.utils import http_retry


class _FallbackTestEnv:
    HELIUS_API_KEY = "test-key"
    HELIUS_API_BASE = "https://api.helius.xyz"
    DISCOVERY_HISTORY_CACHE_TTL_SECONDS = 300
    DISCOVERY_HISTORY_NEGATIVE_CACHE_TTL_SECONDS = 3600
    DISCOVERY_HISTORY_MAX_CONCURRENCY = 5
    DISCOVERY_HISTORY_MAX_RETRIES = 0
    DISCOVERY_HISTORY_RETRY_BASE_SECONDS = 0.01
    DISCOVERY_HISTORY_RETRY_MAX_SECONDS = 0.02
    DISCOVERY_HISTORY_STALE_CACHE_TTL_SECONDS = 21600
    DISCOVERY_HISTORY_RPC_FALLBACK_ENABLED = True
    DISCOVERY_HISTORY_RPC_FALLBACK_MAX_SIGNATURES = 10
    DISCOVERY_RPC_MIN_INTERVAL_SECONDS = 0.0
    DISCOVERY_RPC_MAX_RETRIES = 0
    # High threshold/short cooldown so these tests' repeated 429s never trip
    # the Helius circuit breaker mid-test and change which code path (real
    # HTTP call vs. circuit-open skip) produces the (identical, either way)
    # transient=True outcome the assertions below check for.
    DISCOVERY_HISTORY_CIRCUIT_FAILURE_THRESHOLD = 1000
    DISCOVERY_HISTORY_CIRCUIT_COOLDOWN_SECONDS = 0.01


class _Always429:
    async def request(self, method, url, timeout=None, **kwargs):
        class _R:
            status_code = 429
            headers = {}
        return _R()


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """These caches are process-global module state (see http_retry.TTLCache
    usage in wallet_discovery_source.py) — reset between tests so one test's
    cached/negative-cached address doesn't leak into the next. Also resets
    the shared "helius_history" ProviderClient (utils/http_retry.py) so one
    test's simulated 429s never leave the circuit breaker open/tripped for
    the next test.
    """
    wds._history_cache = None
    wds._history_negative_cache = None
    wds._history_stale_cache = None
    http_retry._provider_clients.pop("helius_history", None)
    yield
    wds._history_cache = None
    wds._history_negative_cache = None
    wds._history_stale_cache = None
    http_retry._provider_clients.pop("helius_history", None)


@pytest.mark.asyncio
async def test_transient_helius_failure_with_no_fallback_available_stays_transient():
    env = _FallbackTestEnv()
    env.DISCOVERY_HISTORY_RPC_FALLBACK_ENABLED = False
    result = await wds.fetch_wallet_swap_history(
        _Always429(), env, "WALLET_NO_FALLBACK", sol_price_usd=150.0, connection=None
    )
    assert result.swaps is None
    assert result.transient is True  # eligible for engines/discovery.py's retry queue


@pytest.mark.asyncio
async def test_rpc_fallback_used_when_helius_unavailable():
    env = _FallbackTestEnv()

    async def fake_rpc_transactions(connection, address, **kwargs):
        return [
            {
                "blockTime": 1_725_000_000,
                "transaction": {"message": {"accountKeys": [{"pubkey": address}, {"pubkey": "OTHER"}]}},
                "meta": {
                    "err": None,
                    "preBalances": [5_000_000_000, 0],
                    "postBalances": [3_000_000_000, 0],
                    "preTokenBalances": [],
                    "postTokenBalances": [
                        {"accountIndex": 2, "owner": address, "mint": "TOKEN_MINT", "uiTokenAmount": {"uiAmount": 400.0}}
                    ],
                },
            }
        ]

    with patch.object(wds, "get_wallet_recent_transactions", AsyncMock(side_effect=fake_rpc_transactions)):
        result = await wds.fetch_wallet_swap_history(
            _Always429(), env, "WALLET_RPC_FALLBACK", sol_price_usd=150.0, connection=object()
        )

    assert result.swaps is not None and len(result.swaps) == 1
    assert result.source == "RPC_FALLBACK"
    assert result.partial is True
    assert result.transient is False  # a usable (if partial) result — not queued for retry


class _CountingAlways429:
    """Same as _Always429 but counts real HTTP calls made — used to prove
    the Helius circuit breaker actually stops calling out once open, rather
    than just returning the same transient=True outcome for a different
    reason (production audit: Helius 429-pressure fix).
    """

    def __init__(self):
        self.call_count = 0

    async def request(self, method, url, timeout=None, **kwargs):
        self.call_count += 1

        class _R:
            status_code = 429
            headers = {}

        return _R()


@pytest.mark.asyncio
async def test_helius_circuit_breaker_opens_after_consecutive_failures_and_stops_calling_out():
    env = _FallbackTestEnv()
    env.DISCOVERY_HISTORY_RPC_FALLBACK_ENABLED = False
    env.DISCOVERY_HISTORY_CIRCUIT_FAILURE_THRESHOLD = 3
    env.DISCOVERY_HISTORY_CIRCUIT_COOLDOWN_SECONDS = 60.0  # long enough not to reset mid-test
    client = _CountingAlways429()

    for _ in range(3):
        result = await wds.fetch_wallet_swap_history(
            client, env, "WALLET_CIRCUIT_BREAKER", sol_price_usd=150.0, connection=None
        )
        assert result.transient is True

    calls_before_open = client.call_count
    assert calls_before_open == 3  # one real HTTP attempt per failure up to the threshold

    # One more call, past the threshold — the breaker should now be open,
    # so this must NOT make a real HTTP request, but the caller-visible
    # outcome (transient=True, eligible for the retry queue) is identical.
    result = await wds.fetch_wallet_swap_history(
        client, env, "WALLET_CIRCUIT_BREAKER", sol_price_usd=150.0, connection=None
    )
    assert result.transient is True
    assert result.helius_rate_limited is True
    assert client.call_count == calls_before_open  # no new HTTP call made — this is the actual fix


@pytest.mark.asyncio
async def test_helius_429_sets_the_rate_limited_flag_used_for_discovery_metrics():
    env = _FallbackTestEnv()
    env.DISCOVERY_HISTORY_RPC_FALLBACK_ENABLED = False
    result = await wds.fetch_wallet_swap_history(
        _Always429(), env, "WALLET_429_FLAG", sol_price_usd=150.0, connection=None
    )
    assert result.helius_rate_limited is True


@pytest.mark.asyncio
async def test_rpc_fallback_still_works_after_the_helius_circuit_breaker_opens():
    """Proves the circuit breaker only short-circuits the Helius HTTP call
    itself — FALLBACK 2 (RPC reconstruction) must keep working exactly as
    before, satisfying the "keep RPC fallback intact" requirement.
    """
    env = _FallbackTestEnv()
    env.DISCOVERY_HISTORY_CIRCUIT_FAILURE_THRESHOLD = 2
    env.DISCOVERY_HISTORY_CIRCUIT_COOLDOWN_SECONDS = 60.0
    client = _CountingAlways429()

    async def fake_rpc_transactions(connection, address, **kwargs):
        return [
            {
                "blockTime": 1_725_000_000,
                "transaction": {"message": {"accountKeys": [{"pubkey": address}]}},
                "meta": {
                    "err": None,
                    "preBalances": [5_000_000_000],
                    "postBalances": [3_000_000_000],
                    "preTokenBalances": [],
                    "postTokenBalances": [
                        {"accountIndex": 0, "owner": address, "mint": "TOKEN_MINT", "uiTokenAmount": {"uiAmount": 400.0}}
                    ],
                },
            }
        ]

    with patch.object(wds, "get_wallet_recent_transactions", AsyncMock(side_effect=fake_rpc_transactions)):
        # Trip the breaker first.
        for _ in range(2):
            await wds.fetch_wallet_swap_history(
                client, env, "WALLET_BREAKER_THEN_FALLBACK", sol_price_usd=150.0, connection=object()
            )
        assert client.call_count == 2

        # Now the breaker is open — RPC fallback must still produce a
        # usable result, exactly like a live 429 would.
        result = await wds.fetch_wallet_swap_history(
            client, env, "WALLET_BREAKER_THEN_FALLBACK", sol_price_usd=150.0, connection=object()
        )

    assert client.call_count == 2  # confirms the breaker really was open (no 3rd HTTP attempt)
    # First call already reconstructed history via RPC and stashed it in the
    # stale-result cache (see test_stale_cache_is_checked_before_rpc_fallback_is_invoked_again),
    # so this 3rd call is served from there rather than invoking RPC again —
    # either way, a usable (non-empty, non-transient) result reached the
    # caller despite the Helius circuit breaker being open, which is the
    # property this test exists to prove.
    assert result.source in ("RPC_FALLBACK", "CACHE_STALE")
    assert result.swaps is not None and len(result.swaps) == 1
    assert result.transient is False


@pytest.mark.asyncio
async def test_rpc_fallback_attempted_flag_reflects_an_attempt_even_when_it_yields_nothing():
    env = _FallbackTestEnv()

    async def fake_rpc_transactions_no_swaps(connection, address, **kwargs):
        return []  # RPC fallback attempted, but nothing classifiable as a swap

    with patch.object(wds, "get_wallet_recent_transactions", AsyncMock(side_effect=fake_rpc_transactions_no_swaps)):
        result = await wds.fetch_wallet_swap_history(
            _Always429(), env, "WALLET_FALLBACK_EMPTY", sol_price_usd=150.0, connection=object()
        )

    assert result.rpc_fallback_attempted is True
    assert result.source == "HELIUS"  # fell through to the retry-queue classification, not a "success" source
    assert result.transient is True


@pytest.mark.asyncio
async def test_successful_helius_fetch_does_not_set_rate_limited_or_fallback_flags():
    env = _FallbackTestEnv()

    class _Always200:
        async def request(self, method, url, timeout=None, **kwargs):
            class _R:
                status_code = 200
                headers = {}

                def json(self):
                    return []

            return _R()

    result = await wds.fetch_wallet_swap_history(
        _Always200(), env, "WALLET_HAPPY_PATH", sol_price_usd=150.0, connection=None
    )
    assert result.source == "HELIUS"
    assert result.swaps == []
    assert result.helius_rate_limited is False
    assert result.rpc_fallback_attempted is False
    env = _FallbackTestEnv()

    call_count = {"n": 0}

    async def fake_rpc_transactions(connection, address, **kwargs):
        call_count["n"] += 1
        return [
            {
                "blockTime": 1_725_000_000,
                "transaction": {"message": {"accountKeys": [{"pubkey": address}]}},
                "meta": {
                    "err": None,
                    "preBalances": [5_000_000_000],
                    "postBalances": [3_000_000_000],
                    "preTokenBalances": [],
                    "postTokenBalances": [
                        {"accountIndex": 0, "owner": address, "mint": "TOKEN_MINT", "uiTokenAmount": {"uiAmount": 400.0}}
                    ],
                },
            }
        ]

    with patch.object(wds, "get_wallet_recent_transactions", AsyncMock(side_effect=fake_rpc_transactions)):
        first = await wds.fetch_wallet_swap_history(
            _Always429(), env, "WALLET_STALE_CACHE", sol_price_usd=150.0, connection=object()
        )
        assert first.source == "RPC_FALLBACK"
        assert call_count["n"] == 1

        second = await wds.fetch_wallet_swap_history(
            _Always429(), env, "WALLET_STALE_CACHE", sol_price_usd=150.0, connection=object()
        )
        assert second.source == "CACHE_STALE"
        assert second.partial is True
        assert call_count["n"] == 1  # RPC fallback not invoked a second time
