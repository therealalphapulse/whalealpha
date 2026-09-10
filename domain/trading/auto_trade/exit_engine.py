"""domain/trading/auto_trade/exit_engine.py

§17 (Take-Profit), §18 (Stop-Loss), §19 (Trailing Stop), §20 (Exit
Priority), §21 (Exit State Machine), §22 (Sellable Balance
Resolution). Runs once per open position per tick of the monitor loop.

Exit priority (§20): stop-loss first (capital preservation), then
trailing stop, then take-profit -- if multiple would fire on the same
tick, the most protective one wins.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from infra.db.session import async_session
from models.auto_trade_position import AutoTradePosition, AutoTradeState
from models.auto_trade_execution import AutoTradeExecution

from . import execution, position_manager

logger = logging.getLogger("AlphaPulse.AutoTrade.ExitEngine")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _check_exit_trigger(position: AutoTradePosition, current_price: float) -> str | None:
    """Returns "sl" | "trailing" | "tp" | None. Pure function, no I/O,
    easy to unit test in isolation from the network (§20/§21)."""
    from .policy_service import TradePolicySnapshot
    snapshot = TradePolicySnapshot.from_json(position.policy_snapshot_json)
    entry_price = float(position.entry_price or 0.0)
    if entry_price <= 0 or current_price <= 0:
        return None
    change_pct = (current_price - entry_price) / entry_price * 100.0

    sl_pct = snapshot.stop_loss_pct if snapshot else None
    if sl_pct is not None and change_pct <= -abs(sl_pct):
        return "sl"

    if snapshot and snapshot.trailing_stop_enabled:
        # Trailing Retracement (%): the new user-configurable pullback
        # threshold from the peak. Falls back to the legacy
        # trailing_stop_pct when unset, so any policy/snapshot created
        # before this field existed keeps triggering at exactly the same
        # distance as before -- no change to already-configured trades.
        retracement_pct = (
            snapshot.trailing_retracement_pct
            if snapshot.trailing_retracement_pct
            else snapshot.trailing_stop_pct
        )
        if retracement_pct:
            highest = float(position.highest_observed_price or entry_price)
            if current_price > highest:
                highest = current_price

            # Trailing Activation (%): minimum gain from entry required
            # before trailing arms and the retracement check applies.
            # Unset/0 preserves the legacy behavior of arming on any gain
            # above entry.
            activation_pct = abs(snapshot.trailing_activation_pct) if snapshot.trailing_activation_pct else 0.0
            activation_price = entry_price * (1 + activation_pct / 100.0)
            armed = highest > entry_price and highest >= activation_price

            if armed:
                trigger_price = highest * (1 - abs(retracement_pct) / 100.0)
                if current_price <= trigger_price:
                    return "trailing"

    tp_pct = snapshot.take_profit_pct if snapshot else None
    if tp_pct is not None and change_pct >= abs(tp_pct):
        return "tp"

    return None


async def _update_trailing_high(position_id: int, current_price: float, highest_so_far: float) -> None:
    if current_price <= highest_so_far:
        return
    async with async_session() as session:
        db_position = await session.get(AutoTradePosition, position_id)
        if db_position and (db_position.highest_observed_price or 0.0) < current_price:
            db_position.highest_observed_price = current_price
            await session.commit()


async def execute_exit(bot, position: AutoTradePosition, reason: str) -> None:
    """§21/§23/§24 -- sell-side state machine for one position."""
    user_id = position.user_id
    await position_manager.set_state(position.id, AutoTradeState.SELL_BALANCE_REETHVING)

    balance = await position_manager.resolve_sellable_balance(user_id, position)
    if not balance["ok"]:
        await position_manager.set_state(position.id, AutoTradeState.SELL_BALANCE_REETHVING, last_error=balance.get("reason"))
        logger.warning("[AutoTrade] sellable balance unresolved, will retry, pos=%s: %s", position.id, balance.get("reason"))
        return

    sellable = balance["sellable_amount"]
    if sellable <= 0:
        await position_manager.set_state(
            position.id, AutoTradeState.CLOSED, remaining_quantity=0.0, closed_at=_now(),
            exit_reason=reason, last_error="Zero on-chain balance at exit time.",
        )
        logger.warning("[AutoTrade] position %s closed with zero on-chain balance at exit", position.id)
        return

    from .policy_service import TradePolicySnapshot
    snapshot = TradePolicySnapshot.from_json(position.policy_snapshot_json)
    slippage_bps = snapshot.slippage_bps if snapshot else 150
    priority_fee_tier = snapshot.priority_fee_tier if snapshot else "auto"

    await position_manager.set_state(position.id, AutoTradeState.SELL_SUBMITTED, exit_reason=reason)

    result = await execution.execute_sell_swap(
        user_id=user_id, contract=position.contract, token_amount=sellable,
        decimals=position.token_decimals or 0, slippage_bps=slippage_bps, priority_fee_tier=priority_fee_tier,
    )

    if not result["ok"]:
        if result.get("uncertain"):
            await position_manager.set_state(position.id, AutoTradeState.SELL_RECONCILING, last_error=result["reason"])
            logger.warning("[AutoTrade] sell outcome uncertain pos=%s: %s", position.id, result["reason"])
        else:
            retry_count = (position.retry_count or 0) + 1
            await position_manager.set_state(
                position.id, AutoTradeState.POSITION_OPEN, last_error=result["reason"], retry_count=retry_count,
            )
            logger.warning("[AutoTrade] sell failed (will retry), pos=%s attempt=%s: %s", position.id, retry_count, result["reason"])
        return

    sol_received = result["sol_received"]
    amount_sold = result["actual_amount_sold"]
    remaining = max(0.0, float(position.remaining_quantity or 0.0) - amount_sold)
    cost_basis_sold = float(position.total_cost_basis_sol or 0.0) * (
        amount_sold / float(position.token_quantity or amount_sold or 1.0)
    )
    realized_pnl = sol_received - cost_basis_sold
    is_full_exit = remaining <= max(1e-9, float(position.token_quantity or 0.0) * 0.001)

    # §17/§20 settlement-time proof: the trigger fired "tp" from a
    # pre-trade price estimate, but only the REAL on-chain proceeds
    # determine whether a profit was actually taken. Never let the
    # reported/persisted reason say "Take-Profit" unless the realized
    # economics (sol_received vs cost_basis_sold, the actual settled
    # numbers) clear the configured TP threshold. This check is
    # independent of whatever price source produced the trigger, so it
    # still catches a bad "tp" call even if that source is stale,
    # unit-mismatched, or drifted from execution via slippage/price
    # impact.
    if reason == "tp":
        tp_pct = snapshot.take_profit_pct if snapshot else None
        realized_pct = (realized_pnl / cost_basis_sold * 100.0) if cost_basis_sold > 0 else None
        tp_condition_met = (
            realized_pct is not None and tp_pct is not None and realized_pct >= abs(tp_pct)
        )
        if not tp_condition_met:
            logger.warning(
                "[AutoTrade] pos=%s trigger=tp but realized_pct=%s does not meet tp_pct=%s "
                "(cost_basis_sold=%.6f sol_received=%.6f) -- reporting as tp_shortfall, not Take-Profit",
                position.id, realized_pct, tp_pct, cost_basis_sold, sol_received,
            )
            reason = "tp_shortfall"

    async with async_session() as session:
        db_position = await session.get(AutoTradePosition, position.id)
        db_position.remaining_quantity = remaining
        db_position.realized_proceeds_sol = float(db_position.realized_proceeds_sol or 0.0) + sol_received
        db_position.realized_pnl_sol = float(db_position.realized_pnl_sol or 0.0) + realized_pnl
        db_position.average_exit_price = (sol_received / amount_sold) if amount_sold > 0 else None
        cost_so_far = float(position.total_cost_basis_sol or 0.0)
        db_position.realized_pnl_pct = (
            (db_position.realized_pnl_sol / cost_so_far * 100.0) if cost_so_far > 0 else None
        )
        db_position.state = AutoTradeState.CLOSED if is_full_exit else AutoTradeState.PARTIALLY_CLOSED
        db_position.exit_reason = reason
        if is_full_exit:
            db_position.closed_at = _now()
        session.add(AutoTradeExecution(
            position_id=position.id, user_id=user_id, side="sell", contract=position.contract,
            requested_amount=sellable, actual_amount=amount_sold,
            quote_amount=sol_received, transaction_signature=result["signature"],
            provider="jupiter", status="confirmed_success", confirmed_at=_now(),
        ))
        await session.commit()

    reason_label = {
        "tp": "Take-Profit", "tp_shortfall": "Exit (TP not met)",
        "sl": "Stop-Loss", "trailing": "Trailing Stop", "manual": "Manual",
    }.get(reason, reason)
    logger.info(
        "[AutoTrade][trade=%s] SELL_CONFIRMED user=%s contract=%s reason=%s proceeds=%.4f ETH pnl=%.4f ETH",
        position.id, user_id, position.contract, reason, sol_received, realized_pnl,
    )
    if bot is not None:
        try:
            await bot.send_message(
                user_id,
                f"🏁 <b>Auto-Trade Closed ({reason_label})</b>\n"
                f"Token: {position.symbol}\n"
                f"Proceeds: {sol_received:.4f} ETH\n"
                f"PnL: {'+' if realized_pnl >= 0 else ''}{realized_pnl:.4f} ETH",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("[AutoTrade] could not notify user %s of exit: %s", user_id, e)


async def monitor_and_exit(bot=None) -> int:
    """One tick of the exit-monitor loop (§40). Evaluates every open
    position across all users; returns the number evaluated."""
    from sqlalchemy import select
    async with async_session() as session:
        result = await session.execute(
            select(AutoTradePosition).where(AutoTradePosition.state.in_({
                AutoTradeState.POSITION_OPEN, AutoTradeState.PARTIALLY_CLOSED,
            }))
        )
        open_positions = result.scalars().all()

    if not open_positions:
        return 0

    view_by_user: dict[int, list[dict]] = {}
    evaluated = 0
    for position in open_positions:
        evaluated += 1
        if position.user_id not in view_by_user:
            view_by_user[position.user_id] = await position_manager.get_live_positions_view(position.user_id)
        views = {v["position"].id: v for v in view_by_user[position.user_id]}
        view = views.get(position.id)
        if not view or view["price_stale"]:
            continue
        current_price = view["current_price"]
        await _update_trailing_high(position.id, current_price, float(position.highest_observed_price or position.entry_price or 0.0))
        trigger = _check_exit_trigger(position, current_price)
        if trigger:
            try:
                await execute_exit(bot, position, trigger)
            except Exception as e:
                logger.error("[AutoTrade] unhandled error exiting position=%s: %s", position.id, e)
    return evaluated
