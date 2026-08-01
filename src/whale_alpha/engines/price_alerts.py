"""% price-move alerts — feature that had zero code path before this port:
no price-polling loop, no threshold check, no alert message existed anywhere.

A user creates a PriceAlert (see bot/commands/alerts.py: /alert) pinning a
`reference_price_usd` baseline and a `threshold_pct`. This loop polls every
`env.PRICE_ALERT_INTERVAL_SECONDS`, and for each active alert compares the
current price to the baseline. When the move crosses the threshold (in the
configured direction) and the per-alert cooldown has elapsed, it DMs the user
and — if `reset_on_trigger` — rebases `reference_price_usd` to the current
price so the next alert measures the *next* move, not the same one repeatedly.

Same asyncio-task-and-sleep-loop shape as engines/scheduler.py, for the same
reason (see that module's docstring re: swapping in arq cron/celery beat once
running multiple workers).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from whale_alpha.config import Env
from whale_alpha.db.models import PriceAlert, User
from whale_alpha.integrations import price_feed
from whale_alpha.utils.logger import child_logger

log = child_logger("priceAlerts")


def start_price_alert_loop(
    env: Env, session_factory: async_sessionmaker, bot: Bot, http_client: httpx.AsyncClient
):
    async def _loop() -> None:
        while True:
            await asyncio.sleep(env.PRICE_ALERT_INTERVAL_SECONDS)
            try:
                await _check_all_alerts(env, session_factory, bot, http_client)
            except Exception as err:  # noqa: BLE001 — mirrors scheduler.py's catch-all
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


async def _check_all_alerts(
    env: Env, session_factory: async_sessionmaker, bot: Bot, http_client: httpx.AsyncClient
) -> None:
    async with session_factory() as session:
        result = await session.execute(select(PriceAlert).where(PriceAlert.active.is_(True)))
        alerts = list(result.scalars())

    if not alerts:
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
                f"📈 *Price Alert*\n\n"
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
