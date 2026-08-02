"""Candidate wallet sourcing + on-chain swap history — new feature, no TS
equivalent. This is the discovery engine's ingestion seam, same spirit as
integrations/helius_webhook.py: a thin, clearly-flagged adapter over a
third-party data source, with everything downstream (engines/discovery.py,
engines/discovery_metrics.py) provider-agnostic.

Nothing in this repo invents on-chain data — every function here performs a
real RPC/HTTP call. Two sourcing/history strategies are implemented, chosen
per-call based on configuration:

  1. Solana RPC only (always available, no API key required):
     `find_candidates_from_token_holders` — for a token that already produced
     a real Signal (i.e. multiple *tracked* whales just accumulated it), pull
     its other largest holders via `getTokenLargestAccounts`. Anyone already
     holding a meaningful position in a token our own tracked whales are
     buying is a reasonable candidate to evaluate. This alone cannot compute
     realized PnL/ROI history (plain RPC has no indexed "this wallet's trade
     history" call), only wallet age (`get_wallet_first_activity_slot`) and
     current activity — see discovery_metrics.py for how a candidate with
     only partial data is scored.

  2. Helius Enhanced Transactions API (`HELIUS_API_KEY` set):
     `fetch_wallet_swap_history` — pages through a wallet's parsed
     transaction history and extracts SWAP events, giving the discovery
     engine the realized buy/sell pairs it needs for accurate ROI/win-rate/
     holding-period metrics. This is the same "Enhanced" product family
     integrations/helius_webhook.py already assumes for inbound events.

ASSUMPTION (flagged, same as helius_webhook.py): the exact Helius Enhanced
Transactions API request/response shape below matches their documented
`GET /v0/addresses/{address}/transactions` endpoint as of this port. Verify
against a live response before depending on it in production — third-party
API shapes change without notice more often than on-chain program layouts do.
If you use a different wallet-history provider (Birdeye, a self-hosted
indexer, etc.), swap `fetch_wallet_swap_history` for a parser matching that
provider's payload; `WalletSwap` downstream is provider-agnostic.
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
) -> list[DiscoveredCandidate]:
    """RPC-only candidate source: other large holders of a token our own
    tracked whales just accumulated (see engines/discovery.py — called with
    the token_mint of recently-generated Signals). Requires no API key.
    """
    try:
        owners = await get_token_largest_accounts(connection, token_mint, limit=max_holders)
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
