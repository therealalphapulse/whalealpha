"""domain/trading/auto_trade/claims.py

§9 (Idempotency). Atomic per-(user, signal) claim reservation, mirroring
real_automation_engine.try_claim_auto_buy's INSERT ... ON CONFLICT
pattern applied to this engine's own AutoTradeClaim table.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from infra.db.session import async_session
from models.auto_trade_claim import AutoTradeClaim

from .constants import CLAIM_RECONCILE_GRACE_SECONDS

logger = logging.getLogger("AlphaPulse.AutoTrade.Claims")


async def _claim_insert_or_reopen(user_id: int, signal_id: int, contract: str) -> AutoTradeClaim | None:
    async with async_session() as session:
        stmt = pg_insert(AutoTradeClaim).values(
            user_id=user_id, signal_id=signal_id, contract=contract, status="pending",
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_auto_trade_claims_user_signal",
            set_={"status": "pending", "updated_at": datetime.now(timezone.utc).replace(tzinfo=None)},
            where=(AutoTradeClaim.status == "failed"),
        ).returning(AutoTradeClaim.id)
        result = await session.execute(stmt)
        row = result.first()
        await session.commit()
        if row is None:
            return None
        return await session.get(AutoTradeClaim, row[0])


async def try_claim(user_id: int, signal_id: int, contract: str) -> AutoTradeClaim | None:
    """Reserve a claim for this (user, signal). Returns None if a claim
    already exists in 'pending' (still in-flight, within the
    reconciliation grace window) or 'committed' state -- the caller
    must not proceed to a buy in that case."""
    claim = await _claim_insert_or_reopen(user_id, signal_id, contract)
    if claim is not None:
        return claim

    async with async_session() as session:
        result = await session.execute(
            select(AutoTradeClaim).where(AutoTradeClaim.user_id == user_id, AutoTradeClaim.signal_id == signal_id)
        )
        existing = result.scalar_one_or_none()
    if existing is None or existing.status == "committed":
        return None
    created_at = existing.created_at
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created_at).total_seconds() if created_at else 0.0
    if age < CLAIM_RECONCILE_GRACE_SECONDS:
        return None
    return await _claim_insert_or_reopen(user_id, signal_id, contract)


async def commit_claim(claim_id: int) -> None:
    async with async_session() as session:
        claim = await session.get(AutoTradeClaim, claim_id)
        if claim:
            claim.status = "committed"
            await session.commit()


async def release_claim(claim_id: int) -> None:
    async with async_session() as session:
        claim = await session.get(AutoTradeClaim, claim_id)
        if claim:
            claim.status = "failed"
            await session.commit()
