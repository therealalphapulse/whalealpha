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

from whale_alpha.db.models import AutoTradingConfig, Trade, TradeStatus, User
from whale_alpha.engines.trading_engine import FIXED_AUTO_POLICY

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

@router.message(Command("autobuy"))
async def autobuy_handler(message: Message) -> None:
    if message.from_user is None:
        return
    telegram_id = str(message.from_user.id)
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            await message.answer("Use /start first to initialize your account.")
            return
        if not user.encrypted_wallet_key or not user.wallet_public_key:
            await message.answer("Connect a trading wallet first with /connectwallet.")
            return
        cfg = user.auto_trading_config
        if cfg is None:
            cfg = AutoTradingConfig(user_id=user.id, enabled=True)
            session.add(cfg)
        else:
            cfg.enabled = not cfg.enabled
        cfg.fixed_trade_amount_usd = FIXED_AUTO_POLICY.amount_usd
        cfg.percent_allocation = None
        cfg.max_slippage_bps = FIXED_AUTO_POLICY.max_slippage_bps
        cfg.min_liquidity_usd = FIXED_AUTO_POLICY.min_liquidity_usd
        cfg.max_open_positions = FIXED_AUTO_POLICY.max_open_positions
        cfg.max_daily_trades = FIXED_AUTO_POLICY.max_daily_trades
        cfg.max_daily_exposure_usd = FIXED_AUTO_POLICY.max_daily_exposure_usd
        cfg.cooldown_minutes = int(FIXED_AUTO_POLICY.cooldown_minutes)
        cfg.max_market_cap_usd = FIXED_AUTO_POLICY.max_market_cap_usd
        await session.commit()
        enabled = cfg.enabled
    await message.answer(
        ("🤖 <b>Auto Buy ENABLED</b>\n\n" if enabled else "⛔ <b>Auto Buy DISABLED</b>\n\n")
        + f"Fixed entry: <b>${FIXED_AUTO_POLICY.amount_usd:.2f}</b> per qualified signal\n"
          f"Max daily trades: {FIXED_AUTO_POLICY.max_daily_trades}\n"
          f"Max daily exposure: ${FIXED_AUTO_POLICY.max_daily_exposure_usd:.2f}\n"
          f"Max slippage: {FIXED_AUTO_POLICY.max_slippage_bps / 100:.2f}%\n"
          f"Min liquidity: ${FIXED_AUTO_POLICY.min_liquidity_usd:,.0f}\n"
          f"Cooldown: {FIXED_AUTO_POLICY.cooldown_minutes:g} min\n\n"
          "Auto Buy fires only from qualified Whale Alpha signals.",
        parse_mode="HTML",
    )

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
    f"🤖 *Auto Buy*: {'ENABLED ✅' if cfg.enabled else 'disabled'}\n"
    f"Fixed entry: ${FIXED_AUTO_POLICY.amount_usd:.2f} per qualified signal\n"
    f"Max daily trades: {FIXED_AUTO_POLICY.max_daily_trades}\n"
    f"Max daily exposure: ${FIXED_AUTO_POLICY.max_daily_exposure_usd:.2f}\n"
    f"Max slippage: {FIXED_AUTO_POLICY.max_slippage_bps / 100:.2f}%\n"
    f"Min liquidity: ${FIXED_AUTO_POLICY.min_liquidity_usd:,.0f}\n"
    f"Cooldown: {FIXED_AUTO_POLICY.cooldown_minutes:g} min\n\n"
    "_Auto Buy fires only on qualified Whale Alpha signals. Use /autobuy to toggle it._",
    parse_mode="Markdown",
)

    return router
