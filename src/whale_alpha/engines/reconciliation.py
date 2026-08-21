"""Startup trade reconciliation — NEW functionality, not in the original TS code.

Per porting requirement #3: before this project goes near real funds, any
trade left in PENDING or SUBMITTED status by a process that crashed or was
redeployed mid-trade must be reconciled against Solana before the bot resumes
normal operation — otherwise a transaction that actually landed on-chain could
be silently retried (double-spend risk) or a transaction that never landed
could be left stuck forever, blocking the user's risk-engine counters
(open positions, daily trade count) with a phantom trade.

Call `reconcile_pending_trades(session, connection)` once at startup, before
the bot starts polling Telegram or the scheduler starts evaluating signals.

Reconciliation strategy:
  * PENDING with no tx_signature and no submitted_at: the process crashed
    before ever calling Jupiter/sendTransaction. Nothing was ever broadcast —
    safe to mark CANCELLED.
  * SUBMITTED with a tx_signature: query Solana for that signature's status.
      - Finalized/confirmed with no error -> mark CONFIRMED.
      - Finalized with an error -> mark FAILED.
      - Not found and old enough that its blockhash's `last_valid_block_height`
        has definitely expired -> mark FAILED (the transaction can no longer
        land; Solana drops the blockhash lease after ~150 blocks/~1-2 min).
      - Not found and still within its valid window -> leave as SUBMITTED and
        let the next reconciliation pass (or the scheduler) check again; it
        may still land.
  * SUBMITTED with a tx_signature but no last_valid_block_height on record
    (shouldn't happen with this port, but defensive): treat missing-on-chain
    as "leave for now" as well, capped by `reconciliation_attempts` to avoid
    an infinite retry loop — after MAX_RECONCILIATION_ATTEMPTS with no result,
    mark FAILED and flag for manual review via an AuditLog entry.
"""

from __future__ import annotations

from datetime import datetime, timezone

from solana.rpc.async_api import AsyncClient
from solders.signature import Signature
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from whale_alpha.utils.logger import child_logger

from whale_alpha.db.models import AuditLog, Trade, TradeStatus

log = child_logger("reconciliation")

MAX_RECONCILIATION_ATTEMPTS = 10


async def reconcile_pending_trades(session: AsyncSession, connection: AsyncClient) -> dict[str, int]:
    """Run once at startup. Returns counts of what happened, for a log line/health check."""
    summary = {"cancelled_never_sent": 0, "confirmed": 0, "failed": 0, "left_pending": 0}

    stale_pending = await session.execute(select(Trade).where(Trade.status == TradeStatus.PENDING))
    for trade in stale_pending.scalars():
        if trade.tx_signature is None and trade.submitted_at is None:
            trade.status = TradeStatus.CANCELLED
            summary["cancelled_never_sent"] += 1
            log.info("Reconciled: cancelled a never-submitted PENDING trade", trade_id=trade.id)

    submitted = await session.execute(select(Trade).where(Trade.status == TradeStatus.SUBMITTED))
    for trade in submitted.scalars():
        if not trade.tx_signature:
            # Shouldn't happen (we only move to SUBMITTED once we have a
            # signature in trade_executor.py), but handle defensively.
            trade.reconciliation_attempts += 1
            if trade.reconciliation_attempts >= MAX_RECONCILIATION_ATTEMPTS:
                trade.status = TradeStatus.FAILED
                summary["failed"] += 1
                await _flag_for_manual_review(session, trade, "no_tx_signature_after_max_attempts")
            else:
                summary["left_pending"] += 1
            continue

        try:
            sig = Signature.from_string(trade.tx_signature)
            status_resp = await connection.get_signature_statuses([sig])
            status = status_resp.value[0]
        except (RuntimeError, ValueError, TypeError) as exc:
            log.error("Reconciliation RPC lookup failed", trade_id=trade.id, error=str(exc))
            trade.reconciliation_attempts += 1
            if trade.reconciliation_attempts >= MAX_RECONCILIATION_ATTEMPTS:
                trade.status = TradeStatus.FAILED
                summary["failed"] += 1
                await _flag_for_manual_review(session, trade, "rpc_lookup_failed_after_max_attempts")
            else:
                summary["left_pending"] += 1
            continue

        if status is not None:
            if status.err is None:
                trade.status = TradeStatus.CONFIRMED
                trade.confirmed_at = datetime.now(timezone.utc)
                summary["confirmed"] += 1
                log.info("Reconciled: trade confirmed on-chain", trade_id=trade.id, tx=trade.tx_signature)
            else:
                trade.status = TradeStatus.FAILED
                summary["failed"] += 1
                log.info(
                    "Reconciled: trade failed on-chain",
                    trade_id=trade.id,
                    tx=trade.tx_signature,
                    err=str(status.err),
                )
            continue

        # Not found on-chain yet. Decide whether the blockhash lease has
        # definitely expired (transaction can never land) or whether it might
        # still be in flight.
        blockhash_expired = await _blockhash_definitely_expired(connection, trade)
        if blockhash_expired:
            trade.status = TradeStatus.FAILED
            summary["failed"] += 1
            log.info(
                "Reconciled: blockhash expired, transaction can no longer land",
                trade_id=trade.id,
                tx=trade.tx_signature,
            )
        else:
            trade.reconciliation_attempts += 1
            if trade.reconciliation_attempts >= MAX_RECONCILIATION_ATTEMPTS:
                trade.status = TradeStatus.FAILED
                summary["failed"] += 1
                await _flag_for_manual_review(session, trade, "not_found_after_max_attempts")
            else:
                summary["left_pending"] += 1

    await session.commit()
    log.info("Trade reconciliation complete", **summary)
    return summary


async def _blockhash_definitely_expired(connection: AsyncClient, trade: Trade) -> bool:
    if trade.last_valid_block_height is None:
        return False
    try:
        current_height_resp = await connection.get_block_height()
        return current_height_resp.value > trade.last_valid_block_height
    except (RuntimeError, ValueError, TypeError):
        # If we can't tell, don't guess "expired" — leave it for the next pass.
        return False


async def _flag_for_manual_review(session: AsyncSession, trade: Trade, reason: str) -> None:
    session.add(
        AuditLog(
            actor_id=trade.user_id,
            action="TRADE_RECONCILIATION_MANUAL_REVIEW",
            target_type="Trade",
            target_id=trade.id,
            metadata_={"reason": reason, "tx_signature": trade.tx_signature},
        )
    )
