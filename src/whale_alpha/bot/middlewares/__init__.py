"""Bot bootstrap — port of src/bot/index.ts (grammY Bot -> aiogram Dispatcher).

Library-driven difference (see PORTING_NOTES.md): grammY's `Bot` instance owns
both transport and dispatch. aiogram v3 splits this into a `Bot` (transport,
API calls) and a `Dispatcher` (routing/middleware), with `Router`s attached to
the dispatcher. We mirror the same command set and the same middleware order
(rate limit -> RBAC) as the original `bot.use(...)` calls.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import Command
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from whale_alpha.bot.commands.admin import register_admin_commands
from whale_alpha.bot.commands.trading import register_trading_commands
from whale_alpha.bot.commands.whales import register_whales_command
from whale_alpha.bot.middlewares.rate_limit import RateLimitMiddleware
from whale_alpha.bot.middlewares.rbac import RbacMiddleware
from whale_alpha.config import Env
from whale_alpha.db.models import Role, User
from whale_alpha.utils.logger import child_logger

log = child_logger("bot")


def create_bot(env: Env, redis: Redis, session_factory: async_sessionmaker) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=env.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    dp.update.outer_middleware(RateLimitMiddleware(redis))
    dp.update.outer_middleware(RbacMiddleware(env))

    @dp.message(Command("start"))
    async def start_handler(message: Message, is_admin: bool = False) -> None:
        telegram_id = str(message.from_user.id)
        async with session_factory() as session:
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            if user is None:
                session.add(User(telegram_id=telegram_id, role=Role.USER))
                await session.commit()

        admin_lines = "\n*Admin:* /addwhale /approvewhale /removewhale" if is_admin else ""
        await message.answer(
            "🐋 *Welcome to Whale Alpha*\n\n"
            "Track elite Solana wallets, get high-confidence signals when multiple "
            "whales accumulate the same token, and trade manually or with rule-based "
            "auto trading.\n\n"
            "/whales — browse the curated whale database\n"
            "/portfolio — your trade history\n"
            "/autotrading — view/configure auto-trading rules\n"
            f"{admin_lines}",
            parse_mode="Markdown",
        )

    dp.include_router(register_whales_command(session_factory))
    dp.include_router(register_trading_commands(session_factory))
    dp.include_router(register_admin_commands(session_factory))

    @dp.errors()
    async def error_handler(event) -> None:
        log.error("Unhandled bot error", err=str(event.exception))

    return bot, dp
