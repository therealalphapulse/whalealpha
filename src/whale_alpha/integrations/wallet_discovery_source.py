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
)
from whale_alpha.utils.logger import child_logger

log = child_logger("walletDiscoverySource")


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
    try:
        res = await client.get(
            url,
            params={"limit": str(max_tokens)},
            headers={"x-api-key": api_key},
            timeout=15.0,
        )
        if res.status_code >= 400:
            log.warning("Jupiter Tokens API request failed", status=res.status_code, url=url)
            return []
        tokens = res.json()
    except Exception as err:  # noqa: BLE001 — a provider hiccup shouldn't stop the discovery cycle
        log.warning("Jupiter Tokens API request errored", err=str(err))
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
    max_transactions: int = 100,
) -> list[WalletSwap] | None:
    """Returns parsed SWAP events for `address`, most recent first, or None if
    no history provider is configured (HELIUS_API_KEY unset) or the request
    fails. Callers must treat None as "insufficient data to score" rather
    than "wallet has zero trades" — see discovery_metrics.py.

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
        return None

    url = f"{env.HELIUS_API_BASE}/v0/addresses/{address}/transactions"
    params = {"api-key": env.HELIUS_API_KEY, "type": "SWAP", "limit": str(min(max_transactions, 100))}

    try:
        res = await client.get(url, params=params, timeout=15.0)
        if res.status_code >= 400:
            log.debug("Helius wallet history request failed", address=address, status=res.status_code)
            return None
        transactions = res.json()
    except Exception as err:  # noqa: BLE001 — a single wallet's history failing shouldn't stop the batch
        log.debug("Helius wallet history request errored", address=address, err=str(err))
        return None

    if not isinstance(transactions, list):
        return None

    swaps: list[WalletSwap] = []
    for tx in transactions:
        if not isinstance(tx, dict):
            continue
        swap = _extract_swap_for_wallet(cast("dict[str, Any]", tx), address, sol_price_usd)
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
