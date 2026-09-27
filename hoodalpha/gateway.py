"""
hoodalpha/gateway.py

HoodAlpha's user-facing Telegram process. Run with:

    python -m hoodalpha.gateway

Deliberately minimal: HoodAlpha's job is to push dip-recovery signals,
not to replicate WhaleAlpha's command surface. Ships with /start,
/help, and /status (a small read-only summary of how many tokens are
currently being watched/armed). Long-polling, same as WhaleAlpha's own
`whalealpha` service (app_platform.gateway.polling_entrypoint) -- but
its own Dispatcher, own Bot, own process.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select, func

from hoodalpha.telegram import build_bot
from hoodalpha.db import hood_session, close_db
from hoodalpha.models import HoodWatch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("HoodAlpha.Gateway")

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>HoodAlpha</b>\n\n"
        "I track meaningful dips on tokens WhaleAlpha has alerted, and "
        "signal when price recovers 20%+ off the lowest dip.\n\n"
        "/status — how many tokens I'm currently watching"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    async with hood_session() as session:
        counts = await session.execute(
            select(HoodWatch.status, func.count()).group_by(HoodWatch.status)
        )
        rows = dict(counts.all())

    watching = rows.get("watching", 0)
    dip_confirmed = rows.get("dip_confirmed", 0)
    stale = rows.get("stale", 0)

    await message.answer(
        "📊 <b>HoodAlpha status</b>\n\n"
        f"Watching (pre-dip): {watching}\n"
        f"Dip confirmed (armed for rise): {dip_confirmed}\n"
        f"Stale: {stale}"
    )


async def main() -> None:
    logger.info("HoodAlpha gateway starting...")
    bot = build_bot()
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
