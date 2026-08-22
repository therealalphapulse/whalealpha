"""Bot bootstrap — port of src/bot/index.ts (grammY Bot -> aiogram Dispatcher)."""

from __future__ import annotations

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import ErrorEvent, Message
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from whale_alpha.bot.commands.admin import register_admin_commands
from whale_alpha.bot.commands.alerts import register_alert_commands
from whale_alpha.bot.commands.manual_trading import register_manual_trading_commands
from whale_alpha.bot.commands.scanner import register_scanner_commands
from whale_alpha.bot.commands.trading import register_trading_commands
from whale_alpha.bot.commands.wallet import register_wallet_commands
from whale_alpha.bot.commands.whales import register_whales_command
from whale_alpha.bot.middlewares.rate_limit import RateLimitMiddleware
from whale_alpha.bot.middlewares.rbac import RbacMiddleware
from whale_alpha.config import Env
from whale_alpha.db.models import Role, User
from whale_alpha.utils.logger import child_logger

log = child_logger("bot")


def create_bot(
    env: Env, redis: Redis, session_factory: async_sessionmaker[AsyncSession], http_client: httpx.AsyncClient
) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=env.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=RedisStorage(redis))

    dp.update.outer_middleware(RateLimitMiddleware(redis))
    dp.update.outer_middleware(RbacMiddleware(env))

    @dp.message(Command("start"))
    async def start_handler(message: Message, is_admin: bool = False) -> None:
        if message.from_user is None:
            return
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
            "Whale Alpha is now an intelligence-only early opportunity detector. "
            "It finds very young Solana tokens showing multiple independent signs of strength, "
            "scores them 0–100, and sends only high-potential opportunities.\n\n"
            "🔎 Send a token contract address — scan any Solana meme token instantly\n"
            "/whales — browse the legacy whale database\n"
            "/mute /unmute — toggle legacy whale-signal DMs\n"
            f"{admin_lines}",
            parse_mode="Markdown",
        )

    dp.include_router(register_scanner_commands(env, http_client))
    dp.include_router(register_whales_command(session_factory))
    if env.ENABLE_LEGACY_TRADING:
        dp.include_router(register_trading_commands(session_factory))
        dp.include_router(register_wallet_commands(session_factory, env))
        dp.include_router(register_manual_trading_commands(session_factory, env, http_client))
        dp.include_router(register_alert_commands(session_factory, env, http_client))
    dp.include_router(register_admin_commands(session_factory))

    @dp.errors()
    async def error_handler(event: ErrorEvent) -> None:
        log.error("Unhandled bot error", err=str(event.exception))

    return bot, dp
