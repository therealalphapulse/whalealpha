"""% price-move alerts â feature that had zero code path before this port:
no price-polling loop, no threshold check, no alert message existed anywhere.

A user creates a PriceAlert (see bot/commands/alerts.py: /alert) pinning a
`reference_price_usd` baseline and a `threshold_pct`. This loop polls every
`env.PRICE_ALERT_INTERVAL_SECONDS`, and for each active alert compares the
current price to the baseline. When the move crosses the threshold (in the
configured direction) and the per-alert cooldown has elapsed, it DMs the user
and â if `reset_on_trigger` â rebases `reference_price_usd` to the current
price so the next alert measures the *next* move, not the same one repeatedly.

Same asyncio-task-and-sleep-loop shape as engines/scheduler.py, for the same
reason (see that module's docstring re: swapping in arq cron/celery beat once
running multiple workers).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from whale_alpha.config import Env
from whale_alpha.db.models import PriceAlert, TokenOpportunity, User
from whale_alpha.integrations import price_feed
from whale_alpha.utils.logger import child_logger

log = child_logger("priceAlerts")


def start_price_alert_loop(
    env: Env, session_factory: async_sessionmaker[AsyncSession], bot: Bot, http_client: httpx.AsyncClient
) -> Callable[[], Awaitable[None]]:
    async def _loop() -> None:
        while True:
            await asyncio.sleep(env.PRICE_ALERT_INTERVAL_SECONDS)
            try:
                await _check_all_alerts(env, session_factory, bot, http_client)
            except Exception as err:  # noqa: BLE001 â mirrors scheduler.py's catch-all
                log.error("Price alert check cycle failed", err=str(err))

    task = asyncio.create_task(_loop())

    async def stop() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return stop


def _direction_matches(direction: str, pct_change: float) -> bool:
    if direction == "UP":
        return pct_change > 0
    if direction == "DOWN":
        return pct_change < 0
    return True  # BOTH


MILESTONE_THRESHOLDS: tuple[tuple[int, float], ...] = (
    (25, 1.25),
    (50, 1.50),
    (75, 1.75),
    (100, 2.00),
    (200, 3.00),
    (300, 4.00),
    (400, 5.00),
)


def crossed_milestones(current_pct: float, sent: set[int]) -> list[int]:
    return [milestone_pct for milestone_pct, _ in MILESTONE_THRESHOLDS if milestone_pct not in sent and current_pct >= milestone_pct]


def _milestone_text(opportunity: TokenOpportunity, milestone_pct: int, current_price: float) -> str:
    reference = opportunity.alert_reference_price_usd or 0.0
    pct = ((current_price - reference) / reference * 100) if reference > 0 else float(milestone_pct)
    multiple = current_price / reference if reference > 0 else 1.0 + milestone_pct / 100.0
    return (
        f"ð <b>${opportunity.symbol or 'UNKNOWN'} reached {milestone_pct}%</b>\n\n"
        f"ð¥ From initial signal: <b>+{pct:.1f}%</b>\n"
        f"ð Multiple: <b>{multiple:.2f}x</b>\n\n"
        f"ð° Signal price: <b>${reference:.10g}</b>\n"
        f"ð Current price: <b>${current_price:.10g}</b>\n\n"
        f"<code>{opportunity.mint}</code>\n\n"
        f"<i>Whale Alpha â¢ Milestone Alert</i>"
    )


async def _check_signal_milestones(
    env: Env, session_factory: async_sessionmaker[AsyncSession], bot: Bot, http_client: httpx.AsyncClient
) -> None:
    async with session_factory() as session:
        result = await session.execute(
            select(TokenOpportunity).where(
                TokenOpportunity.alert_delivered_at.is_not(None),
                TokenOpportunity.alert_reference_price_usd.is_not(None),
            )
        )
        opportunities = list(result.scalars().all())
        if not opportunities:
            return

        mints = [
            o.mint for o in opportunities
            if (o.alert_reference_price_usd or 0) > 0 and o.alert_message_ids
        ]
        if not mints:
            return
        prices = await price_feed.get_prices_usd(http_client, env, mints)
        now = datetime.now(UTC)

        for opportunity in opportunities:
            reference = opportunity.alert_reference_price_usd or 0.0
            message_ids = opportunity.alert_message_ids or {}
            current = prices.get(opportunity.mint)
            if reference <= 0 or not message_ids or current is None or current <= 0:
                continue

            current_pct = (current - reference) / reference * 100
            sent = {int(x) for x in (opportunity.quote_milestones or [])}
            crossed = crossed_milestones(current_pct, sent)
            for milestone_pct in crossed:
                text = _milestone_text(opportunity, milestone_pct, current)
                delivered_any = False
                for chat_id, original_message_id in message_ids.items():
                    try:
                        await bot.send_message(
                            chat_id=int(chat_id),
                            text=text,
                            parse_mode="HTML",
                            reply_to_message_id=int(original_message_id),
                            allow_sending_without_reply=True,
                        )
                        delivered_any = True
                    except (TelegramAPIError, ValueError) as err:
                        log.warning(
                            "Failed to deliver signal milestone",
                            mint=opportunity.mint,
                            milestone_pct=milestone_pct,
                            chat_id=chat_id,
                            err=str(err),
                        )
                # Persist immediately on the attached row before processing the next
                # threshold. This prevents the same milestone from being emitted again
                # on the next polling cycle after a successful Telegram delivery.
                if delivered_any:
                    sent.add(milestone_pct)
                    opportunity.quote_milestones = sorted(sent)
                    opportunity.last_seen_at = now
                    await session.flush()
                    log.info(
                        "Signal milestone fired",
                        mint=opportunity.mint,
                        milestone_pct=milestone_pct,
                        current_pct=current_pct,
                    )
        await session.commit()


async def _check_all_alerts(
    env: Env, session_factory: async_sessionmaker[AsyncSession], bot: Bot, http_client: httpx.AsyncClient
) -> None:
    async with session_factory() as session:
        result = await session.execute(select(PriceAlert).where(PriceAlert.active.is_(True)))
        alerts = list(result.scalars())

    if not alerts:
        await _check_signal_milestones(env, session_factory, bot, http_client)
        return

    mints = list({a.token_mint for a in alerts})
    prices = await price_feed.get_prices_usd(http_client, env, mints)

    cooldown = timedelta(minutes=env.PRICE_ALERT_MIN_COOLDOWN_MINUTES)
    now = datetime.now(UTC)

    async with session_factory() as session:
        for alert in alerts:
            current_price = prices.get(alert.token_mint)
            if current_price is None or alert.reference_price_usd <= 0:
                continue

            pct_change = ((current_price - alert.reference_price_usd) / alert.reference_price_usd) * 100

            if abs(pct_change) < alert.threshold_pct:
                continue
            if not _direction_matches(alert.direction.value, pct_change):
                continue
            if alert.last_triggered_at is not None and now - alert.last_triggered_at < cooldown:
                continue

            row = await session.get(PriceAlert, alert.id)
            if row is None or not row.active:
                continue

            direction_word = "up" if pct_change > 0 else "down"
            text = (
                f"ð *Price Alert*\n\n"
                f"`{alert.token_mint[:4]}...{alert.token_mint[-4:]}` is {direction_word} "
                f"*{abs(pct_change):.1f}%* from your reference price.\n\n"
                f"Reference: ${alert.reference_price_usd:.6f}\n"
                f"Current: ${current_price:.6f}\n\n"
                f"Full mint: `{alert.token_mint}`"
            )

            try:
                user = await session.get(User, alert.user_id)
                if user is None:
                    continue
                await bot.send_message(chat_id=int(user.telegram_id), text=text, parse_mode="Markdown")
                log.info("Price alert fired", alert_id=alert.id, pct_change=pct_change)
            except (TelegramAPIError, ValueError) as err:
                log.warning("Failed to deliver price alert", alert_id=alert.id, err=str(err))
                continue

            row.last_triggered_at = now
            if row.reset_on_trigger:
                row.reference_price_usd = current_price

        await session.commit()

    await _check_signal_milestones(env, session_factory, bot, http_client)
