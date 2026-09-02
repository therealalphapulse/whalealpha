"""Bot bootstrap â port of src/bot/index.ts (grammY Bot -> aiogram Dispatcher)."""

from __future__ import annotations

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
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
    env: Env,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    *,
    use_redis_storage: bool = True,
) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=env.TELEGRAM_BOT_TOKEN)
    storage = RedisStorage(redis) if use_redis_storage else MemoryStorage()
    dp = Dispatcher(storage=storage)

    dp.update.outer_middleware(RateLimitMiddleware(redis))
    dp.update.outer_middleware(RbacMiddleware(env))

    async def _welcome(message: Message, is_admin: bool = False) -> None:
        if message.from_user is None:
            return
        telegram_id = str(message.from_user.id)
        async with session_factory() as session:
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            if user is None:
                session.add(User(telegram_id=telegram_id, role=Role.USER))
                await session.commit()
        admin_section = (
            "\n\n<b>ð  ADMIN</b>\n<code>/addwhale</code>  <code>/approvewhale</code>  <code>/removewhale</code>"
            if is_admin else ""
        )
        await message.answer(
            "ð <b>WHALE ALPHA</b>\n"
            "<i>Professional Solana intelligence terminal</i>\n\n"
            "<b>ð TOKEN SCANNER</b>\n"
            "Send <b>only the Solana contract address</b>. No command required.\n\n"
            "<b>ð¨ SIGNALS</b>\n"
            "High-potential early opportunities are delivered automatically when the existing quality gates pass.\n\n"
            "<b>ð PERFORMANCE</b>\n"
            "Qualified signals can receive reply-based quote updates at crossed gain milestones.\n\n"
            "<b>ð INTELLIGENCE</b>\n"
            "<code>/whales</code> â elite wallet intelligence\n"
            "<code>/mute</code> / <code>/unmute</code> â signal notifications\n"
            "<code>/alerts</code> â manage price alerts\n"
            "<code>/help</code> â command guide"
            f"{admin_section}\n\n"
            "<i>Built for disciplined on-chain research. Data is informational, not financial advice.</i>",
            parse_mode="HTML",
        )

    @dp.message(Command("start"))
    async def start_handler(message: Message, is_admin: bool = False) -> None:
        await _welcome(message, is_admin)

    @dp.message(Command("help"))
    async def help_handler(message: Message, is_admin: bool = False) -> None:
        await _welcome(message, is_admin)

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
