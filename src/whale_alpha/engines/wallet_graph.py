"""Wallet Graph Expansion — Hybrid Discovery Engine Priority 4.

New module (Phase 1 refactor). Every promoted wallet becomes a discovery
node: we look at the tokens it has actually traded (real swap history, same
data source as engines/discovery_metrics.py) and re-query who else holds
those tokens. A related address that keeps showing up across *distinct*
tokens traded by an already-trusted wallet is meaningfully more likely to be
a genuine co-trading relationship (shared alpha, a cabal, a copy-trading
cluster) than one shared hot token, which could just be coincidence — hence
the DISCOVERY_GRAPH_MIN_COOCCURRENCE gate before a related address is queued
as its own candidate.

Deliberately built on the existing Postgres schema (a small
WalletRelationship table, see db/models.py) rather than a graph database,
per the architecture requirements — this scales comfortably to the
500-5,000 wallet target without new infrastructure.

The relationship-strength math is a pure function (`update_relationship`,
`compute_strength`) — fully unit-testable without a database. The
orchestration around it (`expand_wallet_graph`) is intentionally thin, same
split as engines/discovery.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from solana.rpc.async_api import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from whale_alpha.config import Env
from whale_alpha.db.models import WalletRelationship, WalletStatus, WhaleWallet
from whale_alpha.integrations import price_feed
from whale_alpha.integrations.wallet_discovery_source import (
    DiscoveredCandidate,
    fetch_wallet_swap_history,
    find_candidates_from_token_holders,
)
from whale_alpha.utils.logger import child_logger

log = child_logger("walletGraph")


# --------------------------------------------------------------------------
# Pure relationship-strength logic — no I/O, fully unit-testable.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RelationshipState:
    """In-memory mirror of the mutable fields on a WalletRelationship row,
    so the update math can be unit-tested without touching the database."""

    co_occurrence_count: int
    shared_token_mints: tuple[str, ...]
    strength: float


def compute_strength(co_occurrence_count: int) -> float:
    """Saturating 0..1 confidence from a co-occurrence count — the first
    couple of shared tokens matter a lot, the marginal value of the tenth
    shared token is small. Never reaches 1.0 (there's always some chance of
    coincidence), and never used as a promotion gate on its own — see
    evaluate_promotion in engines/discovery.py.
    """
    return round(1.0 - math.exp(-0.5 * co_occurrence_count), 3)


def update_relationship(existing: RelationshipState | None, token_mint: str) -> RelationshipState:
    """Returns the RelationshipState after observing `token_mint` as a
    shared token between two wallets this cycle. Idempotent for a token
    already recorded (re-observing the same mint doesn't inflate the count)
    — only genuinely *new* shared tokens raise co-occurrence.
    """
    if existing is None:
        mints = (token_mint,)
        return RelationshipState(co_occurrence_count=1, shared_token_mints=mints, strength=compute_strength(1))

    if token_mint in existing.shared_token_mints:
        return existing

    mints = (*existing.shared_token_mints, token_mint)
    count = existing.co_occurrence_count + 1
    return RelationshipState(co_occurrence_count=count, shared_token_mints=mints, strength=compute_strength(count))


# --------------------------------------------------------------------------
# Orchestration — I/O around the pure functions above.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphExpansionConfig:
    batch_size: int
    max_tokens_per_wallet: int
    min_cooccurrence: int
    max_holders_per_token: int
    rpc_min_interval_seconds: float
    rpc_max_retries: int

    @classmethod
    def from_env(cls, env: Env) -> GraphExpansionConfig:
        return cls(
            batch_size=env.DISCOVERY_GRAPH_EXPANSION_BATCH_SIZE,
            max_tokens_per_wallet=env.DISCOVERY_GRAPH_MAX_TOKENS_PER_WALLET,
            min_cooccurrence=env.DISCOVERY_GRAPH_MIN_COOCCURRENCE,
            max_holders_per_token=env.DISCOVERY_MAX_HOLDERS_PER_TOKEN,
            rpc_min_interval_seconds=env.DISCOVERY_RPC_MIN_INTERVAL_SECONDS,
            rpc_max_retries=env.DISCOVERY_RPC_MAX_RETRIES,
        )


async def _get_relationship(
    session: AsyncSession, wallet_address: str, related_address: str
) -> WalletRelationship | None:
    result = await session.execute(
        select(WalletRelationship).where(
            WalletRelationship.wallet_address == wallet_address,
            WalletRelationship.related_address == related_address,
        )
    )
    return result.scalar_one_or_none()


async def expand_wallet_graph(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    connection: AsyncClient,
    env: Env,
    known_addresses: set[str],
) -> tuple[list[DiscoveredCandidate], int]:
    """Processes a batch of already-APPROVED wallets (oldest-updated first,
    so every tracked wallet eventually gets expanded without ever redoing
    the whole population in one cycle — same incremental-batch shape as
    rescore_tracked_wallets in engines/discovery.py), building/reinforcing
    wallet_relationships and returning any related addresses that just
    crossed the co-occurrence threshold as fresh DiscoveredCandidates.

    Returns (new_candidates, wallets_processed). Never raises for a single
    wallet's or token's failure — those are logged and skipped, consistent
    with every other discovery source.
    """
    config = GraphExpansionConfig.from_env(env)

    result = await session.execute(
        select(WhaleWallet)
        .where(WhaleWallet.status == WalletStatus.APPROVED)
        .order_by(WhaleWallet.updated_at.asc())
        .limit(config.batch_size)
    )
    batch = list(result.scalars())
    if not batch:
        return [], 0

    sol_price_usd = await price_feed.get_sol_price_usd(http_client, env)
    if sol_price_usd is None:
        log.debug("Graph expansion: SOL/USD price unavailable, skipping this cycle")
        return [], 0

    new_candidates: list[DiscoveredCandidate] = []
    processed = 0

    for wallet in batch:
        try:
            swaps = await fetch_wallet_swap_history(
                http_client, env, wallet.address, sol_price_usd=sol_price_usd
            )
        except Exception as err:  # noqa: BLE001 — one wallet's history failing shouldn't stop the batch
            log.debug("Graph expansion: swap history fetch failed", address=wallet.address, err=str(err))
            continue

        if not swaps:
            continue
        processed += 1

        # Most recently traded distinct mints first — recent activity is
        # the more relevant signal for "who else is trading alongside this
        # wallet right now".
        seen_mints: list[str] = []
        for swap in sorted(swaps, key=lambda s: s.timestamp, reverse=True):
            if swap.token_mint not in seen_mints:
                seen_mints.append(swap.token_mint)
            if len(seen_mints) >= config.max_tokens_per_wallet:
                break

        for token_mint in seen_mints:
            try:
                holders = await find_candidates_from_token_holders(
                    connection,
                    token_mint,
                    config.max_holders_per_token,
                    min_interval_seconds=config.rpc_min_interval_seconds,
                    max_retries=config.rpc_max_retries,
                )
            except Exception as err:  # noqa: BLE001 — one bad mint shouldn't stop the wallet's expansion
                log.debug("Graph expansion: holder lookup failed", mint=token_mint, err=str(err))
                continue

            for holder in holders:
                related_address = holder.address
                if related_address == wallet.address:
                    continue

                existing_row = await _get_relationship(session, wallet.address, related_address)
                existing_state = (
                    RelationshipState(
                        co_occurrence_count=existing_row.co_occurrence_count,
                        shared_token_mints=tuple(existing_row.shared_token_mints),
                        strength=existing_row.strength,
                    )
                    if existing_row is not None
                    else None
                )
                new_state = update_relationship(existing_state, token_mint)

                if existing_row is None:
                    session.add(
                        WalletRelationship(
                            wallet_address=wallet.address,
                            related_address=related_address,
                            relationship_type="CO_HOLDER",
                            co_occurrence_count=new_state.co_occurrence_count,
                            shared_token_mints=list(new_state.shared_token_mints),
                            strength=new_state.strength,
                        )
                    )
                elif new_state.co_occurrence_count != existing_row.co_occurrence_count:
                    existing_row.co_occurrence_count = new_state.co_occurrence_count
                    existing_row.shared_token_mints = list(new_state.shared_token_mints)
                    existing_row.strength = new_state.strength
                    existing_row.last_observed_at = datetime.now(UTC)

                already_known = related_address in known_addresses
                if not already_known and new_state.co_occurrence_count >= config.min_cooccurrence:
                    known_addresses.add(related_address)
                    new_candidates.append(
                        DiscoveredCandidate(
                            address=related_address,
                            source="wallet_graph_expansion",
                            discovered_from_token_mint=token_mint,
                        )
                    )
                    log.info(
                        "Wallet graph expanded",
                        source_wallet=wallet.address,
                        related_wallet=related_address,
                        co_occurrence=new_state.co_occurrence_count,
                    )

    await session.commit()
    return new_candidates, processed
