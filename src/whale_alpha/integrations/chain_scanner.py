"""Blockchain-first wallet discovery — Phase 1 refactor, new module.

This replaces API-first wallet discovery (pump.fun/LaunchLab/Raydium/
Meteora "recent launches" HTTP endpoints, the Jupiter/Birdeye/DexScreener
trending-token fallback chain — see integrations/free_market_sources.py and
integrations/wallet_discovery_source.find_candidates_from_trending_tokens)
with the Solana blockchain itself as the PRIMARY source of new candidate
wallets: the chain, not a third-party market-data API, is now what decides
which addresses get evaluated for promotion. Those HTTP-API integrations are
left completely intact for later re-use as *enrichment* (Phase 2+) — this
module just stops the discovery cycle from calling them to source new
wallets (see engines/discovery.discover_candidates and
config.DISCOVERY_API_SOURCES_ENABLED).

How it works
------------
Every discovery cycle, `scan_new_blocks`:

  1. Reads the last fully-processed slot from Postgres
     (`db.models.DiscoveryScanProgress`) — or, on the very first run,
     starts `DISCOVERY_BLOCK_SCAN_INITIAL_LOOKBACK_SLOTS` behind the current
     chain tip rather than genesis.
  2. Fetches a small, bounded batch of the *next* slots
     (`DISCOVERY_BLOCK_SCAN_BATCH_SIZE`, capped further by
     `DISCOVERY_BLOCK_SCAN_MAX_CATCHUP_SLOTS` so a long-stopped process
     doesn't try to replay its entire backlog in one cycle — it just closes
     the gap gradually, cycle over cycle).
  3. Fetches each slot's full block (`getBlock`, jsonParsed, version 0)
     concurrently, bounded by an `asyncio.Semaphore`
     (`DISCOVERY_BLOCK_SCAN_CONCURRENCY`).
  4. For every transaction that touches one of `SWAP_PROGRAM_IDS` (Jupiter,
     Raydium AMM v4/CLMM/CPMM, Raydium LaunchLab, Pump.fun bonding-curve,
     Pump.fun's PumpSwap migration AMM — see that dict's docstring), takes
     the transaction's fee payer (accountKeys[0], the wallet that signed and
     paid for it — i.e. the trader) as a discovered candidate wallet.
  5. Deduplicates within the batch and against everything already known
     (existing WhaleWallet/WalletCandidate rows, passed in by the caller —
     see engines/discovery.discover_candidates), then persists the new
     last-processed slot.

RPC calls go through the same retry/backoff/circuit-breaker/metrics layer
every other discovery-source provider uses (utils/http_retry.py) via plain
JSON-RPC POST requests over httpx — NOT the solana-py `AsyncClient` used
elsewhere in this repo — specifically so `Retry-After` response headers are
honored (solana-py's client does not surface response headers to callers).
`get_provider_client("solana_block_scan", ...)` gives this its own
semaphore + circuit breaker + metrics bucket, so a rate-limited block
scanner never starves (or is starved by) other RPC-bound discovery work.

Never scans the whole chain: `getBlock` is only ever called for a small,
explicitly bounded batch of slots per cycle, and progress is persisted after
every batch so a restart resumes from exactly where the engine left off
instead of re-scanning or silently skipping slots.

ASSUMPTION (flagged, same convention as integrations/free_market_sources.py
and integrations/wallet_discovery_source.py): the program IDs in
SWAP_PROGRAM_IDS are best-effort, verified against public documentation as
of this port, and are NOT guaranteed to stay current — DEX programs are
occasionally redeployed/upgraded/superseded. `DISCOVERY_BLOCK_SCAN_EXTRA_PROGRAM_IDS`
lets ops extend (never replace) this set without a code change.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from whale_alpha.config import Env
from whale_alpha.db.models import DiscoveryScanProgress
from whale_alpha.integrations.solana_connection import is_valid_solana_address
from whale_alpha.integrations.wallet_discovery_source import DiscoveredCandidate
from whale_alpha.utils.http_retry import TTLCache, get_provider_client
from whale_alpha.utils.logger import child_logger

log = child_logger("chainScanner")

# Solana program IDs treated as "this transaction is a swap/migration, its
# fee payer is a trader wallet worth evaluating". Verified against public
# documentation as of this port (see this module's docstring) — extend via
# DISCOVERY_BLOCK_SCAN_EXTRA_PROGRAM_IDS rather than editing this dict for
# a one-off addition.
SWAP_PROGRAM_IDS: dict[str, str] = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter_swap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium_amm_v4_swap",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium_clmm_swap",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium_cpmm_swap",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "raydium_launchlab_trade",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pumpfun_trade",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap_migration_trade",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora_dlmm_swap",
}

_DEFAULT_CLUSTER_RPC_METHOD = "getBlock"


@dataclass(frozen=True)
class ScanResult:
    candidates: list[DiscoveredCandidate]
    slots_scanned: int
    blocks_found: int
    from_slot: int | None
    to_slot: int | None


_tip_cache: TTLCache[int] | None = None


def _get_tip_cache(ttl_seconds: float) -> TTLCache[int]:
    global _tip_cache
    if _tip_cache is None or _tip_cache._ttl != ttl_seconds:  # noqa: SLF001 — re-key on config change
        _tip_cache = TTLCache(ttl_seconds=ttl_seconds, max_entries=4)
    return _tip_cache


def _extra_program_ids(env: Env) -> set[str]:
    return {p.strip() for p in env.DISCOVERY_BLOCK_SCAN_EXTRA_PROGRAM_IDS.split(",") if p.strip()}


async def _rpc_call(
    client: httpx.AsyncClient,
    env: Env,
    method: str,
    params: list[Any],
) -> dict[str, Any] | None:
    """One JSON-RPC call to SOLANA_RPC_URL, through the shared retry/
    backoff/circuit-breaker/Retry-After/metrics layer (see module
    docstring). Returns the `result` field on success, or None on any
    failure (network, non-2xx after retries, malformed JSON, or a JSON-RPC
    `error` object) — callers treat None as "skip this slot/call, try again
    next cycle", never as a reason to raise and abort the whole batch.
    """
    provider = get_provider_client(
        "solana_block_scan",
        max_concurrency=env.DISCOVERY_BLOCK_SCAN_CONCURRENCY,
        failure_threshold=env.DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds=env.DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
    )
    result = await provider.post(
        client,
        env.SOLANA_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Content-Type": "application/json"},
        max_retries=env.DISCOVERY_BLOCK_SCAN_MAX_RETRIES,
        base_backoff_seconds=env.DISCOVERY_BLOCK_SCAN_RETRY_BASE_SECONDS,
        max_backoff_seconds=env.DISCOVERY_BLOCK_SCAN_RETRY_MAX_SECONDS,
    )
    if result.response is None or result.response.status_code >= 400:
        log.debug(
            "chain_scanner RPC call failed",
            method=method,
            status=result.response.status_code if result.response else None,
            transient=result.transient,
            circuit_open=result.circuit_open,
        )
        return None
    try:
        payload = result.response.json()
    except Exception as err:  # noqa: BLE001 — malformed JSON from the RPC node, skip and move on
        log.debug("chain_scanner RPC response unparseable", method=method, err=str(err))
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("error") is not None:
        log.debug("chain_scanner RPC returned an error object", method=method, error=payload.get("error"))
        return None
    return payload.get("result")


async def get_chain_tip_slot(client: httpx.AsyncClient, env: Env) -> int | None:
    """Current finalized-ish slot (`getSlot`, default/confirmed commitment),
    short-TTL cached (DISCOVERY_BLOCK_SCAN_TIP_CACHE_TTL_SECONDS) so calling
    this more than once in a cycle doesn't cost an extra RPC round trip.
    """
    cache = _get_tip_cache(env.DISCOVERY_BLOCK_SCAN_TIP_CACHE_TTL_SECONDS)
    cached = cache.get("tip")
    if cached is not None:
        return cached
    slot = await _rpc_call(client, env, "getSlot", [{"commitment": "confirmed"}])
    if not isinstance(slot, int):
        return None
    cache.set("tip", slot)
    return slot


async def _load_progress(session: AsyncSession, cluster: str) -> DiscoveryScanProgress | None:
    result = await session.execute(select(DiscoveryScanProgress).where(DiscoveryScanProgress.cluster == cluster))
    return result.scalar_one_or_none()


async def _save_progress(session: AsyncSession, cluster: str, last_processed_slot: int) -> None:
    row = await _load_progress(session, cluster)
    now = datetime.now(UTC)
    if row is None:
        session.add(
            DiscoveryScanProgress(cluster=cluster, last_processed_slot=last_processed_slot, last_scanned_at=now)
        )
    else:
        row.last_processed_slot = last_processed_slot
        row.last_scanned_at = now
    await session.commit()


def _extract_fee_payer_candidates(
    block: dict[str, Any],
    swap_program_ids: set[str],
    *,
    max_wallets: int,
) -> set[str]:
    """Pure parser: given one `getBlock` (jsonParsed) result, returns the
    set of fee-payer wallet addresses for every transaction that touches at
    least one address in `swap_program_ids`. No I/O — unit-testable on a
    hand-built block payload the same way discovery.py's pure functions are.

    "Touches" = the program id appears anywhere in the transaction's account
    keys (covers both a top-level instruction AND a CPI into that program,
    e.g. Jupiter routing into Raydium in the same transaction — both the
    router's own program id and the inner DEX's program id will be present
    in accountKeys either way). The fee payer (accountKeys[0]) is always the
    transaction's first signer — the wallet that actually initiated and
    paid for the swap — never a pool/vault/PDA account.
    """
    found: set[str] = set()
    transactions = block.get("transactions") or []
    for entry in transactions:
        if len(found) >= max_wallets:
            break
        if not isinstance(entry, dict):
            continue
        meta = entry.get("meta") or {}
        if meta.get("err") is not None:
            continue  # failed transaction — nothing actually swapped
        message = (entry.get("transaction") or {}).get("message") or {}
        account_keys = message.get("accountKeys") or []
        if not account_keys:
            continue

        keys: list[str] = []
        for key_entry in account_keys:
            pubkey = key_entry.get("pubkey") if isinstance(key_entry, dict) else key_entry
            if isinstance(pubkey, str):
                keys.append(pubkey)

        if not keys:
            continue
        if not any(key in swap_program_ids for key in keys):
            continue

        fee_payer = keys[0]
        if fee_payer in swap_program_ids:
            continue  # malformed/unexpected shape — never treat a program id itself as a trader
        found.add(fee_payer)
    return found


async def _fetch_block(client: httpx.AsyncClient, env: Env, slot: int) -> dict[str, Any] | None:
    return await _rpc_call(
        client,
        env,
        "getBlock",
        [
            slot,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "transactionDetails": "full",
                "rewards": False,
            },
        ],
    )


async def scan_new_blocks(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    env: Env,
    known_addresses: set[str],
) -> ScanResult:
    """Scans the next bounded batch of not-yet-processed slots and returns
    every newly-discovered, not-already-known trader wallet as a
    DiscoveredCandidate — the blockchain-first discovery engine's primary
    (Priority 0) source. See engines/discovery.discover_candidates for how
    this is wired into the rest of the pipeline, and this module's
    docstring for the full design.

    Persists the new checkpoint (DiscoveryScanProgress) itself, inside this
    call, so a caller that only reads `known_addresses`/candidates back out
    still gets a durable, resumable checkpoint even if it never touches
    progress directly.
    """
    cluster = env.SOLANA_CLUSTER
    progress = await _load_progress(session, cluster)

    tip = await get_chain_tip_slot(http_client, env)
    if tip is None:
        log.warning("chain_scanner: could not fetch current slot, skipping this cycle")
        return ScanResult(candidates=[], slots_scanned=0, blocks_found=0, from_slot=None, to_slot=None)

    if progress is None:
        start_slot = max(0, tip - env.DISCOVERY_BLOCK_SCAN_INITIAL_LOOKBACK_SLOTS)
    else:
        start_slot = progress.last_processed_slot + 1

    if start_slot > tip:
        # Already caught up to (or somehow ahead of) the tip — nothing new yet.
        return ScanResult(candidates=[], slots_scanned=0, blocks_found=0, from_slot=start_slot, to_slot=tip)

    # Never scan the entire backlog in one cycle, however far behind the
    # checkpoint has fallen — bounded catch-up, gradual over many cycles.
    max_end = min(tip, start_slot + env.DISCOVERY_BLOCK_SCAN_MAX_CATCHUP_SLOTS - 1)
    end_slot = min(max_end, start_slot + env.DISCOVERY_BLOCK_SCAN_BATCH_SIZE - 1)
    slots = list(range(start_slot, end_slot + 1))

    swap_program_ids = set(SWAP_PROGRAM_IDS) | _extra_program_ids(env)
    semaphore = asyncio.Semaphore(env.DISCOVERY_BLOCK_SCAN_CONCURRENCY)

    async def _scan_one(slot: int) -> tuple[int, dict[str, Any] | None]:
        async with semaphore:
            block = await _fetch_block(http_client, env, slot)
            return slot, block

    started = time.monotonic()
    results = await asyncio.gather(*(_scan_one(slot) for slot in slots))

    candidates: list[DiscoveredCandidate] = []
    blocks_found = 0
    seen_this_batch: set[str] = set()
    for _slot, block in results:
        if block is None:
            # Slot was skipped (no leader block produced) or the fetch
            # failed after retries — either way it's final for this slot;
            # we still advance past it below rather than getting stuck
            # retrying a permanently-skipped slot forever.
            continue
        blocks_found += 1
        wallets = _extract_fee_payer_candidates(
            block, swap_program_ids, max_wallets=env.DISCOVERY_BLOCK_SCAN_MAX_WALLETS_PER_BLOCK
        )
        for address in wallets:
            if address in known_addresses or address in seen_this_batch:
                continue
            if not is_valid_solana_address(address):
                continue
            seen_this_batch.add(address)
            candidates.append(DiscoveredCandidate(address=address, source="blockchain_scan"))

    await _save_progress(session, cluster, end_slot)

    log.info(
        "chain_scanner batch complete",
        from_slot=start_slot,
        to_slot=end_slot,
        slots_scanned=len(slots),
        blocks_found=blocks_found,
        candidates_found=len(candidates),
        elapsed_seconds=round(time.monotonic() - started, 2),
        tip=tip,
        lag=tip - end_slot,
    )

    return ScanResult(
        candidates=candidates,
        slots_scanned=len(slots),
        blocks_found=blocks_found,
        from_slot=start_slot,
        to_slot=end_slot,
    )
