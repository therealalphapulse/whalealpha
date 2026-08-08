"""Candidate wallet sourcing + on-chain swap history — new feature, no TS
equivalent. This is the discovery engine's ingestion seam, same spirit as
integrations/helius_webhook.py: a thin, clearly-flagged adapter over a
third-party data source, with everything downstream (engines/discovery.py,
engines/discovery_metrics.py) provider-agnostic.

Nothing in this repo invents on-chain data — every function here performs a
real RPC/HTTP call. Three sourcing/history strategies are implemented,
chosen per-call based on configuration:

  1. Solana RPC only (always available, no API key required):
     `find_candidates_from_token_holders` — for a token that already produced
     a real Signal (i.e. multiple *tracked* whales just accumulated it), pull
     its other largest holders via `getTokenLargestAccounts`. Anyone already
     holding a meaningful position in a token our own tracked whales are
     buying is a reasonable candidate to evaluate.

  2. Jupiter Tokens API V2 (`JUPITER_API_KEY` or `PRICE_FEED_API_KEY` set):
     `find_candidates_from_trending_tokens` — pulls platform-wide
     trending/most-traded tokens (independent of anything already tracked),
     then reuses the same token-holders lookup as (1). This exists
     specifically to break the discovery engine's cold-start deadlock: with
     zero tracked wallets there are zero Signals, so (1) alone can never
     produce a first candidate — see engines/discovery.py's
     `discover_candidates` docstring for the full loop this closes.

  Both (1) and (2) can only observe *current holdings*, not history — plain
  RPC has no indexed "this wallet's trade history" call — so they give
  wallet age (`estimate_wallet_age_days`) and current activity, not
  PnL/ROI/win-rate. That requires:

  3. Helius Enhanced Transactions API (`HELIUS_API_KEY` set):
     `fetch_wallet_swap_history` — pages through a wallet's parsed
     transaction history and extracts SWAP events, giving the discovery
     engine the realized buy/sell pairs it needs for accurate ROI/win-rate/
     holding-period metrics. This is the same "Enhanced" product family
     integrations/helius_webhook.py already assumes for inbound events.

ASSUMPTION (flagged, same as helius_webhook.py): the exact Helius Enhanced
Transactions API and Jupiter Tokens API V2 request/response shapes below
match their documented endpoints as of this port
(`GET /v0/addresses/{address}/transactions` and
`GET /tokens/v2/{category}/{interval}` respectively). Verify against a live
response before depending on either in production — third-party API shapes
change without notice more often than on-chain program layouts do. If you
use a different wallet-history or trending-token provider (Birdeye, a
self-hosted indexer, etc.), swap the relevant function for a parser matching
that provider's payload; `WalletSwap` and `DiscoveredCandidate` downstream
are both provider-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from solana.rpc.async_api import AsyncClient

from whale_alpha.config import Env
from whale_alpha.integrations.price_feed import SOL_MINT
from whale_alpha.integrations.solana_connection import (
    get_token_largest_accounts,
    get_wallet_first_activity_slot,
    get_wallet_recent_transactions,
)
from whale_alpha.utils.http_retry import TTLCache, get_provider_client
from whale_alpha.utils.logger import child_logger

log = child_logger("walletDiscoverySource")

# Process-wide result cache for Helius wallet-history lookups — see
# fetch_wallet_swap_history. Module-level (not per-call) so every candidate
# in a batch shares the same cache regardless of how many concurrent tasks
# are fetching history at once. Concurrency bounding + circuit breaking for
# the underlying Helius HTTP calls themselves now lives in
# utils/http_retry.get_provider_client("helius_history", ...) — see
# _fetch_from_helius — rather than a hand-rolled semaphore here.
_history_cache: TTLCache[list["WalletSwap"]] | None = None
_history_negative_cache: TTLCache[bool] | None = None


def _get_history_cache(ttl_seconds: float) -> TTLCache[list["WalletSwap"]]:
    global _history_cache
    if _history_cache is None:
        _history_cache = TTLCache(ttl_seconds=ttl_seconds)
    return _history_cache


def _get_history_negative_cache(ttl_seconds: float) -> TTLCache[bool]:
    global _history_negative_cache
    if _history_negative_cache is None:
        _history_negative_cache = TTLCache(ttl_seconds=ttl_seconds)
    return _history_negative_cache


_history_stale_cache: TTLCache[list["WalletSwap"]] | None = None


def _get_history_stale_cache(ttl_seconds: float) -> TTLCache[list["WalletSwap"]]:
    """Long-TTL cache written on every successful fetch (from any source —
    primary or a fallback) and read only as Fallback 1 when the primary is
    unavailable — see fetch_wallet_swap_history's fallback chain. Separate
    from `_history_cache` (the short-TTL "skip a refetch this cycle" cache)
    because the two serve different purposes: that one exists to avoid
    duplicate work, this one exists to have *something* to fall back to."""
    global _history_stale_cache
    if _history_stale_cache is None:
        _history_stale_cache = TTLCache(ttl_seconds=ttl_seconds)
    return _history_stale_cache


@dataclass(frozen=True)
class WalletHistoryFetch:
    """Result of fetch_wallet_swap_history. `swaps` is None whenever no
    usable history could be produced this call; `transient` then tells the
    caller whether that's worth retrying (429/5xx/network error — see
    utils/http_retry.py) or permanent (no provider configured, or the
    provider gave a definitive 4xx like an invalid address). `cache_hit`
    feeds the discovery cycle's cache-hit-ratio metric.

    `source` records which layer of the PRIMARY -> stale cache -> RPC
    fallback -> retry queue chain actually produced `swaps` ("HELIUS",
    "CACHE_STALE", or "RPC_FALLBACK"). `partial` is True whenever `source`
    isn't "HELIUS" — a fallback result is real on-chain/cached data, never
    fabricated, but is lower-fidelity (a stale snapshot, or a cruder RPC-only
    reconstruction — see `_extract_swap_from_rpc_transaction`) and downstream
    scoring should discount confidence accordingly rather than treat it as
    equivalent to a fresh Helius fetch (see
    engines/discovery.py::evaluate_candidates' confidence adjustment,
    `env.DISCOVERY_HISTORY_FALLBACK_CONFIDENCE_MULTIPLIER`).
    """

    swaps: list[WalletSwap] | None
    transient: bool
    cache_hit: bool = False
    source: str = "HELIUS"
    partial: bool = False
    # --- NEW (Helius 429-pressure audit/fix) ---
    # Per-call observability flags consumed by engines/discovery.py to build
    # the per-cycle Helius/RPC-fallback metrics (helius_requests/helius_429/
    # helius_success/rpc_fallback/rpc_fallback_success — see
    # run_discovery_cycle's structured "DISCOVERY CYCLE COMPLETE" summary).
    # Both default False so every existing call site constructing this
    # dataclass (all keyword-only) is unaffected.
    helius_rate_limited: bool = False  # a 429 was observed on THIS call, whatever the eventual outcome
    rpc_fallback_attempted: bool = False  # FALLBACK 2 was invoked this call, whatever the outcome


@dataclass(frozen=True)
class DiscoveredCandidate:
    address: str
    source: str
    discovered_from_token_mint: str | None = None


@dataclass(frozen=True)
class WalletSwap:
    side: str  # "BUY" or "SELL"
    token_mint: str
    amount_usd: float
    timestamp: datetime


async def find_candidates_from_token_holders(
    connection: AsyncClient,
    token_mint: str,
    max_holders: int,
    *,
    min_interval_seconds: float = 0.12,
    max_retries: int = 3,
) -> list[DiscoveredCandidate]:
    """RPC-only candidate source: other large holders of a token our own
    tracked whales just accumulated (see engines/discovery.py — called with
    the token_mint of recently-generated Signals). Requires no API key.

    `min_interval_seconds`/`max_retries` pace the underlying RPC calls (see
    get_token_largest_accounts's docstring) — pass
    `env.DISCOVERY_RPC_MIN_INTERVAL_SECONDS`/`env.DISCOVERY_RPC_MAX_RETRIES`
    from the caller rather than relying on these defaults where an `env` is
    available, so it's actually configurable in production.
    """
    try:
        owners = await get_token_largest_accounts(
            connection,
            token_mint,
            limit=max_holders,
            min_interval_seconds=min_interval_seconds,
            max_retries=max_retries,
        )
    except Exception as err:  # noqa: BLE001 — one bad mint shouldn't stop the discovery cycle
        log.warning("Failed to fetch token largest accounts", mint=token_mint, err=str(err))
        return []

    return [
        DiscoveredCandidate(
            address=owner,
            source="token_holder_of_signaled_token",
            discovered_from_token_mint=token_mint,
        )
        for owner in owners
    ]


async def find_candidates_from_trending_tokens(
    client: httpx.AsyncClient,
    connection: AsyncClient,
    env: Env,
    *,
    max_tokens: int,
    max_holders_per_token: int,
) -> list[DiscoveredCandidate]:
    """Independent-of-tracked-wallets candidate source: pulls Jupiter's
    platform-wide trending/most-traded tokens (`JUPITER_TOKENS_API_BASE`,
    category/interval from `DISCOVERY_TRENDING_CATEGORY`/
    `DISCOVERY_TRENDING_INTERVAL`), then reuses
    `find_candidates_from_token_holders` for each. This is what lets the
    discovery engine find its first wallets with zero admin seeding — see
    the module docstring's strategy (2) and
    engines/discovery.discover_candidates.

    Returns [] (with a warning logged once, not per-cycle-spammed at debug
    level) if no API key is configured or the request fails — callers should
    treat that as "bootstrap source unavailable this cycle", not an error
    worth crashing the whole discovery cycle over.
    """
    api_key = env.JUPITER_API_KEY or env.PRICE_FEED_API_KEY
    if not api_key:
        log.warning(
            "Trending-token discovery source has no API key configured "
            "(JUPITER_API_KEY / PRICE_FEED_API_KEY) — skipping. Discovery can "
            "only source from holders of tokens already produced by your own "
            "tracked whales' Signals until this is set, which cannot bootstrap "
            "from zero tracked wallets."
        )
        return []

    url = f"{env.JUPITER_TOKENS_API_BASE}/{env.DISCOVERY_TRENDING_CATEGORY}/{env.DISCOVERY_TRENDING_INTERVAL}"
    result = await get_provider_client(
        "jupiter_trending",
        max_concurrency=env.DISCOVERY_PROVIDER_MAX_CONCURRENCY,
        failure_threshold=env.DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds=env.DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
    ).get(
        client,
        url,
        params={"limit": str(max_tokens)},
        headers={"x-api-key": api_key},
        max_retries=env.DISCOVERY_PROVIDER_MAX_RETRIES,
        base_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_BASE_SECONDS,
        max_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_MAX_SECONDS,
    )
    if result.response is None or result.response.status_code >= 400:
        # Transient (429/5xx, retries exhausted) or permanent failure either
        # way falls through to the Birdeye/DexScreener chain this cycle — see
        # _queue_trending_bootstrap_candidates — so no retry queue needed here.
        log.warning(
            "Jupiter Tokens API request failed",
            status=result.response.status_code if result.response else None,
            transient=result.transient,
        )
        return []
    try:
        tokens = result.response.json()
    except Exception as err:  # noqa: BLE001 — a provider hiccup shouldn't stop the discovery cycle
        log.warning("Jupiter Tokens API response unparseable", err=str(err))
        return []

    if not isinstance(tokens, list):
        return []

    token_mints = [t["id"] for t in tokens if isinstance(t, dict) and t.get("id")][:max_tokens]

    candidates: list[DiscoveredCandidate] = []
    for mint in token_mints:
        holders = await find_candidates_from_token_holders(
            connection,
            mint,
            max_holders_per_token,
            min_interval_seconds=env.DISCOVERY_RPC_MIN_INTERVAL_SECONDS,
            max_retries=env.DISCOVERY_RPC_MAX_RETRIES,
        )
        candidates.extend(
            DiscoveredCandidate(
                address=h.address, source="trending_token_holder", discovered_from_token_mint=mint
            )
            for h in holders
        )
    return candidates


async def fetch_wallet_swap_history(
    client: httpx.AsyncClient,
    env: Env,
    address: str,
    *,
    sol_price_usd: float,
    connection: AsyncClient | None = None,
    max_transactions: int = 100,
) -> WalletHistoryFetch:
    """Returns parsed SWAP events for `address`, most recent first, wrapped
    in a WalletHistoryFetch so callers can tell "no data, don't bother
    retrying" (`transient=False`) apart from "provider hiccup, worth
    retrying" (`transient=True`; see engines/discovery.py's retry-queue
    handling in evaluate_candidates). Callers must treat `swaps is None` as
    "insufficient data to score" rather than "wallet has zero trades" — see
    discovery_metrics.py.

    PROVIDER FALLBACK CHAIN (production fix — closes the single-point-of-
    failure on Helius): when the primary provider can't produce history this
    call, three fallbacks are tried in order before giving up:

      PRIMARY (Helius Enhanced Transactions, `_fetch_from_helius`)
        ↓ unavailable (no HELIUS_API_KEY, rate-limited, erroring, or down)
      FALLBACK 1 — existing cached history, even past its normal "fresh"
        TTL (`DISCOVERY_HISTORY_STALE_CACHE_TTL_SECONDS`, default 6h).
        Stale real data beats none.
        ↓ no cache entry
      FALLBACK 2 — reconstruct swaps directly from Solana RPC
        (`get_wallet_recent_transactions`, routed through the existing
        Helius RPC/DRPC/Alchemy/Ankr failover in solana_connection.py —
        see `_fetch_via_rpc_fallback`). Only attempted if
        `DISCOVERY_HISTORY_RPC_FALLBACK_ENABLED` and a `connection` was
        passed in.
        ↓ nothing reconstructable either
      FALLBACK 3 — retry queue. This function returns the primary's own
        `transient` flag unchanged; engines/discovery.py's
        `decide_history_fetch_outcome` is what actually queues the
        candidate for a later retry instead of rejecting it outright.

    Every fallback returns `partial=True` and a `source` other than
    "HELIUS" — this NEVER fabricates or estimates history: each layer
    either returns real cached/on-chain data or nothing at all. Downstream
    scoring (evaluate_candidates) discounts confidence for partial results
    rather than treating them as equivalent to a fresh Helius fetch.

    RATE LIMIT RESILIENCE (fixes a real production issue): previously a
    single 429 from Helius meant the candidate was rejected outright with
    NO_HISTORY_PROVIDER_OR_FETCH_FAILED, discarding a wallet that might be
    perfectly good — free-tier Helius rate limits are hit constantly under
    normal discovery load. Requests are now paced by a process-wide
    semaphore (`env.DISCOVERY_HISTORY_MAX_CONCURRENCY`), retried with
    exponential backoff + jitter honoring `Retry-After`
    (`env.DISCOVERY_HISTORY_MAX_RETRIES` attempts per call — see
    utils/http_retry.fetch_with_retry), and successful/negative results are
    cached in-process (`env.DISCOVERY_HISTORY_CACHE_TTL_SECONDS` /
    `env.DISCOVERY_HISTORY_NEGATIVE_CACHE_TTL_SECONDS`) so re-discovering the
    same address from multiple sources in one cycle — or across
    closely-spaced cycles — doesn't refetch it.
    """
    cache = _get_history_cache(env.DISCOVERY_HISTORY_CACHE_TTL_SECONDS)
    cached = cache.get(address)
    if cached is not None:
        return WalletHistoryFetch(swaps=cached, transient=False, cache_hit=True, source="HELIUS")

    negative_cache = _get_history_negative_cache(env.DISCOVERY_HISTORY_NEGATIVE_CACHE_TTL_SECONDS)
    if negative_cache.get(address):
        return WalletHistoryFetch(swaps=None, transient=False, cache_hit=True, source="HELIUS")

    primary = await _fetch_from_helius(client, env, address, sol_price_usd=sol_price_usd, max_transactions=max_transactions)
    if primary.swaps is not None:
        cache.set(address, primary.swaps)
        _get_history_stale_cache(env.DISCOVERY_HISTORY_STALE_CACHE_TTL_SECONDS).set(address, primary.swaps)
        return primary

    if primary.transient is False and env.HELIUS_API_KEY:
        # A definitive Helius failure for this specific address (bad
        # address, unparseable payload) — not "no key configured", which
        # isn't address-specific. Negative-cache it; the fallbacks below
        # still get a chance, but there's no point re-hitting Helius for
        # this address again soon.
        negative_cache.set(address, True)

    # PRIMARY unavailable — try the fallback chain.
    stale = _get_history_stale_cache(env.DISCOVERY_HISTORY_STALE_CACHE_TTL_SECONDS).get(address)
    if stale is not None:
        log.debug("Wallet history fallback: served from stale cache", address=address)
        return WalletHistoryFetch(
            swaps=stale,
            transient=False,
            source="CACHE_STALE",
            partial=True,
            helius_rate_limited=primary.helius_rate_limited,
        )

    if env.DISCOVERY_HISTORY_RPC_FALLBACK_ENABLED and connection is not None:
        rpc_swaps = await _fetch_via_rpc_fallback(connection, env, address, sol_price_usd=sol_price_usd)
        if rpc_swaps:
            _get_history_stale_cache(env.DISCOVERY_HISTORY_STALE_CACHE_TTL_SECONDS).set(address, rpc_swaps)
            log.debug("Wallet history fallback: reconstructed via RPC", address=address, swap_count=len(rpc_swaps))
            return WalletHistoryFetch(
                swaps=rpc_swaps,
                transient=False,
                source="RPC_FALLBACK",
                partial=True,
                helius_rate_limited=primary.helius_rate_limited,
                rpc_fallback_attempted=True,
            )
        # RPC fallback was attempted but reconstructed nothing usable
        # (no signatures, nothing classifiable as a swap, or the RPC calls
        # themselves failed) — still worth recording as an attempt for the
        # per-cycle rpc_fallback/rpc_fallback_success metrics, even though
        # the caller falls through to the retry queue below exactly as
        # before this fix.
        primary = WalletHistoryFetch(
            swaps=primary.swaps,
            transient=primary.transient,
            cache_hit=primary.cache_hit,
            source=primary.source,
            partial=primary.partial,
            helius_rate_limited=primary.helius_rate_limited,
            rpc_fallback_attempted=True,
        )

    # Fallback 3 (retry queue) is the caller's responsibility — return the
    # primary result's own transient/permanent classification unchanged.
    return primary


async def _fetch_from_helius(
    client: httpx.AsyncClient,
    env: Env,
    address: str,
    *,
    sol_price_usd: float,
    max_transactions: int,
) -> WalletHistoryFetch:
    """PRIMARY wallet-history provider — see fetch_wallet_swap_history's
    fallback-chain docstring for how a failure here is handled. No caching
    here (the wrapper owns both the fresh and stale caches, since fallback
    results need to populate the stale cache too).

    ASSUMPTION (judgment call, same spirit as auto_trading.py's documented
    portfolio-value approximation): each swap's USD size is
    `native_lamports / 1e9 * sol_price_usd` using the *current* SOL/USD
    price passed in by the caller, not the historical price at the time of
    that swap (Helius's enhanced payload doesn't include a USD value, and
    this repo has no historical price feed). This is exact for recent swaps
    and increasingly approximate for older ones — acceptable for a
    ROI/win-rate *ranking* signal, not for anything precision-sensitive.
    """
    if not env.HELIUS_API_KEY:
        return WalletHistoryFetch(swaps=None, transient=False, source="HELIUS")

    url = f"{env.HELIUS_API_BASE}/v0/addresses/{address}/transactions"
    params = {"api-key": env.HELIUS_API_KEY, "type": "SWAP", "limit": str(min(max_transactions, 100))}

    # --- Helius 429-pressure fix (production audit) ---
    # Previously this went through a hand-rolled module-level semaphore
    # (_get_helius_semaphore) with retry/backoff only — no circuit breaker,
    # unlike every OTHER discovery-source provider (Jupiter, Birdeye,
    # DexScreener, pump.fun, LaunchLab, Raydium, Meteora — all already route
    # through get_provider_client). Routing Helius through the same
    # ProviderClient closes that gap using EXISTING infrastructure rather
    # than inventing a new one: same semaphore-bounded concurrency as
    # before (still governed by DISCOVERY_HISTORY_MAX_CONCURRENCY), PLUS a
    # circuit breaker that opens after DISCOVERY_HISTORY_CIRCUIT_FAILURE_THRESHOLD
    # consecutive transient failures and fails fast (no network call at all)
    # for DISCOVERY_HISTORY_CIRCUIT_COOLDOWN_SECONDS. During a sustained 429
    # burst this is what actually stops hammering Helius — every candidate
    # after the threshold gets an immediate transient=True (routed to the
    # RETRY QUEUE exactly like a real 429 would be, see
    # engines/discovery.evaluate_candidates) instead of each one separately
    # burning through its own retry budget against a provider that's
    # already known to be rate-limiting. PLUS free per-provider metrics
    # (requests/successes/failures/rate_limited/retries/circuit_open) via
    # utils.http_retry.get_all_provider_metrics(), which feeds the new
    # per-cycle Helius metrics in the discovery summary log.
    provider = get_provider_client(
        "helius_history",
        max_concurrency=env.DISCOVERY_HISTORY_MAX_CONCURRENCY,
        failure_threshold=env.DISCOVERY_HISTORY_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds=env.DISCOVERY_HISTORY_CIRCUIT_COOLDOWN_SECONDS,
    )
    result = await provider.get(
        client,
        url,
        params=params,
        max_retries=env.DISCOVERY_HISTORY_MAX_RETRIES,
        base_backoff_seconds=env.DISCOVERY_HISTORY_RETRY_BASE_SECONDS,
        max_backoff_seconds=env.DISCOVERY_HISTORY_RETRY_MAX_SECONDS,
    )

    if result.circuit_open:
        log.debug("Helius circuit open, skipping request this call", address=address)
        return WalletHistoryFetch(swaps=None, transient=True, source="HELIUS", helius_rate_limited=True)

    if result.response is None:
        # Retries exhausted on a transient error (429/5xx/network) — do NOT
        # negative-cache; the whole point is this address is worth trying
        # again later (see the retry-queue handling in evaluate_candidates).
        log.debug("Helius wallet history unavailable after retries", address=address, transient=result.transient)
        return WalletHistoryFetch(
            swaps=None, transient=result.transient, source="HELIUS", helius_rate_limited=result.rate_limited
        )

    if result.response.status_code >= 400:
        log.debug("Helius wallet history request failed", address=address, status=result.response.status_code)
        return WalletHistoryFetch(swaps=None, transient=False, source="HELIUS", helius_rate_limited=result.rate_limited)

    try:
        transactions = result.response.json()
    except Exception as err:  # noqa: BLE001 — a malformed payload shouldn't stop the batch
        log.debug("Helius wallet history response unparseable", address=address, err=str(err))
        return WalletHistoryFetch(swaps=None, transient=False, source="HELIUS", helius_rate_limited=result.rate_limited)

    if not isinstance(transactions, list):
        return WalletHistoryFetch(swaps=None, transient=False, source="HELIUS", helius_rate_limited=result.rate_limited)

    swaps: list[WalletSwap] = []
    for tx in transactions:
        if not isinstance(tx, dict):
            continue
        swap = _extract_swap_for_wallet(cast("dict[str, Any]", tx), address, sol_price_usd)
        if swap is not None:
            swaps.append(swap)

    return WalletHistoryFetch(swaps=swaps, transient=False, source="HELIUS", helius_rate_limited=result.rate_limited)


async def _fetch_via_rpc_fallback(
    connection: AsyncClient,
    env: Env,
    address: str,
    *,
    sol_price_usd: float,
) -> list[WalletSwap]:
    """FALLBACK 2 in fetch_wallet_swap_history's chain — reconstructs a
    best-effort swap history directly from raw Solana RPC transactions
    (`get_wallet_recent_transactions`) when Helius Enhanced Transactions is
    unavailable. Deliberately conservative: only classifies a transaction as
    a swap when the wallet's own token balance AND native SOL balance both
    moved in a consistent direction (see `_extract_swap_from_rpc_transaction`)
    — anything ambiguous is skipped rather than guessed, so this never
    fabricates a trade that didn't happen.

    Cruder than the Helius parser (which uses Helius's own pre-computed
    `events.swap`): no DEX-program identification, no multi-hop-route
    awareness, and it can miscount a transaction that happens to move both
    balances for an unrelated reason (e.g. a token transfer bundled with an
    account-rent-reclaim in the same tx) as a swap. That's why every result
    from this path is flagged `partial=True`/`source="RPC_FALLBACK"` by the
    caller — a real signal, but a lower-confidence one.
    """
    try:
        raw_transactions = await get_wallet_recent_transactions(
            connection,
            address,
            max_signatures=env.DISCOVERY_HISTORY_RPC_FALLBACK_MAX_SIGNATURES,
            min_interval_seconds=env.DISCOVERY_RPC_MIN_INTERVAL_SECONDS,
            max_retries=env.DISCOVERY_RPC_MAX_RETRIES,
        )
    except Exception as err:  # noqa: BLE001 — fallback failing shouldn't stop the batch
        log.debug("RPC history fallback errored", address=address, err=str(err))
        return []

    swaps: list[WalletSwap] = []
    for tx in raw_transactions:
        swap = _extract_swap_from_rpc_transaction(tx, address, sol_price_usd)
        if swap is not None:
            swaps.append(swap)
    return swaps


def _extract_swap_for_wallet(
    transaction: dict[str, Any], wallet_address: str, sol_price_usd: float
) -> WalletSwap | None:
    """Classifies one Helius Enhanced transaction as a BUY or SELL from
    `wallet_address`'s perspective, using the same tokenTransfers shape
    integrations/helius_webhook.py already assumes. A swap is a BUY when the
    wallet's non-SOL token balance increased (received a non-SOL mint from
    someone other than itself) and a SELL when it decreased.

    USD sizing uses `events.swap.nativeInput/nativeOutput` (Helius's own
    computed native-SOL amount for the swap) — see fetch_wallet_swap_history's
    docstring for the current-price approximation. A swap without a resolvable
    native amount returns None rather than guessing — a swap we can't size
    shouldn't silently count as $0 towards position-sizing metrics.
    """
    timestamp = transaction.get("timestamp")
    if not timestamp:
        return None

    received = None
    sent = None
    for transfer in transaction.get("tokenTransfers") or []:
        mint = transfer.get("mint")
        if mint == SOL_MINT or not mint:
            continue
        if transfer.get("toUserAccount") == wallet_address:
            received = transfer
        elif transfer.get("fromUserAccount") == wallet_address:
            sent = transfer

    swap_event = ((transaction.get("events") or {}).get("swap")) or {}
    native_input = swap_event.get("nativeInput") or {}
    native_output = swap_event.get("nativeOutput") or {}

    if received is not None and sent is None:
        lamports = native_input.get("amount")
        if lamports is None:
            return None
        try:
            amount_usd = (float(lamports) / 1e9) * sol_price_usd
        except (TypeError, ValueError):
            return None
        return WalletSwap(
            side="BUY",
            token_mint=received["mint"],
            amount_usd=amount_usd,
            timestamp=datetime.fromtimestamp(int(timestamp), tz=UTC),
        )
    if sent is not None and received is None:
        lamports = native_output.get("amount")
        if lamports is None:
            return None
        try:
            amount_usd = (float(lamports) / 1e9) * sol_price_usd
        except (TypeError, ValueError):
            return None
        return WalletSwap(
            side="SELL",
            token_mint=sent["mint"],
            amount_usd=amount_usd,
            timestamp=datetime.fromtimestamp(int(timestamp), tz=UTC),
        )
    return None


def _extract_swap_from_rpc_transaction(
    transaction: dict[str, Any], wallet_address: str, sol_price_usd: float
) -> WalletSwap | None:
    """FALLBACK 2's parser — classifies one raw, jsonParsed Solana RPC
    transaction (from `get_wallet_recent_transactions`) as a BUY or SELL
    from `wallet_address`'s perspective, using `meta.preTokenBalances` /
    `postTokenBalances` (token side) and `meta.preBalances` / `postBalances`
    (native SOL side) — the same information Helius's own enhanced parser is
    ultimately built on top of, just without Helius's DEX-program
    identification or multi-hop-route handling.

    ASSUMPTION (flagged, same convention as `_extract_swap_for_wallet` and
    the rest of this module): direction comes from the wallet's own non-SOL
    token balance delta; size is approximated from the *absolute* native SOL
    balance delta at `wallet_address`'s own account index (which nets out
    the transaction fee automatically, since the fee payer's SOL balance
    already reflects it). A transaction is only classified as a swap when
    the wallet has both a non-zero token delta AND a non-zero SOL delta in
    a consistent direction — anything else (a plain SPL transfer, a account
    close/rent-reclaim, an NFT mint, etc.) returns None rather than guessing.
    Verify against live RPC responses before trusting this at higher
    confidence than `partial=True` already implies — see
    `_fetch_via_rpc_fallback`'s docstring.
    """
    timestamp = transaction.get("blockTime")
    if not timestamp:
        return None

    message = ((transaction.get("transaction") or {}).get("message")) or {}
    account_keys = message.get("accountKeys") or []

    def _key(entry: Any) -> str | None:
        if isinstance(entry, dict):
            return entry.get("pubkey")
        return entry if isinstance(entry, str) else None

    keys = [_key(entry) for entry in account_keys]
    if wallet_address not in keys:
        return None
    wallet_index = keys.index(wallet_address)

    meta = transaction.get("meta") or {}
    if meta.get("err") is not None:
        return None  # failed transaction — no actual swap happened

    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    if wallet_index >= len(pre_balances) or wallet_index >= len(post_balances):
        return None
    try:
        sol_delta_lamports = int(post_balances[wallet_index]) - int(pre_balances[wallet_index])
    except (TypeError, ValueError):
        return None
    if sol_delta_lamports == 0:
        return None

    def _ui_amount(entry: dict[str, Any] | None) -> float:
        if entry is None:
            return 0.0
        try:
            return float((entry.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    pre_token_by_index = {
        b.get("accountIndex"): b
        for b in (meta.get("preTokenBalances") or [])
        if isinstance(b, dict) and b.get("owner") == wallet_address
    }
    post_token_by_index = {
        b.get("accountIndex"): b
        for b in (meta.get("postTokenBalances") or [])
        if isinstance(b, dict) and b.get("owner") == wallet_address
    }

    token_mint: str | None = None
    token_delta = 0.0
    for account_index, post_entry in post_token_by_index.items():
        delta = _ui_amount(post_entry) - _ui_amount(pre_token_by_index.get(account_index))
        mint = post_entry.get("mint")
        if delta != 0 and mint and mint != SOL_MINT:
            token_mint, token_delta = mint, delta
            break
    if token_mint is None:
        # A token account that existed pre-tx and vanished post-tx (fully
        # sold + closed) never appears in postTokenBalances at all.
        for account_index, pre_entry in pre_token_by_index.items():
            if account_index in post_token_by_index:
                continue
            pre_amount = _ui_amount(pre_entry)
            mint = pre_entry.get("mint")
            if pre_amount > 0 and mint and mint != SOL_MINT:
                token_mint, token_delta = mint, -pre_amount
                break

    if token_mint is None or token_delta == 0:
        return None
    # Direction must agree: received tokens + spent SOL = BUY; sent tokens +
    # received SOL = SELL. Anything else isn't a clean swap — skip it.
    if token_delta > 0 and sol_delta_lamports >= 0:
        return None
    if token_delta < 0 and sol_delta_lamports <= 0:
        return None

    amount_usd = (abs(sol_delta_lamports) / 1e9) * sol_price_usd
    if amount_usd <= 0:
        return None

    return WalletSwap(
        side="BUY" if token_delta > 0 else "SELL",
        token_mint=token_mint,
        amount_usd=amount_usd,
        timestamp=datetime.fromtimestamp(int(timestamp), tz=UTC),
    )


async def estimate_wallet_age_days(connection: AsyncClient, address: str) -> int | None:
    """Best-effort wallet age in days from its oldest RPC-visible signature.
    See get_wallet_first_activity_slot's docstring for the under-counting
    caveat on very old wallets.
    """
    slot = await get_wallet_first_activity_slot(connection, address)
    if slot is None:
        return None
    try:
        block_time = await connection.get_block_time(slot)
    except Exception:  # noqa: BLE001 — best-effort; missing block time just means "unknown age"
        return None
    if block_time.value is None:
        return None
    first_seen = datetime.fromtimestamp(block_time.value, tz=UTC)
    return max(0, (datetime.now(UTC) - first_seen).days)
