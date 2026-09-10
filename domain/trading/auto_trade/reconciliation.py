"""domain/trading/auto_trade/reconciliation.py

§25 (Reconciliation Rules) and §33 (Crash Recovery). "The blockchain is
the source of truth" -- this module resolves any AutoTradePosition or
AutoTradeClaim left in a non-terminal / uncertain state (typically
after a worker crash or restart mid-trade) by checking actual on-chain
outcomes before allowing anything to proceed.

Runs once at worker startup (see worker.py) and can also be invoked
periodically to sweep BUY_RECONCILING / SELL_RECONCILING positions that
a single failed confirmation poll left dangling.
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

logger = logging.getLogger("AlphaPulse.AutoTrade.Reconciliation")


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
                await claims.release_claim(position.claim_id)
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
                await claims.release_claim(position.claim_id)
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
    path in between already calls claims.release_claim() to flip the
    claim back to "failed" so it can be retried on the signal's next
    evaluation. The only way a claim is left "pending" forever with no
    AutoTradePosition at all is a hard process crash inside that narrow
    window (not a caught exception -- those already release the claim).

    A claim like that can never resolve itself: claims.try_claim's own
    grace-period retry only reopens a claim once its status is "failed"
    (see _claim_insert_or_reopen's `where=(AutoTradeClaim.status ==
    "failed")`), and nothing else ever sets a "pending" claim to "failed"
    except claims.release_claim() -- which nothing is left to call once
    the process that would have called it is gone. Sweep those here, the
    same way reconcile_position() above resolves positions left
    mid-flight: any claim still "pending" past the grace period with no
    matching position is definitively abandoned -- if anything were
    still executing it, it would already have created that position row
    -- so it's safe to flip to "failed" and let it be retried normally.
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


async def run_startup_reconciliation() -> int:
    """§33 -- called once when the worker process starts, before the
    scan/exit loops begin. Resolves anything left mid-flight by a
    previous crash so no position -- or claim -- is ever silently
    orphaned."""
    try:
        await sweep_orphaned_claims()
    except Exception as e:
        logger.error("[AutoTrade] startup orphaned-claim sweep failed: %s", e)

    positions = await position_manager.get_all_non_terminal_positions()
    reconciled = 0
    for position in positions:
        if position.state in (AutoTradeState.BUY_RECONCILING, AutoTradeState.SELL_RECONCILING):
            try:
                await reconcile_position(position)
                reconciled += 1
            except Exception as e:
                logger.error("[AutoTrade] startup reconciliation failed for pos=%s: %s", position.id, e)
    if reconciled:
        logger.info("[AutoTrade] startup reconciliation resolved %s position(s)", reconciled)
    return reconciled
