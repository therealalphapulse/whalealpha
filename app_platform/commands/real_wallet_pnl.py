import html
import logging
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton

from domain.trading.real import real_trade_engine
from domain.trading.real.solana_wallet import get_real_wallet
from app_platform.keyboards.real_wallet import real_wallet_menu_kb
from app_platform.domain.trading.real_pnl_image import generate_real_pnl_card
from providers.marketdata.dexscreener import get_token_card_info

router = Router()
logger = logging.getLogger("AlphaPulse.RealWalletPnL")


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _hold_time(opened_at, closed_at=None) -> str:
    if not opened_at:
        return "N/A"
    end = closed_at or datetime.now(timezone.utc).replace(tzinfo=None)
    start = opened_at.replace(tzinfo=None) if getattr(opened_at, "tzinfo", None) else opened_at
    seconds = max(0, int((end - start).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


async def _build_card_data(trade):
    """Build the visual snapshot from the persisted real trade and live market data.

    Trading/PnL calculations remain unchanged. Missing token metadata is filled
    from DexScreener so the card never renders placeholder identity when live
    token metadata is available.
    """
    entry_price = _to_float(trade.entry_price)
    token_quantity = _to_float(trade.token_quantity)
    remaining_quantity = _to_float(trade.remaining_quantity) if trade.remaining_quantity is not None else token_quantity

    if entry_price <= 0 or token_quantity <= 0:
        return None, "This trade does not contain enough recorded execution data to build a PnL card."

    original_cost_usd = entry_price * token_quantity
    info = None
    try:
        info = await get_token_card_info(trade.contract)
    except Exception as exc:
        logger.debug("Real PnL metadata lookup failed trade=%s: %s", trade.id, exc)

    logo_url = (info or {}).get("image_url") or ""
    name = (trade.name or (info or {}).get("name") or "Unknown Token").strip()
    symbol = (trade.symbol or (info or {}).get("symbol") or "???").strip()

    if trade.status == "open":
        current_price = _to_float((info or {}).get("price"))
        if current_price <= 0:
            current_price = _to_float(trade.current_price)
        if current_price <= 0:
            return None, "Could not fetch a current price for this position. Please try again shortly."

        current_value_usd = remaining_quantity * current_price
        remaining_cost_usd = remaining_quantity * entry_price
        unrealized_usd = current_value_usd - remaining_cost_usd

        sold_quantity = max(0.0, token_quantity - remaining_quantity)
        realized_usd = 0.0
        if sold_quantity > 0 and trade.exit_price is not None:
            exit_price = _to_float(trade.exit_price)
            if exit_price > 0:
                realized_usd = (exit_price - entry_price) * sold_quantity

        pnl_usd = realized_usd + unrealized_usd
        pnl_pct = (pnl_usd / original_cost_usd * 100.0) if original_cost_usd > 0 else 0.0
        status_label = "OPEN"
    else:
        exit_price = _to_float(trade.exit_price)
        if exit_price <= 0:
            return None, "This closed trade has no recorded exit price, so its PnL cannot be determined safely."
        current_price = exit_price
        pnl_usd = (exit_price - entry_price) * token_quantity
        pnl_pct = (pnl_usd / original_cost_usd * 100.0) if original_cost_usd > 0 else 0.0
        realized_usd = pnl_usd
        unrealized_usd = 0.0
        status_label = (trade.exit_reason or trade.status or "CLOSED").replace("closed_", "").upper()

    opened_str = trade.opened_at.strftime("%Y-%m-%d %H:%M UTC") if trade.opened_at else "N/A"
    hold_time = _hold_time(trade.opened_at, trade.closed_at)

    return {
        "name": name,
        "symbol": symbol,
        "entry_price": entry_price,
        "current_price": current_price,
        "usd_invested": original_cost_usd,
        "quantity": token_quantity,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "realized_pnl_usd": realized_usd,
        "unrealized_pnl_usd": unrealized_usd,
        "status": status_label,
        "opened_at_str": opened_str,
        "hold_time": hold_time,
        "logo_url": logo_url,
    }, None


@router.callback_query(F.data == "rw:history")
async def cb_real_wallet_history(callback: CallbackQuery):
    try:
        trades = await real_trade_engine.get_real_trade_history(callback.from_user.id)
        if not trades:
            await callback.answer("No closed trades yet.", show_alert=True)
            return

        wallet = await get_real_wallet(callback.from_user.id)
        await callback.answer()
        await callback.message.edit_text(
            "📜 <b>Recent Real Trades</b>\n\nEach trade below has its own PnL card.",
            reply_markup=real_wallet_menu_kb(wallet.auto_trading_enabled if wallet else False),
        )

        for trade in trades[:10]:
            realized = _to_float(trade.realized_pnl_sol)
            pnl_sign = "+" if realized >= 0 else ""
            text = (
                f"• <b>{html.escape(trade.symbol or '???')}</b> — "
                f"{pnl_sign}{realized:.4f} SOL "
                f"({html.escape(trade.status or 'unknown')})"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 Generate PnL Card", callback_data=f"rw:pnlcard:{trade.id}")],
            ])
            await callback.message.answer(text, reply_markup=kb)
    except Exception as exc:
        logger.exception("Real Wallet history error: %s", exc)
        await callback.answer("Could not load trade history.", show_alert=True)


@router.callback_query(F.data.startswith("rw:pnlcard:"))
async def cb_real_wallet_pnl_card(callback: CallbackQuery):
    try:
        trade_id = int(callback.data.split(":", 2)[2])
    except (ValueError, IndexError):
        await callback.answer("Invalid trade.", show_alert=True)
        return

    try:
        trades = await real_trade_engine.get_real_trade_history(callback.from_user.id, limit=100)
        trade = next((t for t in trades if t.id == trade_id), None)
    except Exception as exc:
        logger.error("Real PnL trade lookup failed user=%s trade=%s: %s", callback.from_user.id, trade_id, exc)
        await callback.answer("Could not load this trade.", show_alert=True)
        return

    if not trade:
        await callback.answer("Trade not found.", show_alert=True)
        return

    await callback.answer("Generating PnL card...")

    try:
        data, error = await _build_card_data(trade)
        if error:
            await callback.message.answer(f"⚠️ {html.escape(error)}")
            return
        png_bytes = await generate_real_pnl_card(data)
        if not png_bytes:
            await callback.message.answer("⚠️ Could not generate the PnL card right now. Please try again.")
            return
        filename = f"{data['symbol'] or 'real_trade'}_pnl.png"
        await callback.message.answer_photo(BufferedInputFile(png_bytes, filename=filename))
    except Exception as exc:
        logger.exception("Real PnL card generation failed user=%s trade=%s: %s", callback.from_user.id, trade_id, exc)
        await callback.message.answer("⚠️ Could not generate the PnL card right now. Please try again.")
