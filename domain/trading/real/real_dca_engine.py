"""
Real Wallet DCA — fully customizable, interval-based Dollar-Cost-
Averaging for the Real Wallet.

Distinct from paper trading's drawdown-triggered DCA (services/
paper_engine.py's add_dca_fill/check_and_apply_dca + models/
paper_dca_settings.py, models/paper_dca_fill.py), which this file does
not touch. A Real DCA schedule is "buy <amount_sol> of <contract> every
<interval_seconds>, for <total_orders> orders", with optional price
guard rails — configured entirely by the user, no fixed presets.

Every fill reuses services.real_trade_engine.execute_real_buy (source=
"dca") so it lands in the same RealTrade/position/portfolio views as a
manual buy — no duplicated swap logic. Spend is metered through
services.solana_wallet.register_auto_spend/release_auto_spend, sharing
the same daily cap + kill switch as Real Trade Automation
(services/real_automation_engine.py), since both spend unattended.

Ticked by run_due_schedules(), invoked from the real_dca_scheduler_loop
background task registered in main.py (same polling pattern as
services/paper_monitor.paper_monitor_loop).
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from infra.db.session import async_session
from models.real_dca_schedule import RealDCASchedule
from models.real_dca_fill import RealDCAFill
from providers.marketdata.dexscreener import get_token_card_info
from domain.trading.real.solana_wallet import (
    get_real_wallet,
    get_wallet_settings,
    register_auto_spend,
    release_auto_spend,
)
from domain.trading.real import real_trade_engine

logger = logging.getLogger("AlphaPulse.RealDCAEngine")

# After this many consecutive failed/blocked ticks (RPC errors, cap
# reached, kill switch, etc.) a schedule auto-pauses rather than
# retrying forever unattended. The user can resume it manually once
# they've addressed the cause (see last_error on the schedule row).
MAX_CONSECUTIVE_FAILURES = 5

MIN_INTERVAL_SECONDS = 60  # floor, so a typo can't create a sub-minute spam loop
MAX_TOTAL_ORDERS = 200


class DCAValidationError(ValueError):
    """User-facing DCA schedule validation error."""


def _validate_new_schedule(
    amount_per_order_sol: float,
    interval_seconds: int,
    total_orders: int,
    price_floor: float | None,
    price_ceiling: float | None,
) -> None:
    if amount_per_order_sol <= 0:
        raise DCAValidationError("Amount per order must be greater than 0 SOL.")
    if interval_seconds < MIN_INTERVAL_SECONDS:
        raise DCAValidationError(f"Interval must be at least {MIN_INTERVAL_SECONDS} seconds.")
    if not (1 <= total_orders <= MAX_TOTAL_ORDERS):
        raise DCAValidationError(f"Total orders must be between 1 and {MAX_TOTAL_ORDERS}.")
    if price_floor is not None and price_floor <= 0:
        raise DCAValidationError("Price floor must be greater than 0 if set.")
    if price_ceiling is not None and price_ceiling <= 0:
        raise DCAValidationError("Price ceiling must be greater than 0 if set.")
    if price_floor is not None and price_ceiling is not None and price_floor >= price_ceiling:
        raise DCAValidationError("Price floor must be lower than price ceiling.")


async def create_schedule(
    user_id: int,
    contract: str,
    name: str | None,
    symbol: str | None,
    amount_per_order_sol: float,
    interval_seconds: int,
    total_orders: int,
    price_floor: float | None = None,
    price_ceiling: float | None = None,
    slippage_bps: int | None = None,
    priority_fee_tier: str | None = None,
) -> RealDCASchedule:
    wallet = await get_real_wallet(user_id)
    if not wallet:
        raise DCAValidationError("No active Real Wallet. Use /realwallet to set one up.")

    _validate_new_schedule(amount_per_order_sol, interval_seconds, total_orders, price_floor, price_ceiling)

    async with async_session() as session:
        schedule = RealDCASchedule(
            user_id=user_id,
            contract=contract,
            name=name,
            symbol=symbol,
            amount_per_order_sol=amount_per_order_sol,
            interval_seconds=interval_seconds,
            total_orders=total_orders,
            price_floor=price_floor,
            price_ceiling=price_ceiling,
            slippage_bps=slippage_bps,
            priority_fee_tier=priority_fee_tier,
            status="active",
            next_run_at=datetime.now(timezone.utc).replace(tzinfo=None),  # first order fires on the next tick
        )
        session.add(schedule)
        await session.commit()
        await session.refresh(schedule)
        logger.info(f"Created Real DCA schedule {schedule.id} for user {user_id} on {contract}")
        return schedule


async def list_schedules(user_id: int, include_finished: bool = False) -> list[RealDCASchedule]:
    async with async_session() as session:
        query = select(RealDCASchedule).where(RealDCASchedule.user_id == user_id)
        if not include_finished:
            query = query.where(RealDCASchedule.status.in_(["active", "paused"]))
        query = query.order_by(RealDCASchedule.created_at.desc())
        result = await session.execute(query)
        return result.scalars().all()


async def get_schedule(schedule_id: int, user_id: int) -> RealDCASchedule | None:
    async with async_session() as session:
        result = await session.execute(
            select(RealDCASchedule).where(
                RealDCASchedule.id == schedule_id, RealDCASchedule.user_id == user_id
            )
        )
        return result.scalar_one_or_none()


async def pause_schedule(schedule_id: int, user_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(RealDCASchedule).where(
                RealDCASchedule.id == schedule_id, RealDCASchedule.user_id == user_id
            )
        )
        schedule = result.scalar_one_or_none()
        if not schedule or schedule.status not in ("active",):
            return False
        schedule.status = "paused"
        await session.commit()
        return True


async def resume_schedule(schedule_id: int, user_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(RealDCASchedule).where(
                RealDCASchedule.id == schedule_id, RealDCASchedule.user_id == user_id
            )
        )
        schedule = result.scalar_one_or_none()
        if not schedule or schedule.status != "paused":
            return False
        schedule.status = "active"
        schedule.consecutive_failures = 0
        schedule.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        return True


async def cancel_schedule(schedule_id: int, user_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(RealDCASchedule).where(
                RealDCASchedule.id == schedule_id, RealDCASchedule.user_id == user_id
            )
        )
        schedule = result.scalar_one_or_none()
        if not schedule or schedule.status in ("cancelled", "completed"):
            return False
        schedule.status = "cancelled"
        await session.commit()
        return True


async def get_schedule_fills(schedule_id: int, user_id: int, limit: int = 20) -> list[RealDCAFill]:
    async with async_session() as session:
        result = await session.execute(
            select(RealDCAFill)
            .where(RealDCAFill.schedule_id == schedule_id, RealDCAFill.user_id == user_id)
            .order_by(RealDCAFill.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()


async def _record_fill(schedule_id, user_id, order_index, status, reason=None, sol_amount=None,
                        token_quantity=None, price=None, tx_signature=None, real_trade_id=None):
    async with async_session() as session:
        fill = RealDCAFill(
            schedule_id=schedule_id, user_id=user_id, order_index=order_index, status=status,
            reason=reason, sol_amount=sol_amount, token_quantity=token_quantity, price=price,
            tx_signature=tx_signature, real_trade_id=real_trade_id,
        )
        session.add(fill)
        await session.commit()


async def _notify(bot, user_id: int, text: str) -> None:
    if bot is None:
        return
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Could not notify user {user_id} about DCA event: {e}")


async def _advance_or_complete(session, schedule: RealDCASchedule) -> None:
    if schedule.orders_filled >= schedule.total_orders:
        schedule.status = "completed"
    else:
        schedule.next_run_at = (datetime.now(timezone.utc) + timedelta(seconds=schedule.interval_seconds)).replace(tzinfo=None)
    await session.commit()


async def _process_one_schedule(schedule_id: int, bot=None) -> None:
    """Re-fetches the schedule fresh (rather than reusing the row from
    the due-list query) so this is safe to call concurrently across
    many due schedules without holding one long-lived session open."""
    async with async_session() as session:
        result = await session.execute(select(RealDCASchedule).where(RealDCASchedule.id == schedule_id))
        schedule = result.scalar_one_or_none()
        if not schedule or schedule.status != "active":
            return

    order_index = schedule.orders_filled + 1

    # Price guard rails — skip this tick, retry next interval, don't
    # count against consecutive_failures (this isn't a fault).
    price = None
    try:
        info = await get_token_card_info(schedule.contract)
        if info and info.get("price") not in (None, "N/A"):
            price = float(info["price"])
    except Exception as e:
        logger.warning(f"Real DCA price lookup failed for schedule {schedule_id}: {e}")

    if price is None:
        async with async_session() as session:
            result = await session.execute(select(RealDCASchedule).where(RealDCASchedule.id == schedule_id))
            schedule = result.scalar_one_or_none()
            schedule.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
            schedule.last_error = "Could not fetch a live price this tick — retrying next interval."
            await _advance_or_complete(session, schedule)
        await _record_fill(schedule_id, schedule.user_id, order_index, "skipped", reason="no_price")
        return

    if schedule.price_floor is not None and price < schedule.price_floor:
        reason = f"Price {price:.10f} below floor {schedule.price_floor:.10f}"
    elif schedule.price_ceiling is not None and price > schedule.price_ceiling:
        reason = f"Price {price:.10f} above ceiling {schedule.price_ceiling:.10f}"
    else:
        reason = None

    if reason:
        async with async_session() as session:
            result = await session.execute(select(RealDCASchedule).where(RealDCASchedule.id == schedule_id))
            schedule = result.scalar_one_or_none()
            schedule.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
            schedule.last_error = reason
            await _advance_or_complete(session, schedule)
        await _record_fill(schedule_id, schedule.user_id, order_index, "skipped", reason=reason, price=price)
        return

    # Daily cap / kill switch / automation-off gate — shared with
    # real_automation_engine.py via services.solana_wallet.
    spend_check = await register_auto_spend(schedule.user_id, schedule.amount_per_order_sol)
    if not spend_check["ok"]:
        await _handle_failure(schedule_id, order_index, spend_check["reason"], bot)
        return

    settings = await get_wallet_settings(schedule.user_id)
    slippage_bps = schedule.slippage_bps if schedule.slippage_bps is not None else settings["slippage_bps"]
    priority_fee_tier = schedule.priority_fee_tier or settings["priority_fee_tier"]

    result = await real_trade_engine.execute_real_buy(
        user_id=schedule.user_id,
        contract=schedule.contract,
        name=schedule.name or "",
        symbol=schedule.symbol or "???",
        current_price=price,
        sol_amount=schedule.amount_per_order_sol,
        slippage_bps=slippage_bps,
        priority_fee_tier=priority_fee_tier,
        source="dca",
    )

    if not result["ok"]:
        await release_auto_spend(schedule.user_id, schedule.amount_per_order_sol)
        await _handle_failure(schedule_id, order_index, result["reason"], bot)
        return

    trade = result["trade"]
    async with async_session() as session:
        db_result = await session.execute(select(RealDCASchedule).where(RealDCASchedule.id == schedule_id))
        schedule = db_result.scalar_one_or_none()
        schedule.orders_filled += 1
        schedule.consecutive_failures = 0
        schedule.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
        schedule.last_error = None
        await _advance_or_complete(session, schedule)

    await _record_fill(
        schedule_id, schedule.user_id, order_index, "filled",
        sol_amount=schedule.amount_per_order_sol, token_quantity=trade.token_quantity,
        price=price, tx_signature=result["signature"], real_trade_id=trade.id,
    )

    completed_note = " — <b>schedule complete 🎉</b>" if schedule.status == "completed" else ""
    await _notify(
        bot, schedule.user_id,
        f"🧬 <b>DCA order {order_index}/{schedule.total_orders} filled</b> — "
        f"bought {trade.token_quantity:,.2f} {trade.symbol or ''} for "
        f"{schedule.amount_per_order_sol:.4f} SOL.{completed_note}",
    )


async def _handle_failure(schedule_id: int, order_index: int, reason: str, bot=None) -> None:
    async with async_session() as lookup_session:
        lookup_result = await lookup_session.execute(
            select(RealDCASchedule).where(RealDCASchedule.id == schedule_id)
        )
        lookup_schedule = lookup_result.scalar_one_or_none()
        if not lookup_schedule:
            return
        owner_user_id = lookup_schedule.user_id

    await _record_fill(schedule_id, owner_user_id, order_index, "failed", reason=reason)

    async with async_session() as session:
        result = await session.execute(select(RealDCASchedule).where(RealDCASchedule.id == schedule_id))
        schedule = result.scalar_one_or_none()
        if not schedule:
            return
        schedule.consecutive_failures += 1
        schedule.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
        schedule.last_error = reason
        user_id = schedule.user_id
        total_orders = schedule.total_orders

        if schedule.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            schedule.status = "paused"
            await session.commit()
            await _notify(
                bot, user_id,
                f"⏸️ <b>DCA schedule auto-paused</b> for {schedule.symbol or schedule.contract[:8]} "
                f"after {MAX_CONSECUTIVE_FAILURES} failed orders in a row.\n"
                f"Last reason: {reason}\n\nResume it from /realwallet once resolved."
            )
            return

        schedule.next_run_at = (datetime.now(timezone.utc) + timedelta(seconds=schedule.interval_seconds)).replace(tzinfo=None)
        await session.commit()


async def run_due_schedules(bot=None) -> int:
    """
    One scheduler tick: processes every active schedule whose
    next_run_at has arrived. Returns the number processed. Never raises
    — a single schedule's unexpected error is logged and skipped so it
    can't take down the whole loop for every other user.
    """
    async with async_session() as session:
        result = await session.execute(
            select(RealDCASchedule.id).where(
                RealDCASchedule.status == "active",
                RealDCASchedule.next_run_at <= datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        due_ids = [row[0] for row in result.all()]

    for schedule_id in due_ids:
        try:
            await _process_one_schedule(schedule_id, bot=bot)
        except Exception as e:
            logger.error(f"Real DCA schedule {schedule_id} tick failed unexpectedly: {e}")

    return len(due_ids)


async def real_dca_scheduler_loop(bot=None, interval_seconds: int = 30):
    """Background task registered in main.py — same shape as
    services.paper_monitor.paper_monitor_loop."""
    import asyncio
    logger.info("🧬 Real DCA scheduler loop starting...")
    while True:
        try:
            await run_due_schedules(bot=bot)
        except Exception as e:
            logger.error(f"Real DCA scheduler loop error (non-fatal): {e}")
        await asyncio.sleep(interval_seconds)
