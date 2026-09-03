"""
Real Wallet Limit Orders — unattended buy-on-trigger engine, available
to every user.

Same design as services/real_exit_engine.py: a standalone table
(models/real_limit_order.py) and one shared execution primitive
(services.real_trade_engine.execute_real_buy). Before firing, a
pending order is atomically claimed (status pending -> filling) so a
slow fill can't be double-triggered by an overlapping tick, and so a
user cancelling at the same moment an order is about to fire can't
race the fill.

Spend is metered through the same daily cap + kill switch as Real
Trade Automation / DCA (services.solana_wallet.register_auto_spend),
since this also spends unattended.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from infra.db.session import async_session
from models.real_limit_order import RealLimitOrder
from providers.marketdata.dexscreener import get_token_card_info
from domain.trading.real.solana_wallet import get_wallet_settings, register_auto_spend, release_auto_spend
from domain.trading.real import real_trade_engine

logger = logging.getLogger("AlphaPulse.RealLimitOrderEngine")

VALID_DIRECTIONS = {"buy_below", "buy_above"}
MAX_OPEN_ORDERS_PER_USER = 20


class LimitOrderValidationError(ValueError):
    """User-facing limit order validation error."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_order(
    user_id: int,
    contract: str,
    name: str | None,
    symbol: str | None,
    direction: str,
    trigger_price: float,
    sol_amount: float,
) -> RealLimitOrder:
    if direction not in VALID_DIRECTIONS:
        raise LimitOrderValidationError("Unknown order direction.")
    if trigger_price <= 0:
        raise LimitOrderValidationError("Trigger price must be greater than 0.")
    if sol_amount <= 0:
        raise LimitOrderValidationError("Amount must be greater than 0 SOL.")

    async with async_session() as session:
        count_result = await session.execute(
            select(RealLimitOrder.id).where(
                RealLimitOrder.user_id == user_id, RealLimitOrder.status == "pending"
            )
        )
        if len(count_result.all()) >= MAX_OPEN_ORDERS_PER_USER:
            raise LimitOrderValidationError(
                f"You can have at most {MAX_OPEN_ORDERS_PER_USER} open limit orders at once."
            )

        order = RealLimitOrder(
            user_id=user_id,
            contract=contract,
            name=name,
            symbol=symbol,
            direction=direction,
            trigger_price=trigger_price,
            sol_amount=sol_amount,
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
    return order


async def get_open_orders(user_id: int) -> list[RealLimitOrder]:
    async with async_session() as session:
        result = await session.execute(
            select(RealLimitOrder)
            .where(RealLimitOrder.user_id == user_id, RealLimitOrder.status == "pending")
            .order_by(RealLimitOrder.created_at.desc())
        )
        return result.scalars().all()


async def get_order_history(user_id: int, limit: int = 20) -> list[RealLimitOrder]:
    async with async_session() as session:
        result = await session.execute(
            select(RealLimitOrder)
            .where(RealLimitOrder.user_id == user_id, RealLimitOrder.status != "pending")
            .order_by(RealLimitOrder.updated_at.desc())
            .limit(limit)
        )
        return result.scalars().all()


async def cancel_order(user_id: int, order_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(RealLimitOrder).where(RealLimitOrder.id == order_id, RealLimitOrder.user_id == user_id)
        )
        order = result.scalar_one_or_none()
        if not order or order.status != "pending":
            return False
        order.status = "cancelled"
        await session.commit()
    return True


def _condition_met(direction: str, current_price: float, trigger_price: float) -> bool:
    if direction == "buy_below":
        return current_price <= trigger_price
    return current_price >= trigger_price


async def _notify(bot, user_id: int, text: str) -> None:
    if bot is None:
        return
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Could not notify user {user_id} about a limit order: {e}")


async def _fill_order(bot, order: RealLimitOrder, current_price: float) -> None:
    spend_check = await register_auto_spend(order.user_id, order.sol_amount)
    if not spend_check["ok"]:
        # Daily cap / kill switch — release the claim so the order goes
        # back to "pending" and is retried on a later tick, same as
        # automation/DCA.
        async with async_session() as session:
            await session.execute(
                update(RealLimitOrder)
                .where(RealLimitOrder.id == order.id, RealLimitOrder.status == "filling")
                .values(status="pending")
            )
            await session.commit()
        return

    settings = await get_wallet_settings(order.user_id)
    result = await real_trade_engine.execute_real_buy(
        user_id=order.user_id,
        contract=order.contract,
        name=order.name or "",
        symbol=order.symbol or "???",
        current_price=current_price,
        sol_amount=order.sol_amount,
        slippage_bps=order.slippage_bps or settings["slippage_bps"],
        priority_fee_tier=order.priority_fee_tier or settings["priority_fee_tier"],
        source="limit_order",
    )

    async with async_session() as session:
        result_row = await session.execute(select(RealLimitOrder).where(RealLimitOrder.id == order.id))
        row = result_row.scalar_one_or_none()
        if not row:
            if not result["ok"]:
                await release_auto_spend(order.user_id, order.sol_amount)
            return
        if result["ok"]:
            row.status = "filled"
            row.tx_signature = result["signature"]
            row.filled_at = _now()
        else:
            await release_auto_spend(order.user_id, order.sol_amount)
            row.status = "failed"
            row.last_error = result["reason"]
        await session.commit()

    if result["ok"]:
        trade = result["trade"]
        await _notify(
            bot, order.user_id,
            f"🎯 <b>Limit order filled — {trade.symbol or ''}</b>\n"
            f"Triggered at ${current_price:.8f} (target ${order.trigger_price:.8f})\n"
            f"Spent: {trade.sol_spent:.4f} SOL\n"
            f"Tx: <code>{result['signature']}</code>\n\n"
            f"Manage this position from /realwallet.",
        )
    else:
        await _notify(
            bot, order.user_id,
            f"⚠️ Limit order on {order.symbol or order.contract[:6]} triggered but the buy failed: "
            f"{result['reason']}",
        )


async def scan_and_execute(bot=None) -> int:
    """One limit-order engine tick. Returns the number of orders filled/attempted."""
    async with async_session() as session:
        result = await session.execute(
            select(RealLimitOrder).where(RealLimitOrder.status == "pending")
        )
        orders = result.scalars().all()

    if not orders:
        return 0

    now = _now()
    price_cache: dict[str, float | None] = {}
    attempts = 0

    for order in orders:
        if order.expires_at and order.expires_at <= now:
            async with async_session() as session:
                result_row = await session.execute(select(RealLimitOrder).where(RealLimitOrder.id == order.id))
                row = result_row.scalar_one_or_none()
                if row and row.status == "pending":
                    row.status = "expired"
                    await session.commit()
            continue

        if order.contract not in price_cache:
            price = None
            try:
                info = await get_token_card_info(order.contract)
                if info and info.get("price") not in (None, "N/A"):
                    price = float(info["price"])
            except Exception:
                pass
            price_cache[order.contract] = price

        current_price = price_cache[order.contract]
        if current_price is None:
            continue

        if _condition_met(order.direction, current_price, order.trigger_price):
            # Atomically claim the order (pending -> filling) before
            # spending/swapping. If another tick or a concurrent cancel
            # already moved it out of "pending", rowcount is 0 and we
            # skip it — this is what prevents the same order from being
            # filled twice (e.g. if a swap takes longer than the tick
            # interval) and stops a fill from racing a user's cancel.
            async with async_session() as session:
                claim = await session.execute(
                    update(RealLimitOrder)
                    .where(RealLimitOrder.id == order.id, RealLimitOrder.status == "pending")
                    .values(status="filling")
                )
                await session.commit()
                if claim.rowcount == 0:
                    continue

            try:
                await _fill_order(bot, order, current_price)
                attempts += 1
            except Exception as e:
                logger.error(f"Limit order {order.id} failed to fill: {e}")
                async with async_session() as session:
                    await session.execute(
                        update(RealLimitOrder)
                        .where(RealLimitOrder.id == order.id, RealLimitOrder.status == "filling")
                        .values(status="pending")
                    )
                    await session.commit()

    return attempts


async def real_limit_order_engine_loop(bot=None, interval_seconds: int = 20):
    """Background task registered in main.py — same shape as
    services.real_automation_engine.real_automation_loop."""
    logger.info("🎯 Real Wallet Limit Order Engine starting...")
    while True:
        try:
            await scan_and_execute(bot=bot)
        except Exception as e:
            logger.error(f"Real limit order engine loop error (non-fatal): {e}")
        await asyncio.sleep(interval_seconds)
