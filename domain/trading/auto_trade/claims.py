"""domain/trading/auto_trade/claims.py

§9 (Idempotency). Atomic per-(user, signal) claim reservation, mirroring
real_automation_engine.try_claim_auto_buy's INSERT ... ON CONFLICT
pattern applied to this engine's own AutoTradeClaim table.

Retry policy (fixes a production incident: a 'failed' claim used to be
reopened back to 'pending' on the very next scan tick with **no**
backoff at all -- AUTO_TRADE_SCAN_INTERVAL_SECONDS is 5s, so any
rejection that kept failing the same way, e.g. an underfunded wallet,
re-fired every ~5s for the entire SIGNAL_LOOKBACK_MINUTES window,
producing a flood of duplicate "Auto-Trade Rejected" notifications for
one signal):

  - A claim failure is either *retryable* (a technical/transient
    condition, e.g. an RPC call failing -- see
    constants.RETRYABLE_REJECTION_REASONS and orchestrator.py's
    classification of execution-failure reasons) or *not* (a
    deterministic outcome given current state, e.g. insufficient
    balance, daily limit reached -- retrying five seconds later can't
    change the answer).
  - Retryable failures get up to MAX_CLAIM_RETRY_ATTEMPTS, each gated
    by CLAIM_RETRY_BACKOFF_SECONDS of real wall-clock spacing, not an
    instant reopen. Once exhausted, the claim is marked "skipped".
  - Non-retryable failures go straight to "skipped" on the first hit.
  - "skipped" (like "committed") is a terminal state try_claim() will
    never reopen for that exact signal_id again. A later, fresh signal
    for the same token gets its own claim and is unaffected.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from infra.db.session import async_session, engine
from models.auto_trade_claim import AutoTradeClaim

from .constants import CLAIM_RECONCILE_GRACE_SECONDS, CLAIM_RETRY_BACKOFF_SECONDS, MAX_CLAIM_RETRY_ATTEMPTS

logger = logging.getLogger("WhaleAlpha.AutoTrade.Claims")

TERMINAL_STATUSES = ("committed", "skipped")


async def _try_insert(user_id: int, signal_id: int, contract: str) -> AutoTradeClaim | None:
    """Plain INSERT ... ON CONFLICT DO NOTHING -- succeeds only when no
    claim exists yet for this (user, signal) at all."""
    async with async_session() as session:
        stmt = pg_insert(AutoTradeClaim).values(
            user_id=user_id, signal_id=signal_id, contract=contract, status="pending",
        )
        stmt = stmt.on_conflict_do_nothing(constraint="uq_auto_trade_claims_user_signal").returning(AutoTradeClaim.id)
        result = await session.execute(stmt)
        row = result.first()
        await session.commit()
        if row is None:
            return None
        return await session.get(AutoTradeClaim, row[0])


async def _reopen(claim_id: int) -> AutoTradeClaim | None:
    async with async_session() as session:
        claim = await session.get(AutoTradeClaim, claim_id)
        if claim is None:
            return None
        claim.status = "pending"
        await session.commit()
        await session.refresh(claim)
        return claim


async def try_claim(user_id: int, signal_id: int, contract: str) -> AutoTradeClaim | None:
    """Reserve a claim for this (user, signal). Returns None when the
    claim must NOT be (re)claimed right now:
      - 'committed' or 'skipped' -- terminal, never retried.
      - 'pending' younger than CLAIM_RECONCILE_GRACE_SECONDS -- still
        genuinely in-flight (another evaluation owns it).
      - 'failed' younger than CLAIM_RETRY_BACKOFF_SECONDS since its
        last update -- a bounded retry that hasn't backed off yet.
    """
    claim = await _try_insert(user_id, signal_id, contract)
    if claim is not None:
        return claim

    async with async_session() as session:
        result = await session.execute(
            select(AutoTradeClaim).where(AutoTradeClaim.user_id == user_id, AutoTradeClaim.signal_id == signal_id)
        )
        existing = result.scalar_one_or_none()
    if existing is None:
        return None
    if existing.status in TERMINAL_STATUSES:
        return None

    reference = existing.updated_at or existing.created_at
    if reference is not None and reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - reference).total_seconds() if reference else 0.0

    if existing.status == "pending":
        if age < CLAIM_RECONCILE_GRACE_SECONDS:
            return None  # still in-flight -- not stuck yet, don't steal it
    elif existing.status == "failed":
        if age < CLAIM_RETRY_BACKOFF_SECONDS:
            return None  # backed off -- too soon for the next bounded retry
    else:
        return None

    return await _reopen(existing.id)


async def commit_claim(claim_id: int) -> None:
    async with async_session() as session:
        claim = await session.get(AutoTradeClaim, claim_id)
        if claim:
            claim.status = "committed"
            await session.commit()


async def finalize_claim(claim_id: int, *, retryable: bool, reason: str | None = None) -> str:
    """Resolve a claim that did not result in a buy this attempt.

    Returns one of:
      "retry_scheduled" -- retryable, attempts remain; claim left
          "failed" and will become reclaimable after
          CLAIM_RETRY_BACKOFF_SECONDS (see try_claim). Caller should
          NOT notify the user on this outcome -- that's the point:
          transient blips get retried quietly, not spammed.
      "gave_up"         -- retryable, but MAX_CLAIM_RETRY_ATTEMPTS is
          now exhausted; claim marked "skipped" (terminal). Caller
          should notify once.
      "skipped"         -- not retryable; claim marked "skipped"
          (terminal) immediately, no attempt consumed. Caller should
          notify once.

    Either terminal outcome ("gave_up" or "skipped") permanently stops
    this exact signal_id from being retried -- see module docstring.
    """
    async with async_session() as session:
        claim = await session.get(AutoTradeClaim, claim_id)
        if claim is None:
            return "skipped"
        claim.last_reason = reason
        if retryable and claim.retry_count < MAX_CLAIM_RETRY_ATTEMPTS:
            claim.retry_count += 1
            claim.status = "failed"
            await session.commit()
            return "retry_scheduled"
        outcome = "gave_up" if retryable else "skipped"
        claim.status = "skipped"
        await session.commit()
        return outcome


async def migrate_auto_trade_claims_schema() -> None:
    """Idempotent production migration for the Auto-Trade Engine's claim
    table, matching the boot-time pattern used by every other
    migrate_*_schema() in this codebase (e.g.
    domain/trading/real/robinhood_wallet.py,
    domain/trading/auto_trade/policy_service.py) -- see that module's
    docstring for why this runs on every boot instead of via Alembic."""
    statements = [
        "ALTER TABLE auto_trade_claims ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE auto_trade_claims ADD COLUMN IF NOT EXISTS last_reason VARCHAR",
    ]
    try:
        async with engine.begin() as conn:
            for stmt in statements:
                await conn.execute(text(stmt))
        logger.info("Auto-Trade claims schema migration complete")
    except Exception as e:
        logger.error(f"Auto-Trade claims schema migration error (non-fatal): {e}")
