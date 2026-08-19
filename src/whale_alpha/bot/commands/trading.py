"""/portfolio, /autotrading — port of src/bot/commands/trading.ts.

Manual trading + auto-trading configuration commands. Actual swap execution
goes through engines/trade_executor.py; this layer only validates input and
displays state.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from whale_alpha.db.models import Trade, TradeStatus, User

router = Router(name="trading")


def register_trading_commands(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    @router.message(Command("portfolio"))
    async def portfolio_handler(message: Message) -> None:
        if message.from_user is None:
            return
        telegram_id = str(message.from_user.id)
        async with session_factory() as session:
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()

            if user is None or not user.wallet_public_key:
                await message.answer("No wallet connected yet. Use /connectwallet to get started.")
                return

            trades_result = await session.execute(
                select(Trade)
                .where(Trade.user_id == user.id, Trade.status == TradeStatus.CONFIRMED)
                .order_by(Trade.created_at.desc())
                .limit(10)
            )
            open_trades = list(trades_result.scalars())

        if not open_trades:
            await message.answer("No trades yet. Use /whales to see tracked wallets or wait for a signal.")
            return

        lines = [
            f"{t.side.value} {t.token_mint[:6]}... — ${t.amount_usd:.2f} ({t.source.value})"
            for t in open_trades
        ]
        await message.answer("📊 *Recent trades*\n\n" + "\n".join(lines), parse_mode="Markdown")

    @router.message(Command("autotrading"))
    async def autotrading_handler(message: Message) -> None:
        if message.from_user is None:
            return
        telegram_id = str(message.from_user.id)
        async with session_factory() as session:
            result = await session.execute(
                select(User)
                .options(selectinload(User.auto_trading_config))
                .where(User.telegram_id == telegram_id)
            )
            user = result.scalar_one_or_none()

            if user is None:
                await message.answer("Use /start first to initialize your account.")
                return

            cfg = user.auto_trading_config
            if cfg is None:
                await message.answer(
                    "Auto Trading is not configured. Use /autotrading_setup to define your "
                    "rules (max daily trades, exposure, slippage, stop loss, etc.) before "
                    "enabling it."
                )
                return

            await message.answer(
                f"🤖 *Auto Trading*: {'ENABLED ✅' if cfg.enabled else 'disabled'}\n"
                f"Max daily trades: {cfg.max_daily_trades}\n"
                f"Max daily exposure: ${cfg.max_daily_exposure_usd}\n"
                f"Max slippage: {cfg.max_slippage_bps / 100}%\n"
                f"Min liquidity: ${cfg.min_liquidity_usd}\n"
                f"Risk profile: {cfg.risk_profile.value}\n\n"
                "_Auto Trading only fires on qualified Whale Alpha signals that pass all of "
                "the above — never directly off a wallet buy._",
                parse_mode="Markdown",
            )

    return router
