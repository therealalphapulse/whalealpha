"""domain/trading/auto_trade/reconciliation.py

§25 (Reconciliation Rules) and §33 (Crash Recovery). "The blockchain is
the source of truth" -- this module resolves any AutoTradePosition or
AutoTradeClaim left in a non-terminal / uncertain state (typically
after a worker crash or restart mid-trade) by checking actual on-chain
outcomes before allowing anything to proceed.

Runs once at worker startup (see worker.py), AND periodically during
normal operation (reconcile_stuck_positions(), called from
auto_trade_exit_loop every RECONCILIATION_SWEEP_INTERVAL_SECONDS) to
sweep BUY_RECONCILING / SELL_RECONCILING positions left dangling by a
single failed confirmation poll (robinhood_swap.py's _sign_send
returning "unknown" -- see that module's docstring). Before this
periodic sweep existed, a position stuck in "unknown" during normal
operation (no crash, no restart) was only ever re-checked against the
chain at the NEXT worker restart -- which could be hours or days away --
rather than within a few minutes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from infra.db.session import async_session
from models.auto_trade_position import AutoTradePosition, AutoTradeState
from models.auto_trade_claim import AutoTradeClaim
from domain.trading.real.robinhood_swap import get_confirmed_transaction_deltas, SwapError
from domain.trading.real.robinhood_wallet import get_real_wallet

from . import claims, position_manager
from .constants import CLAIM_RECONCILE_GRACE_SECONDS

logger = logging.getLogger("WhaleAlpha.AutoTrade.Reconciliation")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def reconcile_position(position: AutoTradePosition) -> None:
    """Resolve one BUY_RECONCILING / SELL_RECONCILING position against
    the chain (§25/§33). Never assumes success or failure without an
    on-chain check."""
    wallet = await get_real_wallet(position.user_id)
    if not wallet:
        logger.warning("[AutoTrade] reconciliation: no wallet for user=%s pos=%s", position.user_id, position.id)
        return

    if position.state == AutoTradeState.BUY_RECONCILING:
        if not position.buy_tx_signature:
            await position_manager.set_state(position.id, AutoTradeState.BUY_FAILED, closed_at=_now())
            if position.claim_id:
                # Left mid-flight by a crash/restart, not a deterministic
                # rejection -- eligible for the normal bounded, backed-off
                # retry (see claims.py), same as any other technical failure.
                await claims.finalize_claim(position.claim_id, retryable=True, reason="reconciled: no buy tx signature recorded")
            return
        try:
            fill = await get_confirmed_transaction_deltas(position.buy_tx_signature, wallet.public_key, position.contract)
        except SwapError as e:
            logger.info("[AutoTrade] buy reconciliation still pending pos=%s: %s", position.id, e)
            return
        if fill["token_delta_raw"] > 0:
            decimals = position.token_decimals or 0
            token_quantity = fill["token_delta_raw"] / (10 ** decimals) if decimals else fill["token_delta_raw"]
            entry_price = (position.sol_spent / token_quantity) if token_quantity else None
            await position_manager.set_state(
                position.id, AutoTradeState.POSITION_OPEN,
                token_quantity=token_quantity, remaining_quantity=token_quantity,
                entry_price=entry_price, total_cost_basis_sol=position.sol_spent,
                highest_observed_price=entry_price, opened_at=_now(),
            )
            if position.claim_id:
                await claims.commit_claim(position.claim_id)
            logger.info("[AutoTrade] buy reconciled as SUCCESS pos=%s qty=%s", position.id, token_quantity)
        else:
            await position_manager.set_state(position.id, AutoTradeState.BUY_FAILED, closed_at=_now())
            if position.claim_id:
                await claims.finalize_claim(position.claim_id, retryable=True, reason="reconciled: no token delta on-chain")
            logger.info("[AutoTrade] buy reconciled as FAILURE pos=%s", position.id)

    elif position.state == AutoTradeState.SELL_RECONCILING:
        balance = await position_manager.resolve_sellable_balance(position.user_id, position)
        if not balance.get("ok"):
            return
        if balance["onchain_amount"] <= max(1e-9, balance["db_amount"] * 0.01):
            await position_manager.set_state(
                position.id, AutoTradeState.CLOSED, remaining_quantity=0.0, closed_at=_now(),
            )
            logger.info("[AutoTrade] sell reconciled as SUCCESS pos=%s (on-chain balance now ~0)", position.id)
        else:
            await position_manager.set_state(position.id, AutoTradeState.POSITION_OPEN)
            logger.info("[AutoTrade] sell reconciled as FAILURE/no-op pos=%s (tokens still held)", position.id)


async def sweep_orphaned_claims() -> int:
    """§9/§33 -- crash recovery for claims, not just positions.

    A claim is reserved (claims.try_claim) *before* its AutoTradePosition
    row is created (see orchestrator.try_auto_trade) -- every early-return
    path in between already calls claims.finalize_claim() to resolve the
    claim (either a bounded, backed-off retry or a permanent "skipped",
    depending on the failure -- see claims.py's module docstring). The
    only way a claim is left "pending" forever with no AutoTradePosition
    at all is a hard process crash inside that narrow window (not a
    caught exception -- those already resolve the claim).

    A claim like that can never resolve itself: claims.try_claim only
    reopens a "pending" claim once it's older than
    CLAIM_RECONCILE_GRACE_SECONDS (stuck-claim recovery), and nothing
    else ever moves a "pending" claim out of that state except
    claims.finalize_claim() -- which nothing is left to call once the
    process that would have called it is gone. Sweep those here, the
    same way reconcile_position() above resolves positions left
    mid-flight: any claim still "pending" past the grace period with no
    matching position is definitively abandoned -- if anything were
    still executing it, it would already have created that position row
    -- so it's safe to flip to "failed" (a technical/crash situation,
    not a deterministic rejection) and let claims.try_claim's normal
    backed-off retry pick it up again.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=CLAIM_RECONCILE_GRACE_SECONDS)
    swept = 0
    async with async_session() as session:
        result = await session.execute(
            select(AutoTradeClaim)
            .outerjoin(AutoTradePosition, AutoTradePosition.claim_id == AutoTradeClaim.id)
            .where(
                AutoTradeClaim.status == "pending",
                AutoTradeClaim.created_at < cutoff,
                AutoTradePosition.id.is_(None),
            )
        )
        orphaned = result.scalars().all()
        for claim in orphaned:
            claim.status = "failed"
            claim.last_reason = "orphaned: worker crashed before position could be created"
            swept += 1
        if swept:
            await session.commit()
    if swept:
        logger.warning(
            "[AutoTrade] reconciliation swept %d orphaned pending claim(s) with no position -- "
            "likely left behind by a previous crash; now retryable",
            swept,
        )
    return swept


async def reconcile_stuck_positions() -> int:
    """Resolves every BUY_RECONCILING / SELL_RECONCILING position against
    the chain. Shared by run_startup_reconciliation() (once, at process
    start) and the periodic sweep in worker.py (every
    RECONCILIATION_SWEEP_INTERVAL_SECONDS during normal operation) --
    same logic either way, just a different trigger."""
    positions = await position_manager.get_all_non_terminal_positions()
    reconciled = 0
    for position in positions:
        if position.state in (AutoTradeState.BUY_RECONCILING, AutoTradeState.SELL_RECONCILING):
            try:
                await reconcile_position(position)
                reconciled += 1
            except Exception as e:
                logger.error("[AutoTrade] reconciliation failed for pos=%s: %s", position.id, e)
    return reconciled


async def run_startup_reconciliation() -> int:
    """§33 -- called once when the worker process starts, before the
    scan/exit loops begin. Resolves anything left mid-flight by a
    previous crash so no position -- or claim -- is ever silently
    orphaned."""
    try:
        await sweep_orphaned_claims()
    except Exception as e:
        logger.error("[AutoTrade] startup orphaned-claim sweep failed: %s", e)

    reconciled = await reconcile_stuck_positions()
    if reconciled:
        logger.info("[AutoTrade] startup reconciliation resolved %s position(s)", reconciled)
    return reconciled
