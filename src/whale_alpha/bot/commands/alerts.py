"""/alert, /alerts, /removealert — user-facing side of the % price-increase
alert feature (see engines/price_alerts.py for the polling loop that fires
these). Also /mute and /unmute, which toggle User.notify_signals — the
subscription flag services/notification.py checks before DMing a new Signal.

Usage:
  /alert <token_mint> <threshold_pct> [up|down|both]   (default: both)
  /alerts                                              (list your active alerts)
  /removealert <alert_id>
"""

from __future__ import annotations

import httpx
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from whale_alpha.config import Env
from whale_alpha.db.models import AlertDirection, PriceAlert, Role, User
from whale_alpha.integrations import price_feed
from whale_alpha.integrations.solana_connection import is_valid_solana_address

router = Router(name="alerts")


async def _get_or_create_user(session_factory: async_sessionmaker, telegram_id: str) -> User:
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, role=Role.USER)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


def register_alert_commands(
    session_factory: async_sessionmaker, env: Env, http_client: httpx.AsyncClient
) -> Router:
    @router.message(Command("alert"))
    async def alert_handler(message: Message) -> None:
        args = (message.text or "").split()[1:]
        if len(args) < 2:
            await message.answer(
                "Usage: /alert <token_mint> <threshold_pct> [up|down|both]\n"
                "Example: /alert <mint> 15 up  — alert me when it's up 15% from now."
            )
            return

        token_mint, threshold_str = args[0], args[1]
        direction_str = args[2].upper() if len(args) > 2 else "BOTH"

        if not is_valid_solana_address(token_mint):
            await message.answer("That doesn't look like a valid Solana token mint address.")
            return

        try:
            threshold_pct = float(threshold_str)
        except ValueError:
            await message.answer("threshold_pct must be a number, e.g. 15 for 15%.")
            return
        if threshold_pct <= 0:
            await message.answer("threshold_pct must be greater than 0.")
            return

        if direction_str not in ("UP", "DOWN", "BOTH"):
            await message.answer("Direction must be one of: up, down, both.")
            return

        current_price = await price_feed.get_price_usd(http_client, env, token_mint)
        if current_price is None:
            await message.answer("⚠️ Couldn't fetch a current price for that mint — double check the address.")
            return

        user = await _get_or_create_user(session_factory, str(message.from_user.id))
        async with session_factory() as session:
            alert = PriceAlert(
                user_id=user.id,
                token_mint=token_mint,
                threshold_pct=threshold_pct,
                direction=AlertDirection(direction_str),
                reference_price_usd=current_price,
            )
            session.add(alert)
            await session.commit()
            await session.refresh(alert)

        await message.answer(
            f"🔔 Alert set for `{token_mint[:6]}...` — I'll DM you when it moves "
            f"{threshold_pct:g}% {direction_str.lower()} from ${current_price:.6f} "
            f"(id: `{alert.id}`).",
            parse_mode="Markdown",
        )

    @router.message(Command("alerts"))
    async def alerts_list_handler(message: Message) -> None:
        user = await _get_or_create_user(session_factory, str(message.from_user.id))
        async with session_factory() as session:
            result = await session.execute(
                select(PriceAlert)
                .where(PriceAlert.user_id == user.id, PriceAlert.active.is_(True))
                .order_by(PriceAlert.created_at.desc())
            )
            alerts = list(result.scalars())

        if not alerts:
            await message.answer("No active price alerts. Set one with /alert <mint> <threshold_pct>.")
            return

        lines = [
            f"`{a.id}` — `{a.token_mint[:6]}...` ± {a.threshold_pct:g}% ({a.direction.value.lower()}) "
            f"from ${a.reference_price_usd:.6f}"
            for a in alerts
        ]
        await message.answer(
            "🔔 *Your active alerts*\n\n" + "\n".join(lines) + "\n\nRemove one with /removealert <id>",
            parse_mode="Markdown",
        )

    @router.message(Command("removealert"))
    async def removealert_handler(message: Message) -> None:
        args = (message.text or "").split()[1:]
        if not args:
            await message.answer("Usage: /removealert <alert_id> — see /alerts for ids.")
            return
        alert_id = args[0]

        user = await _get_or_create_user(session_factory, str(message.from_user.id))
        async with session_factory() as session:
            alert = await session.get(PriceAlert, alert_id)
            if alert is None or alert.user_id != user.id:
                await message.answer("No alert with that id found for your account.")
                return
            alert.active = False
            await session.commit()

        await message.answer("🗑️ Alert removed.")

    @router.message(Command("mute"))
    async def mute_handler(message: Message) -> None:
        user = await _get_or_create_user(session_factory, str(message.from_user.id))
        async with session_factory() as session:
            db_user = await session.get(User, user.id)
            db_user.notify_signals = False
            await session.commit()
        await message.answer("🔇 Signal notifications muted. Use /unmute to turn them back on.")

    @router.message(Command("unmute"))
    async def unmute_handler(message: Message) -> None:
        user = await _get_or_create_user(session_factory, str(message.from_user.id))
        async with session_factory() as session:
            db_user = await session.get(User, user.id)
            db_user.notify_signals = True
            await session.commit()
        await message.answer("🔔 Signal notifications re-enabled.")

    return router
