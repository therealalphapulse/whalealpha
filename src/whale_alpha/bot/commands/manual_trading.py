"""/buy, /sell — did not exist at all before this port. Every registered
command was checked against the codebase; there was no manual trading entry
point despite `engines/trade_executor.py` containing a fully-working
executor. This is the missing bot-layer caller for it, using
TradeSource.MANUAL (as opposed to auto_trading.py's TradeSource.AUTO_SIGNAL).

Usage:
  /buy <token_mint> <usd_amount> [slippage_bps]
  /sell <token_mint> <token_amount|all> [slippage_bps]

Both commands go through the same PENDING-row-before-execution pattern as
auto_trading.py, for the same crash-safety reason (see trade_executor.py's
and reconciliation.py's docstrings) — a manual trade is just as important to
recover after a crash as an automated one.
"""

from __future__ import annotations

import httpx
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from whale_alpha.config import Env
from whale_alpha.db.models import Trade, TradeSide, TradeSource, TradeStatus, User
from whale_alpha.engines.trade_executor import ExecuteTradeParams, execute_trade
from whale_alpha.integrations import price_feed
from whale_alpha.integrations.solana_connection import (
    create_connection,
    get_token_balance,
    is_valid_solana_address,
)
from whale_alpha.utils.logger import child_logger

router = Router(name="manual_trading")
log = child_logger("manualTrading")


async def _get_connected_user(session_factory: async_sessionmaker[AsyncSession], telegram_id: str) -> User | None:
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None or not user.encrypted_wallet_key or not user.wallet_public_key:
            return None
        return user


def register_manual_trading_commands(
    session_factory: async_sessionmaker[AsyncSession], env: Env, http_client: httpx.AsyncClient
) -> Router:
    @router.message(Command("buy"))
    async def buy_handler(message: Message) -> None:
        args = (message.text or "").split()[1:]
        if len(args) < 2:
            await message.answer("Usage: /buy <token_mint> <usd_amount> [slippage_bps]")
            return

        token_mint, usd_amount_str = args[0], args[1]
        slippage_bps = env.DEFAULT_MAX_SLIPPAGE_BPS
        if len(args) > 2:
            try:
                slippage_bps = int(args[2])
            except ValueError:
                await message.answer("slippage_bps must be an integer (basis points, e.g. 150 = 1.5%).")
                return

        if not is_valid_solana_address(token_mint):
            await message.answer("That doesn't look like a valid Solana token mint address.")
            return

        try:
            usd_amount = float(usd_amount_str)
        except ValueError:
            await message.answer("usd_amount must be a number, e.g. /buy <mint> 25")
            return
        if usd_amount <= 0:
            await message.answer("usd_amount must be greater than 0.")
            return

        if message.from_user is None:
            return
        user = await _get_connected_user(session_factory, str(message.from_user.id))
        if user is None:
            await message.answer("No wallet connected. Use /connectwallet first.")
            return

        sol_price = await price_feed.get_sol_price_usd(http_client, env)
        if sol_price is None:
            await message.answer("⚠️ SOL/USD price is temporarily unavailable — try again in a moment.")
            return

        lamports = round((usd_amount / sol_price) * 1e9)

        status_msg = await message.answer(f"⏳ Buying ${usd_amount:.2f} of `{token_mint[:6]}...`...", parse_mode="Markdown")

        async with session_factory() as session:
            trade = Trade(
                user_id=user.id,
                signal_id=None,
                source=TradeSource.MANUAL,
                side=TradeSide.BUY,
                token_mint=token_mint,
                amount_usd=usd_amount,
                status=TradeStatus.PENDING,
                slippage_bps=slippage_bps,
            )
            session.add(trade)
            await session.commit()
            await session.refresh(trade)

            try:
                result = await execute_trade(
                    session,
                    http_client,
                    env,
                    ExecuteTradeParams(
                        side="BUY",
                        token_mint=token_mint,
                        amount_lamports_or_tokens=lamports,
                        slippage_bps=slippage_bps,
                        encrypted_wallet_key=user.encrypted_wallet_key,  # type: ignore[arg-type]
                        trade_row_id=trade.id,
                    ),
                )
                await status_msg.edit_text(
                    f"✅ Bought `{token_mint[:6]}...` for ${usd_amount:.2f}\n"
                    f"Tx: `{result.tx_signature}`",
                    parse_mode="Markdown",
                )
            except Exception as err:  # noqa: BLE001
                log.error("Manual buy failed", err=str(err), user_id=user.id, token_mint=token_mint)
                await status_msg.edit_text(f"❌ Buy failed: {err}")

    @router.message(Command("sell"))
    async def sell_handler(message: Message) -> None:
        args = (message.text or "").split()[1:]
        if len(args) < 2:
            await message.answer("Usage: /sell <token_mint> <token_amount|all> [slippage_bps]")
            return

        token_mint, amount_str = args[0], args[1]
        slippage_bps = env.DEFAULT_MAX_SLIPPAGE_BPS
        if len(args) > 2:
            try:
                slippage_bps = int(args[2])
            except ValueError:
                await message.answer("slippage_bps must be an integer (basis points, e.g. 150 = 1.5%).")
                return

        if not is_valid_solana_address(token_mint):
            await message.answer("That doesn't look like a valid Solana token mint address.")
            return

        if message.from_user is None:
            return
        user = await _get_connected_user(session_factory, str(message.from_user.id))
        if user is None:
            await message.answer("No wallet connected. Use /connectwallet first.")
            return

        connection = create_connection(env)
        try:
            raw_balance, decimals = await get_token_balance(connection, user.wallet_public_key, token_mint)  # type: ignore[arg-type]
        except Exception as err:  # noqa: BLE001
            await message.answer(f"⚠️ Couldn't fetch your token balance: {err}")
            return
        finally:
            await connection.close()

        if raw_balance <= 0:
            await message.answer("You don't hold any of that token in the connected wallet.")
            return

        if amount_str.lower() == "all":
            base_units = raw_balance
            human_amount = raw_balance / (10**decimals)
        else:
            try:
                human_amount = float(amount_str)
            except ValueError:
                await message.answer("token_amount must be a number, or `all`.")
                return
            if human_amount <= 0:
                await message.answer("token_amount must be greater than 0.")
                return
            base_units = round(human_amount * (10**decimals))
            if base_units > raw_balance:
                await message.answer(
                    f"You only hold {raw_balance / (10**decimals):g} of this token — can't sell {human_amount:g}."
                )
                return

        status_msg = await message.answer(f"⏳ Selling {human_amount:g} of `{token_mint[:6]}...`...", parse_mode="Markdown")

        async with session_factory() as session:
            # amount_usd is filled in as an estimate from the price feed where
            # available; execute_trade doesn't require it, but Trade.amount_usd
            # is non-nullable, so a best-effort estimate beats a fabricated 0.
            price = await price_feed.get_price_usd(http_client, env, token_mint)
            estimated_usd = (human_amount * price) if price is not None else 0.0

            trade = Trade(
                user_id=user.id,
                signal_id=None,
                source=TradeSource.MANUAL,
                side=TradeSide.SELL,
                token_mint=token_mint,
                amount_usd=estimated_usd,
                amount_tokens=human_amount,
                status=TradeStatus.PENDING,
                slippage_bps=slippage_bps,
            )
            session.add(trade)
            await session.commit()
            await session.refresh(trade)

            try:
                result = await execute_trade(
                    session,
                    http_client,
                    env,
                    ExecuteTradeParams(
                        side="SELL",
                        token_mint=token_mint,
                        amount_lamports_or_tokens=base_units,
                        slippage_bps=slippage_bps,
                        encrypted_wallet_key=user.encrypted_wallet_key,  # type: ignore[arg-type]
                        trade_row_id=trade.id,
                    ),
                )
                await status_msg.edit_text(
                    f"✅ Sold {human_amount:g} of `{token_mint[:6]}...`\n"
                    f"Tx: `{result.tx_signature}`",
                    parse_mode="Markdown",
                )
            except Exception as err:  # noqa: BLE001
                log.error("Manual sell failed", err=str(err), user_id=user.id, token_mint=token_mint)
                await status_msg.edit_text(f"❌ Sell failed: {err}")

    return router
