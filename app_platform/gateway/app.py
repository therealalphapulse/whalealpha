"""Telegram gateway composition root for AlphaPulse v4."""

from __future__ import annotations

import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config.settings import BOT_TOKEN

logger = logging.getLogger("AlphaPulse.Gateway")


def _build_storage():
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        logger.warning("REDIS_URL not set — using in-memory FSM storage.")
        return MemoryStorage()
    try:
        from aiogram.fsm.storage.redis import RedisStorage
        logger.info("Using Redis-backed FSM storage (%s)", redis_url.split("@")[-1])
        return RedisStorage.from_url(redis_url)
    except ImportError:
        logger.error("REDIS_URL is set but redis package is unavailable; using in-memory FSM storage.")
        return MemoryStorage()


def _wire_keyboard_factory() -> None:
    from app_platform.keyboards.token_actions import token_actions_keyboard
    from domain.signals.keyboard_provider import set_keyboard_factory
    set_keyboard_factory(token_actions_keyboard)


def _register_middleware(dp: Dispatcher) -> None:
    from app_platform.middleware.correlation_middleware import CorrelationMiddleware
    from app_platform.middleware.auth_middleware import AuthMiddleware
    from app_platform.middleware.rbac_middleware import RBACMiddleware
    from app_platform.middleware.premium_middleware import PremiumMiddleware
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(CorrelationMiddleware())
        observer.outer_middleware(AuthMiddleware())
        observer.middleware(RBACMiddleware())
        observer.middleware(PremiumMiddleware())


def _register_routers(dp: Dispatcher) -> None:
    from app_platform.commands.start import router as start_router
    from app_platform.commands.market import router as market_router
    from app_platform.commands.trending import router as trending_router
    from app_platform.commands.token import router as token_router
    from app_platform.commands.security import router as security_router
    from app_platform.commands.score import router as score_router
    from app_platform.commands.watchlist import router as watchlist_router
    from app_platform.commands.narrative import router as narrative_router
    from app_platform.commands.whales import router as whales_router
    from app_platform.commands.portfolio import router as portfolio_router
    from app_platform.commands.wallet_portfolio import router as wallet_portfolio_router
    from app_platform.commands.kol import router as kol_router
    from app_platform.commands.pump import router as pump_router
    from app_platform.commands.signals import router as signals_router
    from app_platform.commands.auto_scan import router as auto_scan_router
    from app_platform.commands.paper_trading import router as paper_trading_router
    from app_platform.commands.real_wallet import router as real_wallet_router
    from app_platform.commands.real_wallet_pnl import router as real_wallet_pnl_router
    from app_platform.commands.real_wallet_auto_settings import router as real_wallet_auto_settings_router
    from app_platform.commands.premium import router as premium_router
    from app_platform.commands.admin_panel import router as admin_panel_router
    for router in (
        start_router, market_router, trending_router, token_router, security_router,
        score_router, watchlist_router, narrative_router, whales_router, portfolio_router,
        wallet_portfolio_router, kol_router, pump_router, signals_router, paper_trading_router,
        real_wallet_pnl_router, real_wallet_router, real_wallet_auto_settings_router, premium_router,
        admin_panel_router, auto_scan_router,
    ):
        dp.include_router(router)


BOT_COMMANDS = [
    BotCommand(command="start", description="Welcome & feature overview"),
    BotCommand(command="signals", description="Active tracked signals"),
    BotCommand(command="winners", description="Best-performing signals"),
    BotCommand(command="top", description="Top 5 performing signals"),
    BotCommand(command="paper", description="Paper trading dashboard"),
    BotCommand(command="realwallet", description="Real Wallet — trade with real funds"),
    BotCommand(command="portfolio", description="Your manual portfolio"),
    BotCommand(command="wallet_portfolio", description="On-chain wallet lookup"),
    BotCommand(command="token", description="Token lookup: /token <contract>"),
    BotCommand(command="security", description="Security/rug check"),
    BotCommand(command="score", description="Token score"),
    BotCommand(command="trending", description="Trending tokens"),
    BotCommand(command="narratives", description="Trending themes/sectors"),
    BotCommand(command="market", description="Market overview"),
    BotCommand(command="watchlist", description="Your price watchlist"),
    BotCommand(command="track", description="Track a wallet"),
    BotCommand(command="wallets", description="Your tracked wallets"),
    BotCommand(command="kol_wallets", description="Known KOL wallets"),
    BotCommand(command="kol_status", description="KOL alert status"),
    BotCommand(command="pump_status", description="Signal alert status"),
    BotCommand(command="premium", description="Premium status & benefits"),
    BotCommand(command="premium_wallets", description="Smart Money wallet leaderboard"),
    BotCommand(command="premium_signals", description="Premium AI + consensus signals"),
    BotCommand(command="premium_snapshot", description="Premium Token Snapshot (Premium only)"),
    BotCommand(command="premium_stats", description="Premium engine status"),
]


def build_app() -> tuple[Bot, Dispatcher]:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing.")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=_build_storage())
    _wire_keyboard_factory()
    _register_middleware(dp)
    _register_routers(dp)
    return bot, dp


async def set_bot_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        logger.error("Failed to set bot command menu (non-fatal): %s", e)