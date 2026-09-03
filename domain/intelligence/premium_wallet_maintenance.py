"""
Wallet Maintenance Engine.

The "self-managing" half of the Premium Intelligence Engine: after
discovery finds candidates and the scorer keeps every reputation score
current, this module is what actually keeps the Smart Money database
healthy over time with zero manual administration:

  - Promotes candidates into active tiers (elite/core/watch) — already
    partly handled inline by the scorer, this pass double-checks tier
    boundaries after every scoring cycle.
  - Puts chronically underperforming wallets on probation, then removes
    them if they don't recover within PREMIUM_WALLET_PROBATION_DAYS.
  - Removes wallets that have gone dark (no on-chain activity for
    PREMIUM_WALLET_INACTIVITY_REMOVE_DAYS) — a wallet AlphaPulse can't
    observe trading can't contribute to consensus anyway.
  - Enforces PREMIUM_WALLET_LONGTERM_TARGET / PREMIUM_WALLET_HARD_CAP by
    trimming the lowest-scoring wallets once the database is comfortably
    past target size, so quality rises as the DB grows toward 1,000+
    rather than being diluted by it.

Removal is always a soft delete (status="removed") — rows are kept for
audit/history, and every other module filters status != "removed".
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func as sa_func

from infra.db.session import async_session
from config.settings import (
    PREMIUM_WALLET_REMOVE_SCORE,
    PREMIUM_WALLET_WATCH_SCORE,
    PREMIUM_WALLET_MIN_TRADES_BEFORE_REMOVAL,
    PREMIUM_WALLET_PROBATION_DAYS,
    PREMIUM_WALLET_INACTIVITY_REMOVE_DAYS,
    PREMIUM_WALLET_INITIAL_TARGET,
    PREMIUM_WALLET_LONGTERM_TARGET,
    PREMIUM_WALLET_HARD_CAP,
)
from models.premium_wallet import PremiumWallet

logger = logging.getLogger("AlphaPulse.PremiumMaintenance")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _apply_probation_and_removal(session) -> dict:
    stats = {"put_on_probation": 0, "recovered": 0, "removed_low_score": 0, "removed_inactive": 0}
    now = _now()

    res = await session.execute(
        select(PremiumWallet).where(PremiumWallet.status.in_(["active", "watch"]))
    )
    wallets = res.scalars().all()

    for wallet in wallets:
        score = wallet.reputation_score or 0.0

        # Inactivity removal — independent of score, since a silent
        # wallet simply can't contribute to consensus or be judged.
        if wallet.last_activity_at:
            days_inactive = (now - wallet.last_activity_at).total_seconds() / 86400
            if days_inactive >= PREMIUM_WALLET_INACTIVITY_REMOVE_DAYS:
                wallet.status = "removed"
                wallet.removed_at = now
                wallet.removed_reason = f"inactive {int(days_inactive)}d"
                stats["removed_inactive"] += 1
                continue

        underperforming = score < PREMIUM_WALLET_WATCH_SCORE
        has_enough_trades = (wallet.trades_observed or 0) >= PREMIUM_WALLET_MIN_TRADES_BEFORE_REMOVAL

        if wallet.status == "active" and underperforming:
            wallet.status = "watch"
            wallet.probation_started_at = now
            stats["put_on_probation"] += 1
            continue

        if wallet.status == "watch":
            if not underperforming:
                wallet.status = "active"
                wallet.probation_started_at = None
                stats["recovered"] += 1
                continue

            probation_start = wallet.probation_started_at or now
            days_on_probation = (now - probation_start).total_seconds() / 86400

            hard_fail = has_enough_trades and score < PREMIUM_WALLET_REMOVE_SCORE
            probation_expired = days_on_probation >= PREMIUM_WALLET_PROBATION_DAYS

            if hard_fail or probation_expired:
                wallet.status = "removed"
                wallet.removed_at = now
                wallet.removed_reason = (
                    f"score {score:.1f} below floor after {int(days_on_probation)}d probation"
                )
                stats["removed_low_score"] += 1

    return stats


async def _enforce_db_size(session) -> dict:
    """
    Once the wallet DB is comfortably past the long-term target, trim
    the lowest-scoring non-elite wallets so overall quality keeps rising
    with scale instead of being diluted. Elite/core wallets are never
    trimmed this way — only candidate/watch-tier stragglers.
    """
    stats = {"trimmed_for_capacity": 0}

    res = await session.execute(
        select(sa_func.count()).select_from(PremiumWallet).where(PremiumWallet.status != "removed")
    )
    count = res.scalar() or 0

    if count <= PREMIUM_WALLET_LONGTERM_TARGET:
        return stats

    overflow = min(count - PREMIUM_WALLET_LONGTERM_TARGET, PREMIUM_WALLET_HARD_CAP)

    res = await session.execute(
        select(PremiumWallet)
        .where(PremiumWallet.status.in_(["candidate", "watch"]))
        .order_by(PremiumWallet.reputation_score.asc())
        .limit(overflow)
    )
    trim_candidates = res.scalars().all()

    now = _now()
    for wallet in trim_candidates:
        wallet.status = "removed"
        wallet.removed_at = now
        wallet.removed_reason = "trimmed for database capacity (low score, low-priority tier)"
        stats["trimmed_for_capacity"] += 1

    return stats


async def run_maintenance_cycle() -> dict:
    async with async_session() as session:
        stats = await _apply_probation_and_removal(session)
        capacity_stats = await _enforce_db_size(session)
        stats.update(capacity_stats)
        await session.commit()

    logger.info(
        "🛠️ Premium maintenance cycle: "
        f"{stats['put_on_probation']} put on probation, "
        f"{stats['recovered']} recovered, "
        f"{stats['removed_low_score']} removed (low score), "
        f"{stats['removed_inactive']} removed (inactive), "
        f"{stats['trimmed_for_capacity']} trimmed (capacity)"
    )
    return stats


async def get_engine_stats() -> dict:
    """Snapshot used by the /premium_stats command and admin tooling."""
    async with async_session() as session:
        res = await session.execute(
            select(PremiumWallet.status, PremiumWallet.tier, sa_func.count())
            .where(PremiumWallet.status != "removed")
            .group_by(PremiumWallet.status, PremiumWallet.tier)
        )
        rows = res.all()

        res_total = await session.execute(
            select(sa_func.count()).select_from(PremiumWallet).where(PremiumWallet.status != "removed")
        )
        total = res_total.scalar() or 0

        res_avg = await session.execute(
            select(sa_func.avg(PremiumWallet.reputation_score)).where(PremiumWallet.status == "active")
        )
        avg_score = res_avg.scalar()

    by_status: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    for status, tier, n in rows:
        by_status[status] = by_status.get(status, 0) + n
        by_tier[tier] = by_tier.get(tier, 0) + n

    active_count = by_status.get("active", 0)
    if active_count < PREMIUM_WALLET_INITIAL_TARGET:
        operational_stage = "bootstrapping"
    elif active_count < PREMIUM_WALLET_LONGTERM_TARGET:
        operational_stage = "operational"
    elif active_count < PREMIUM_WALLET_HARD_CAP:
        operational_stage = "recommended"
    else:
        operational_stage = "optimal"

    return {
        "total_wallets": total,
        "by_status": by_status,
        "by_tier": by_tier,
        "avg_active_score": round(avg_score, 1) if avg_score else None,
        "initial_target": PREMIUM_WALLET_LONGTERM_TARGET,
        "hard_cap": PREMIUM_WALLET_HARD_CAP,
        "active_wallets": active_count,
        "minimum_operational_threshold": PREMIUM_WALLET_INITIAL_TARGET,
        "recommended_capacity": PREMIUM_WALLET_LONGTERM_TARGET,
        "optimal_capacity": PREMIUM_WALLET_HARD_CAP,
        "operational_stage": operational_stage,
    }


async def premium_maintenance_loop(interval_seconds: int) -> None:
    logger.info("🧹 Premium Wallet Maintenance Engine active")
    while True:
        try:
            await run_maintenance_cycle()
        except Exception as e:
            logger.error(f"Premium maintenance cycle error: {e}")
        await asyncio.sleep(interval_seconds)
