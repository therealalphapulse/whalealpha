import asyncio
import calendar as cal_module
import html
import logging
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from domain.trading.paper.paper_engine import (
    get_or_create_portfolio,
    get_or_create_settings,
    update_setting,
    get_open_trades,
    get_all_trades,
    execute_paper_buy,
    get_trade_by_id,
    close_paper_trade,
    partial_close_paper_trade,
    get_pnl_calendar,
    get_trades_opened_today_count,
    reset_portfolio,
    get_or_create_filters,
    update_filter,
    get_or_create_dca_settings,
    update_dca_setting,
    set_dca_custom_levels,
    get_dca_custom_levels,
    parse_custom_dca_levels,
    get_dca_fills,
)
from providers.marketdata.dexscreener import get_token_card_info
from domain.trading.pnl_image import generate_pnl_card_image
from domain.trading.pnl_calendar_image import generate_calendar_image
from app_platform.keyboards.portfolio import portfolio_hub_row

router = Router()
logger = logging.getLogger("AlphaPulse.PaperTrading")


class PaperAmountStates(StatesGroup):
    waiting_buy_amount = State()
    waiting_sell_amount = State()
    waiting_sl_pct = State()


class PaperFilterStates(StatesGroup):
    waiting_filter_value = State()


class PaperDCAStates(StatesGroup):
    waiting_max_entries = State()
    waiting_trigger_pct = State()
    waiting_entry_amount = State()
    waiting_custom_levels = State()


# Auto-Buy Filter field definitions: keyed by the short token used in
# callback_data (paper:filter_set:<key>). "range" fields store a min/max
# pair; "single" fields store one threshold.
FILTER_FIELDS = {
    "mcap": {
        "label": "Market Cap Range (USD)",
        "kind": "range",
        "min_attr": "min_market_cap",
        "max_attr": "max_market_cap",
        "example": "10000-500000",
    },
    "holders": {
        "label": "Minimum Holder Count",
        "kind": "single",
        "attr": "min_holders",
        "example": "50",
    },
    "liquidity": {
        "label": "Minimum Liquidity (USD)",
        "kind": "single",
        "attr": "min_liquidity_usd",
        "example": "5000",
    },
    "bundle": {
        "label": "Max Bundle Wallet %",
        "kind": "single",
        "attr": "max_bundle_pct",
        "example": "15",
    },
    "devhold": {
        "label": "Max Dev Holding %",
        "kind": "single",
        "attr": "max_dev_holding_pct",
        "example": "10",
    },
    "age": {
        "label": "Token Age Range (hours)",
        "kind": "range",
        "min_attr": "min_age_hours",
        "max_attr": "max_age_hours",
        "example": "0-6",
    },
}


def format_usd(value) -> str:
    try:
        num = float(value)
        if abs(num) >= 1000:
            return f"${num:,.2f}"
        return f"${num:.2f}"
    except Exception:
        return "N/A"


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return default


def _parse_sl_pct(text: str) -> tuple[float | None, str | None]:
    raw = (text or "").strip().replace("%", "").replace(",", "")
    if not raw:
        return None, "Please enter a stop-loss percentage."

    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None, "That doesn't look like a valid number."

    if value <= 0:
        return None, "Stop loss must be greater than zero."
    if value > 95:
        return None, "Stop loss must be 95% or less."

    return round(value, 2), None


def _parse_single_filter_input(text: str) -> tuple[float | None, str | None]:
    raw = (text or "").strip().replace(",", "").replace("%", "").replace("$", "")
    if not raw:
        return None, "Please enter a number."

    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None, "That doesn't look like a valid number."

    if value < 0:
        return None, "Value must be zero or greater."

    return value, None


def _parse_range_filter_input(text: str) -> tuple[float | None, float | None, str | None]:
    raw = (text or "").strip().replace(" ", "").replace("$", "")
    if not raw:
        return None, None, "Please enter a number or a min-max range."

    sep = "-" if "-" in raw else ("," if "," in raw else None)

    if sep:
        parts = raw.split(sep, 1)
        if len(parts) != 2:
            return None, None, "Use the format min-max, e.g. 10000-500000."
        try:
            min_v = float(parts[0]) if parts[0] else None
            max_v = float(parts[1]) if parts[1] else None
        except (ValueError, TypeError):
            return None, None, "That doesn't look like a valid range."
    else:
        try:
            min_v = float(raw)
            max_v = None
        except (ValueError, TypeError):
            return None, None, "That doesn't look like a valid number or range."

    if min_v is not None and min_v < 0:
        return None, None, "Minimum must be zero or greater."
    if max_v is not None and max_v < 0:
        return None, None, "Maximum must be zero or greater."
    if min_v is not None and max_v is not None and min_v > max_v:
        return None, None, "Minimum cannot be greater than maximum."

    return min_v, max_v, None


def _format_range_display(min_v, max_v) -> str:
    if min_v is not None and max_v is not None:
        return f"{min_v:g} – {max_v:g}"
    if min_v is not None:
        return f"≥ {min_v:g}"
    if max_v is not None:
        return f"≤ {max_v:g}"
    return "Any"


def _parse_sell_amount(text: str, current_value_usd: float) -> tuple[float | None, str | None]:
    """
    Parses a user-typed sell amount into a sell percentage.
    Accepts either a percentage ("40" or "40%") or a USD amount ("$25" or "25").
    A trailing '%' is treated as a percentage; anything else is treated as USD.
    Returns (sell_pct, error_message).
    """
    raw = (text or "").strip().replace(",", "")
    if not raw:
        return None, "Please enter an amount."

    is_pct = raw.endswith("%")
    numeric_part = raw[:-1] if is_pct else raw.lstrip("$")

    try:
        value = float(numeric_part)
    except (ValueError, TypeError):
        return None, "That doesn't look like a valid number."

    if value <= 0:
        return None, "Amount must be greater than zero."

    if is_pct:
        if value > 100:
            return None, "Percentage can't exceed 100%."
        return value, None

    # Treat as a USD amount of the position's current value.
    if current_value_usd <= 0:
        return None, "This position has no remaining value to sell."

    if value > current_value_usd * 1.001:  # small epsilon for rounding
        return None, f"You only have {format_usd(current_value_usd)} available in this position."

    pct = (value / current_value_usd) * 100
    return min(pct, 100.0), None


def _parse_buy_amount(text: str, available_balance: float) -> tuple[float | None, str | None]:
    """
    Parses a user-typed buy amount (USD). Validates it's positive and does
    not exceed the user's current virtual balance.
    """
    raw = (text or "").strip().replace(",", "").lstrip("$")
    if not raw:
        return None, "Please enter a USD amount."

    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None, "That doesn't look like a valid number."

    if value <= 0:
        return None, "Amount must be greater than zero."

    if value > available_balance:
        return None, f"That exceeds your available balance of {format_usd(available_balance)}."

    return round(value, 2), None


async def build_paper_dashboard(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds the Paper Trading dashboard text + keyboard.
    Shared by the /paper command and the Portfolio hub's "Paper Portfolio" tab.
    """
    portfolio = await get_or_create_portfolio(user_id)
    settings = await get_or_create_settings(user_id)
    trades = await get_open_trades(user_id)
    today_trade_count = await get_trades_opened_today_count(user_id)

    win_rate = 0
    if portfolio.total_trades > 0:
        win_rate = (portfolio.winning_trades / portfolio.total_trades) * 100

    invested = sum(t.usd_invested for t in trades)
    available = portfolio.balance

    text = (
        "📊 <b>Paper Trading Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Virtual Balance: <b>{format_usd(available)}</b>\n"
        f"📈 Invested: <b>{format_usd(invested)}</b>\n"
        f"💎 Portfolio Value: <b>{format_usd(available + invested)}</b>\n\n"
        f"📊 Total Trades: <b>{portfolio.total_trades}</b>\n"
        f"🟢 Wins: <b>{portfolio.winning_trades}</b>\n"
        f"🔴 Losses: <b>{portfolio.losing_trades}</b>\n"
        f"🎯 Win Rate: <b>{win_rate:.1f}%</b>\n\n"
        f"💵 Net PnL: <b>{format_usd(portfolio.net_pnl)}</b>\n"
        f"🟢 Total Profit: <b>{format_usd(portfolio.total_profit)}</b>\n"
        f"🔴 Total Loss: <b>{format_usd(portfolio.total_loss)}</b>\n"
        f"🏆 Best Trade: <b>{format_usd(portfolio.best_trade_pnl)}</b>\n"
        f"📉 Worst Trade: <b>{format_usd(portfolio.worst_trade_pnl)}</b>\n\n"
        f"📦 Open Positions: <b>{len(trades)}</b>\n\n"
        "⚙️ <b>Settings</b>\n"
        f"Auto Buy: <b>{'✅' if settings.auto_buy else '❌'}</b>\n"
        f"Buy Amount: <b>{format_usd(settings.buy_amount_usd)}</b>\n"
        f"Take Profit: <b>{settings.take_profit_pct}%</b>\n"
        f"Stop Loss: <b>{settings.stop_loss_pct}%</b>\n"
        f"Max Positions: <b>{settings.max_open_positions}</b>\n"
        f"Daily Auto-Buy Limit: <b>{settings.daily_trade_limit}/day</b> (used today: {today_trade_count})\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 AlphaPulse Paper Trading"
    )

    now = datetime.now(timezone.utc)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        portfolio_hub_row("paper"),
        [
            InlineKeyboardButton(text="📦 Positions", callback_data="paper:positions"),
            InlineKeyboardButton(text="📜 History", callback_data="paper:history"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Settings", callback_data="paper:settings"),
            InlineKeyboardButton(text="🧰 Auto-Buy Filters", callback_data="paper:filters"),
        ],
        [
            InlineKeyboardButton(text="🧬 DCA Strategy", callback_data="paper:dca"),
        ],
        [
            InlineKeyboardButton(text="🗓️ PnL Calendar", callback_data=f"paper:calendar:{now.year}:{now.month}"),
        ],
    ])

    return text, keyboard


def _fmt_filter_range(min_v, max_v) -> str:
    return _format_range_display(min_v, max_v)


def _fmt_filter_single(value, suffix: str = "") -> str:
    return "Any" if value is None else f"{value:g}{suffix}"


async def build_filters_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds the Auto-Buy Filters screen: shows each filter's current value
    and lets the user tap a field to edit it. If no filters are configured
    (or they're paused), auto-buy falls back to randomized selection from
    high-potential signals instead of filter matching.
    """
    filters = await get_or_create_filters(user_id)
    active = filters.has_active_filters()

    if active and filters.enabled:
        status = "✅ Active — auto-buy only matches these criteria"
    elif active and not filters.enabled:
        status = "⏸️ Paused — auto-buy is using randomized high-potential picks"
    else:
        status = "⚪ None set — auto-buy is using randomized high-potential picks"

    text = (
        "🧰 <b>Auto-Buy Filters</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Status: <b>{status}</b>\n\n"
        f"💰 Market Cap: <b>{_fmt_filter_range(filters.min_market_cap, filters.max_market_cap)}</b>\n"
        f"👥 Min Holders: <b>{_fmt_filter_single(filters.min_holders)}</b>\n"
        f"💧 Min Liquidity: <b>{_fmt_filter_single(filters.min_liquidity_usd)}</b>\n"
        f"📦 Max Bundle %: <b>{_fmt_filter_single(filters.max_bundle_pct, '%')}</b>\n"
        f"👤 Max Dev Holding %: <b>{_fmt_filter_single(filters.max_dev_holding_pct, '%')}</b>\n"
        f"🕒 Token Age (hrs): <b>{_fmt_filter_range(filters.min_age_hours, filters.max_age_hours)}</b>\n\n"
        "Tap a field below to set or update it. Leaving everything unset "
        "means the bot picks for you from high-potential signals.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 AlphaPulse Paper Trading"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Market Cap", callback_data="paper:filter_set:mcap")],
        [InlineKeyboardButton(text="👥 Min Holders", callback_data="paper:filter_set:holders")],
        [InlineKeyboardButton(text="💧 Min Liquidity", callback_data="paper:filter_set:liquidity")],
        [InlineKeyboardButton(text="📦 Max Bundle %", callback_data="paper:filter_set:bundle")],
        [InlineKeyboardButton(text="👤 Max Dev Holding %", callback_data="paper:filter_set:devhold")],
        [InlineKeyboardButton(text="🕒 Token Age", callback_data="paper:filter_set:age")],
        [
            InlineKeyboardButton(
                text=("⏸️ Pause Filters" if filters.enabled else "▶️ Resume Filters"),
                callback_data="paper:filter_toggle",
            ),
        ],
        [InlineKeyboardButton(text="🗑️ Clear All Filters", callback_data="paper:filter_clear_all")],
        [InlineKeyboardButton(text="⬅️ Back to Settings", callback_data="paper:settings")],
    ])

    return text, keyboard


@router.message(Command("paper", "portfolio_paper", "pp"))
async def cmd_paper_dashboard(message: Message):
    try:
        text, keyboard = await build_paper_dashboard(message.from_user.id)
        await message.answer(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"/paper dashboard error: {e}")
        await message.answer(
            "⚠️ Could not load your paper dashboard right now.\n"
            "Please send /start once, then try /paper again."
        )


@router.callback_query(F.data == "paper:dashboard")
async def cb_paper_dashboard(callback: CallbackQuery):
    try:
        text, keyboard = await build_paper_dashboard(callback.from_user.id)
        await callback.message.answer(text, reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"paper dashboard callback error: {e}")
        await callback.answer("Could not load dashboard.", show_alert=True)


@router.callback_query(F.data == "paper:noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("paper_buy:"))
async def cb_paper_buy(callback: CallbackQuery):
    """
    Triggered when a user clicks '📝 Paper Buy' on a signal/scan card.
    Callback data format: paper_buy:<contract_address>
    """
    try:
        parts = callback.data.split(":", 1)
        if len(parts) < 2 or not parts[1].strip():
            await callback.answer("Invalid token.", show_alert=True)
            return

        contract = parts[1].strip()
        user_id = callback.from_user.id

        await callback.answer("Opening paper trade...")

        data = await get_token_card_info(contract)
        if not data:
            await callback.message.answer("⚠️ Could not fetch token data for paper buy.")
            return

        price = _to_float(data.get("price"))
        if price <= 0:
            await callback.message.answer("⚠️ Invalid token price. Cannot open paper trade.")
            return

        result = await execute_paper_buy(
            user_id=user_id,
            contract=contract,
            name=data.get("name", "Unknown"),
            symbol=data.get("symbol", "???"),
            current_price=price,
        )

        if result.get("ok"):
            if result.get("dca_fill"):
                text = (
                    f"🧬 <b>DCA Fill #{result.get('fill_number')} Added</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📛 {result.get('name', 'Unknown')} (${result.get('symbol', '???')})\n"
                    f"💰 Added: {format_usd(result.get('fill_usd_amount', 0))} @ ${result.get('fill_price', 0):.8f}\n"
                    f"📊 New Avg Entry: ${result.get('new_avg_entry_price', 0):.8f}\n"
                    f"💼 Total Invested: {format_usd(result.get('new_total_invested', 0))}\n"
                    f"💵 Balance Left: {format_usd(result.get('balance_remaining', 0))}\n\n"
                    f"<code>{contract}</code>"
                )
            else:
                text = (
                    "✅ <b>Paper Trade Opened!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📛 {result.get('name', 'Unknown')} (${result.get('symbol', '???')})\n"
                    f"💰 Entry: ${result.get('entry_price', 0):.8f}\n"
                    f"📊 Invested: {format_usd(result.get('usd_invested', 0))}\n"
                    f"🪙 Tokens: {result.get('token_quantity', 0):.4f}\n"
                    f"💵 Balance Left: {format_usd(result.get('balance_remaining', 0))}\n\n"
                    f"<code>{contract}</code>"
                )
            await callback.message.answer(text)
        else:
            await callback.message.answer(f"⚠️ {result.get('reason', 'Paper trade failed.')}")

    except Exception as e:
        logger.error(f"cb_paper_buy error: {e}")
        try:
            await callback.answer("Paper buy failed. Try again.", show_alert=True)
        except Exception:
            pass


async def _get_live_price(contract: str, fallback: float) -> float:
    """
    Fetches the live market price for a contract, falling back to `fallback`
    (e.g. entry_price) if the fetch fails or returns an invalid value. This is
    what keeps the Open Positions P/L in sync with the real market instead of
    the stale price stored on the trade at open time (current_price on the
    PaperTrade row is only ever set once, at open/close — it is never updated
    while a trade is open — so reading it directly always showed 0% P/L).
    """
    try:
        data = await get_token_card_info(contract)
        if data:
            price = _to_float(data.get("price"))
            if price > 0:
                return price
    except Exception as e:
        logger.warning(f"positions: live price fetch failed for {contract}: {e}")
    return fallback


@router.callback_query(F.data == "paper:positions")
async def cb_positions(callback: CallbackQuery):
    try:
        trades = await get_open_trades(callback.from_user.id)

        if not trades:
            await callback.message.answer("📭 No open positions.")
            await callback.answer()
            return

        await callback.answer()

        trades = trades[:10]

        # Fetch all live prices concurrently so opening the Positions list
        # doesn't get slower the more positions a user has open.
        live_prices = await asyncio.gather(*[
            _get_live_price(t.contract, t.current_price or t.entry_price) for t in trades
        ])

        for t, current_price in zip(trades, live_prices):
            remaining = t.remaining_quantity if t.remaining_quantity is not None else t.token_quantity
            cur_val = remaining * current_price
            cost = t.usd_invested * (remaining / t.token_quantity) if t.token_quantity > 0 else 0
            pnl = cur_val - cost
            pnl_pct = (pnl / cost) * 100 if cost > 0 else 0
            emoji = "🟢" if pnl >= 0 else "🔴"

            # TP/SL progress, based on live price movement vs entry, so it's
            # synchronized with the same current_price used for PnL above.
            change_pct = ((current_price - t.entry_price) / t.entry_price) * 100 if t.entry_price > 0 else 0
            tp_sl_bits = []
            if t.take_profit_pct:
                tp_prog = max(0, min(100, (change_pct / t.take_profit_pct) * 100)) if t.take_profit_pct > 0 else 0
                tp_sl_bits.append(f"TP {t.take_profit_pct}% ({tp_prog:.0f}%)")
            if t.stop_loss_pct:
                sl_prog = max(0, min(100, (-change_pct / t.stop_loss_pct) * 100)) if t.stop_loss_pct > 0 else 0
                tp_sl_bits.append(f"SL {t.stop_loss_pct}% ({sl_prog:.0f}%)")
            tp_sl_line = " | ".join(tp_sl_bits) if tp_sl_bits else "No TP/SL set"
            dca_line = f"   🧬 DCA fills: {t.dca_fills}\n" if getattr(t, "dca_fills", 0) else ""

            text = (
                f"<b>{html.escape(t.name or 'Unknown')} (${t.symbol})</b>\n"
                f"   Entry: ${t.entry_price:.8f}\n"
                f"   Current: ${current_price:.8f}\n"
                f"   {emoji} PnL: {format_usd(pnl)} ({pnl_pct:+.1f}%)\n"
                f"   {tp_sl_line}\n"
                f"{dca_line}"
            )

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🌙 Sell 25%", callback_data=f"paper_sell:{t.id}:25"),
                    InlineKeyboardButton(text="🌙 Sell 50%", callback_data=f"paper_sell:{t.id}:50"),
                ],
                [
                    InlineKeyboardButton(text="❌ Close 100%", callback_data=f"paper_sell:{t.id}:100"),
                ],
                [
                    InlineKeyboardButton(text="✏️ Custom Sell", callback_data=f"paper_sell_custom:{t.id}"),
                ],
                [
                    InlineKeyboardButton(text="📊 Generate PnL Card", callback_data=f"paper_pnlcard:{t.id}"),
                ],
            ])

            await callback.message.answer(text, reply_markup=kb)

    except Exception as e:
        logger.error(f"paper positions error: {e}")
        await callback.answer("Could not load positions.", show_alert=True)


async def _execute_sell(trade_id: int, price: float, pct: float) -> tuple[bool, str]:
    """
    Shared by preset sell buttons (25%/50%/100%) and the custom sell input flow.
    Returns (ok, text) where text is either the result message or an error message.
    """
    if pct >= 100:
        result = await close_paper_trade(trade_id, price, "closed_manual")
        if not result.get("ok"):
            return False, result.get("reason", "Could not close.")

        text = (
            "🖐️ <b>Manual Close</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📛 {result.get('name')} (${result.get('symbol')})\n"
            f"💰 Exit: ${result.get('exit_price', 0):.8f}\n"
            f"{'🟢' if result.get('pnl_usd', 0) >= 0 else '🔴'} PnL: {format_usd(result.get('pnl_usd', 0))} "
            f"({result.get('pnl_pct', 0):+.1f}%)"
        )
        return True, text

    result = await partial_close_paper_trade(trade_id, price, pct, "moonbag_sell")
    if not result.get("ok"):
        return False, result.get("reason", "Could not sell.")

    status_line = "Position fully closed." if result.get("fully_closed") else f"Remaining: {result.get('remaining_quantity', 0):.4f} tokens (moonbag active)."
    text = (
        f"🌙 <b>Sell {pct:.0f}%</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📛 {result.get('name')} (${result.get('symbol')})\n"
        f"💰 Sell Price: ${result.get('exit_price', 0):.8f}\n"
        f"💵 Proceeds: {format_usd(result.get('proceeds', 0))}\n"
        f"{'🟢' if result.get('pnl_usd', 0) >= 0 else '🔴'} PnL: {format_usd(result.get('pnl_usd', 0))} "
        f"({result.get('pnl_pct', 0):+.1f}%)\n\n"
        f"{status_line}"
    )
    return True, text


@router.callback_query(F.data.startswith("paper_sell:"))
async def cb_paper_sell(callback: CallbackQuery):
    """
    Handles both full close (100%) and moonbag partial sells (<100%).
    Callback data format: paper_sell:<trade_id>:<pct>
    """
    try:
        _, trade_id_str, pct_str = callback.data.split(":")
        trade_id = int(trade_id_str)
        pct = float(pct_str)

        trade = await get_trade_by_id(trade_id, callback.from_user.id)
        if not trade:
            await callback.answer("Trade not found.", show_alert=True)
            return

        # Acknowledge here, before the price lookup — get_token_card_info is
        # an external API call and can be slow under upstream rate limits,
        # so answering first keeps the spinner from sitting through it. A
        # callback can only be answered once, so the failure cases below are
        # now reported as chat messages instead of toast alerts.
        await callback.answer()

        data = await get_token_card_info(trade.contract)
        if not data:
            await callback.message.answer("⚠️ Could not fetch current price.")
            return

        price = _to_float(data.get("price"))
        if price <= 0:
            await callback.message.answer("⚠️ Invalid current price.")
            return

        ok, text = await _execute_sell(trade_id, price, pct)
        if not ok:
            await callback.message.answer(f"⚠️ {text}")
            return

        await callback.message.answer(text)

    except Exception as e:
        logger.error(f"paper sell error: {e}")
        try:
            await callback.answer("Could not process sell.", show_alert=True)
        except Exception:
            # callback.answer() was already called earlier in the try block
            # (see comment above) — Telegram rejects a second answer, so
            # fall back to a plain chat message rather than failing silently.
            try:
                await callback.message.answer("⚠️ Could not process sell. Please try again.")
            except Exception:
                pass


@router.callback_query(F.data.startswith("paper_pnlcard:"))
async def cb_pnl_card(callback: CallbackQuery):
    """
    Generates a PnL card image for an open position (feature: Generate PnL Card).
    """
    try:
        trade_id = int(callback.data.split(":")[1])
        trade = await get_trade_by_id(trade_id, callback.from_user.id)

        if not trade:
            await callback.answer("Position not found.", show_alert=True)
            return

        await callback.answer("Generating PnL card...")

        if trade.status == "open":
            data = await get_token_card_info(trade.contract)
            current_price = _to_float(data.get("price")) if data else (trade.current_price or trade.entry_price)

            remaining = trade.remaining_quantity if trade.remaining_quantity is not None else trade.token_quantity
            cur_val = remaining * current_price
            cost = trade.usd_invested * (remaining / trade.token_quantity) if trade.token_quantity > 0 else 0
            pnl_usd = cur_val - cost
            pnl_pct = (pnl_usd / cost) * 100 if cost > 0 else 0
            status_label = "OPEN"
        else:
            current_price = trade.exit_price or trade.current_price or trade.entry_price
            pnl_usd = trade.pnl_usd or 0.0
            pnl_pct = trade.pnl_pct or 0.0
            status_label = (trade.exit_reason or trade.status or "CLOSED").replace("closed_", "").upper()

        opened_str = trade.opened_at.strftime("%Y-%m-%d %H:%M UTC") if trade.opened_at else "N/A"

        card_data = {
            "name": trade.name or "Unknown",
            "symbol": trade.symbol or "???",
            "entry_price": trade.entry_price,
            "current_price": current_price,
            "usd_invested": trade.usd_invested,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "status": status_label,
            "opened_at_str": opened_str,
            "tp_pct": trade.take_profit_pct if trade.status == "open" else None,
            "sl_pct": trade.stop_loss_pct if trade.status == "open" else None,
        }

        png_bytes = await generate_pnl_card_image(card_data)

        if not png_bytes:
            await callback.message.answer("⚠️ Could not generate PnL card right now.")
            return

        photo = BufferedInputFile(png_bytes, filename=f"{trade.symbol}_pnl.png")
        await callback.message.answer_photo(photo)

    except Exception as e:
        logger.error(f"pnl card error: {e}")
        try:
            await callback.answer("Could not generate PnL card.", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "paper:history")
async def cb_history(callback: CallbackQuery):
    try:
        trades = await get_all_trades(callback.from_user.id, limit=10)

        if not trades:
            await callback.message.answer("📭 No trade history yet.")
            await callback.answer()
            return

        await callback.answer()

        for t in trades[:10]:
            emoji = "🟢" if t.pnl_usd >= 0 else "🔴"
            reason = (t.exit_reason or t.status or "").replace("closed_", "").upper()

            text = (
                f"<b>{t.symbol}</b> — {reason}\n"
                f"   {emoji} {format_usd(t.pnl_usd)} ({t.pnl_pct:+.1f}%)\n"
                f"   Invested: {format_usd(t.usd_invested)}"
            )

            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📊 Generate PnL Card", callback_data=f"paper_pnlcard:{t.id}"),
            ]])

            await callback.message.answer(text, reply_markup=kb)

    except Exception as e:
        logger.error(f"paper history error: {e}")
        await callback.answer("Could not load history.", show_alert=True)


@router.callback_query(F.data == "paper:settings")
async def cb_settings(callback: CallbackQuery):
    try:
        settings = await get_or_create_settings(callback.from_user.id)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Auto Buy: {'✅' if settings.auto_buy else '❌'}",
                    callback_data="paper:toggle_autobuy"
                ),
            ],
            [
                InlineKeyboardButton(text="$10", callback_data="paper:buy_amt:10"),
                InlineKeyboardButton(text="$25", callback_data="paper:buy_amt:25"),
                InlineKeyboardButton(text="$50", callback_data="paper:buy_amt:50"),
                InlineKeyboardButton(text="$100", callback_data="paper:buy_amt:100"),
            ],
            [
                InlineKeyboardButton(text="✏️ Custom Buy Amount", callback_data="paper:custom_buy"),
            ],
            [
                InlineKeyboardButton(text="TP 50%", callback_data="paper:tp:50"),
                InlineKeyboardButton(text="TP 100%", callback_data="paper:tp:100"),
                InlineKeyboardButton(text="TP 200%", callback_data="paper:tp:200"),
            ],
            [
                InlineKeyboardButton(
                    text=f"✏️ Custom Stop Loss ({settings.stop_loss_pct:g}%)",
                    callback_data="paper:custom_sl",
                ),
            ],
            [
                InlineKeyboardButton(text="Daily Limit: 5", callback_data="paper:dailylimit:5"),
                InlineKeyboardButton(text="10", callback_data="paper:dailylimit:10"),
                InlineKeyboardButton(text="20", callback_data="paper:dailylimit:20"),
                InlineKeyboardButton(text="50", callback_data="paper:dailylimit:50"),
            ],
            [
                InlineKeyboardButton(text="🧰 Auto-Buy Filters", callback_data="paper:filters"),
            ],
            [
                InlineKeyboardButton(text="🧬 DCA Strategy", callback_data="paper:dca"),
            ],
            [
                InlineKeyboardButton(text="🔄 Reset Portfolio", callback_data="paper:reset_confirm"),
            ],
        ])

        await callback.message.answer("⚙️ <b>Paper Trading Settings</b>\n\nTap to change:", reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"paper settings error: {e}")
        await callback.answer("Could not load settings.", show_alert=True)


@router.callback_query(F.data == "paper:toggle_autobuy")
async def cb_toggle_autobuy(callback: CallbackQuery):
    try:
        settings = await get_or_create_settings(callback.from_user.id)
        new_val = not settings.auto_buy
        await update_setting(callback.from_user.id, "auto_buy", new_val)
        await callback.answer(f"Auto Buy: {'Enabled' if new_val else 'Disabled'}", show_alert=True)
    except Exception as e:
        logger.error(f"toggle autobuy error: {e}")
        await callback.answer("Could not update.", show_alert=True)


@router.callback_query(F.data.startswith("paper:buy_amt:"))
async def cb_buy_amt(callback: CallbackQuery):
    try:
        amount = float(callback.data.split(":")[-1])
        await update_setting(callback.from_user.id, "buy_amount_usd", amount)
        await callback.answer(f"Buy Amount set to ${amount:.0f}", show_alert=True)
    except Exception as e:
        logger.error(f"buy amt error: {e}")
        await callback.answer("Could not update.", show_alert=True)


@router.callback_query(F.data.startswith("paper:tp:"))
async def cb_tp(callback: CallbackQuery):
    try:
        tp = float(callback.data.split(":")[-1])
        await update_setting(callback.from_user.id, "take_profit_pct", tp)
        await callback.answer(f"Take Profit set to {tp:.0f}%", show_alert=True)
    except Exception as e:
        logger.error(f"tp error: {e}")
        await callback.answer("Could not update.", show_alert=True)


@router.callback_query(F.data == "paper:custom_sl")
async def cb_custom_sl_prompt(callback: CallbackQuery, state: FSMContext):
    try:
        settings = await get_or_create_settings(callback.from_user.id)
        await state.set_state(PaperAmountStates.waiting_sl_pct)
        await callback.message.answer(
            "✏️ <b>Custom Stop Loss</b>\n\n"
            f"Current Stop Loss: <b>{settings.stop_loss_pct:g}%</b>\n\n"
            "Send the stop-loss percentage to use for all future trades, "
            "e.g. <code>5</code>, <code>12</code>, or <code>35</code>.\n"
            "This applies to both paper and live trades until you change it again.\n\n"
            "Send /cancel to cancel."
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"custom sl prompt error: {e}")
        await callback.answer("Could not start custom stop loss.", show_alert=True)


@router.message(PaperAmountStates.waiting_sl_pct)
async def process_custom_sl(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text.lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.")
        return

    if text.startswith("/"):
        await state.clear()
        await message.answer("Cancelled custom stop loss. Please resend your command.")
        return

    sl, error = _parse_sl_pct(text)
    if error:
        await message.answer(f"⚠️ {error}\n\nTry again, or send /cancel.")
        return

    try:
        await update_setting(message.from_user.id, "stop_loss_pct", sl)
        await state.clear()
        await message.answer(
            f"✅ Stop Loss set to {sl:g}%\n\n"
            "This will automatically apply to all subsequent trades until you update it again."
        )
    except Exception as e:
        logger.error(f"process custom sl error: {e}")
        await state.clear()
        await message.answer("⚠️ Could not update your stop loss. Please try again from /paper settings.")


@router.callback_query(F.data.startswith("paper:dailylimit:"))
async def cb_daily_limit(callback: CallbackQuery):
    try:
        limit = int(callback.data.split(":")[-1])
        await update_setting(callback.from_user.id, "daily_trade_limit", limit)
        await callback.answer(f"Daily auto-buy limit set to {limit}/day", show_alert=True)
    except Exception as e:
        logger.error(f"daily limit error: {e}")
        await callback.answer("Could not update.", show_alert=True)


@router.callback_query(F.data == "paper:reset_confirm")
async def cb_reset_confirm(callback: CallbackQuery):
    try:
        await callback.answer()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yes, Reset", callback_data="paper:reset_execute"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="paper:reset_cancel"),
        ]])
        await callback.message.answer(
            "⚠️ <b>Reset Paper Portfolio?</b>\n\n"
            "This will close any open positions and reset your balance and "
            "all-time stats back to the starting amount. This cannot be undone.\n\n"
            "Are you sure?",
            reply_markup=kb,
        )
    except Exception as e:
        logger.error(f"reset confirm error: {e}")
        await callback.answer("Could not open confirmation.", show_alert=True)


@router.callback_query(F.data == "paper:reset_cancel")
async def cb_reset_cancel(callback: CallbackQuery):
    await callback.answer("Reset cancelled.")
    await callback.message.answer("❌ Reset cancelled. Your portfolio is unchanged.")


@router.callback_query(F.data == "paper:reset_execute")
async def cb_reset_execute(callback: CallbackQuery):
    try:
        result = await reset_portfolio(callback.from_user.id)
        await callback.answer("Portfolio reset.", show_alert=True)
        await callback.message.answer(
            "✅ <b>Portfolio Reset Complete</b>\n\n"
            f"💰 New Balance: {format_usd(result.get('new_balance', 0))}\n"
            f"📦 Positions Closed: {result.get('closed_positions', 0)}\n\n"
            "Use /paper to view your fresh dashboard."
        )
    except Exception as e:
        logger.error(f"reset execute error: {e}")
        await callback.answer("Could not reset portfolio.", show_alert=True)


# ============================================================
# User-Configurable Auto-Buy Filters
# ============================================================

@router.callback_query(F.data == "paper:filters")
async def cb_filters(callback: CallbackQuery):
    try:
        text, keyboard = await build_filters_view(callback.from_user.id)
        await callback.message.answer(text, reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"paper filters view error: {e}")
        await callback.answer("Could not load filters.", show_alert=True)


@router.callback_query(F.data == "paper:filter_toggle")
async def cb_filter_toggle(callback: CallbackQuery):
    try:
        filters = await get_or_create_filters(callback.from_user.id)
        new_val = not filters.enabled
        await update_filter(callback.from_user.id, "enabled", new_val)
        await callback.answer(f"Filters {'Resumed' if new_val else 'Paused'}", show_alert=True)
        text, keyboard = await build_filters_view(callback.from_user.id)
        await callback.message.answer(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"filter toggle error: {e}")
        await callback.answer("Could not update.", show_alert=True)


@router.callback_query(F.data == "paper:filter_clear_all")
async def cb_filter_clear_all(callback: CallbackQuery):
    try:
        for field in (
            "min_market_cap", "max_market_cap",
            "min_holders", "min_liquidity_usd",
            "max_bundle_pct", "max_dev_holding_pct",
            "min_age_hours", "max_age_hours",
        ):
            await update_filter(callback.from_user.id, field, None)

        await callback.answer("All filters cleared.", show_alert=True)
        text, keyboard = await build_filters_view(callback.from_user.id)
        await callback.message.answer(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"filter clear error: {e}")
        await callback.answer("Could not clear filters.", show_alert=True)


@router.callback_query(F.data.startswith("paper:filter_set:"))
async def cb_filter_set_prompt(callback: CallbackQuery, state: FSMContext):
    field_key = callback.data.split(":")[-1]
    field = FILTER_FIELDS.get(field_key)

    if not field:
        await callback.answer("Unknown filter.", show_alert=True)
        return

    try:
        await state.set_state(PaperFilterStates.waiting_filter_value)
        await state.update_data(field_key=field_key)

        await callback.message.answer(
            f"✏️ <b>{field['label']}</b>\n\n"
            f"Send a value, e.g. <code>{field['example']}</code>.\n"
            "Send <code>clear</code> to remove this filter.\n"
            "Send /cancel to cancel."
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"filter set prompt error: {e}")
        await callback.answer("Could not start filter update.", show_alert=True)


@router.message(PaperFilterStates.waiting_filter_value)
async def process_filter_value(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text.lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.")
        return

    if text.startswith("/"):
        await state.clear()
        await message.answer("Cancelled filter update. Please resend your command.")
        return

    fsm_data = await state.get_data()
    field_key = fsm_data.get("field_key")
    field = FILTER_FIELDS.get(field_key)

    if not field:
        await state.clear()
        await message.answer("⚠️ Something went wrong. Please try again from /paper settings.")
        return

    try:
        if field["kind"] == "range":
            if text.lower() in ("clear", "any"):
                await update_filter(message.from_user.id, field["min_attr"], None)
                await update_filter(message.from_user.id, field["max_attr"], None)
                await state.clear()
                await message.answer(f"✅ {field['label']} filter cleared.")
                return

            min_v, max_v, error = _parse_range_filter_input(text)
            if error:
                await message.answer(f"⚠️ {error}\n\nTry again, or send /cancel.")
                return

            await update_filter(message.from_user.id, field["min_attr"], min_v)
            await update_filter(message.from_user.id, field["max_attr"], max_v)
            await state.clear()
            await message.answer(f"✅ {field['label']} set to {_format_range_display(min_v, max_v)}")
        else:
            if text.lower() in ("clear", "any"):
                await update_filter(message.from_user.id, field["attr"], None)
                await state.clear()
                await message.answer(f"✅ {field['label']} filter cleared.")
                return

            value, error = _parse_single_filter_input(text)
            if error:
                await message.answer(f"⚠️ {error}\n\nTry again, or send /cancel.")
                return

            await update_filter(message.from_user.id, field["attr"], value)
            await state.clear()
            await message.answer(f"✅ {field['label']} set to {value:g}")
    except Exception as e:
        logger.error(f"process filter value error: {e}")
        await state.clear()
        await message.answer("⚠️ Could not update that filter. Please try again from /paper settings.")


# ============================================================
# DCA (Dollar-Cost Averaging) Strategy
# ============================================================

def _fmt_dca_levels(settings) -> str:
    custom = get_dca_custom_levels(settings)
    if custom:
        return "\n".join(f"   • -{lv['drop_pct']:g}% → {format_usd(lv['amount_usd'])}" for lv in custom)
    max_entries = settings.max_entries or 3
    remaining = max(0, max_entries - 1)
    return (
        f"   • Every -{settings.default_trigger_drop_pct:g}% → "
        f"{format_usd(settings.default_entry_amount_usd)} "
        f"(up to {remaining} add-in{'s' if remaining != 1 else ''})"
    )


async def build_dca_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    settings = await get_or_create_dca_settings(user_id)
    custom = get_dca_custom_levels(settings)

    status = "✅ Enabled" if settings.enabled else "❌ Disabled"
    mode = "Custom ladder" if custom else "Default (flat)"

    text = (
        "🧬 <b>DCA Strategy</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Status: <b>{status}</b>\n"
        f"Mode: <b>{mode}</b>\n\n"
        "When enabled, Auto-Buy adds to an already-open position instead "
        "of opening a duplicate one, and the bot automatically buys more "
        "as price drops through your configured levels — averaging down "
        "your entry price automatically.\n\n"
        "📉 <b>Levels</b>\n"
        f"{_fmt_dca_levels(settings)}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 AlphaPulse Paper Trading"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=("⏸️ Disable DCA" if settings.enabled else "▶️ Enable DCA"),
                callback_data="paper:dca_toggle",
            ),
        ],
        [
            InlineKeyboardButton(text="🔢 Max Entries", callback_data="paper:dca_set_max"),
        ],
        [
            InlineKeyboardButton(text="📉 Default Trigger %", callback_data="paper:dca_set_trigger"),
            InlineKeyboardButton(text="💵 Default Amount", callback_data="paper:dca_set_amount"),
        ],
        [
            InlineKeyboardButton(text="✏️ Custom DCA Ladder", callback_data="paper:dca_set_custom"),
        ],
        [
            InlineKeyboardButton(text="🗑️ Clear Custom Ladder", callback_data="paper:dca_clear_custom"),
        ],
        [InlineKeyboardButton(text="⬅️ Back to Settings", callback_data="paper:settings")],
    ])

    return text, keyboard


@router.callback_query(F.data == "paper:dca")
async def cb_dca_view(callback: CallbackQuery):
    try:
        text, keyboard = await build_dca_view(callback.from_user.id)
        await callback.message.answer(text, reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"dca view error: {e}")
        await callback.answer("Could not load DCA settings.", show_alert=True)


@router.callback_query(F.data == "paper:dca_toggle")
async def cb_dca_toggle(callback: CallbackQuery):
    try:
        settings = await get_or_create_dca_settings(callback.from_user.id)
        new_val = not settings.enabled
        await update_dca_setting(callback.from_user.id, "enabled", new_val)
        await callback.answer(f"DCA {'Enabled' if new_val else 'Disabled'}", show_alert=True)
        text, keyboard = await build_dca_view(callback.from_user.id)
        await callback.message.answer(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"dca toggle error: {e}")
        await callback.answer("Could not update.", show_alert=True)


@router.callback_query(F.data == "paper:dca_set_max")
async def cb_dca_set_max_prompt(callback: CallbackQuery, state: FSMContext):
    try:
        settings = await get_or_create_dca_settings(callback.from_user.id)
        await state.set_state(PaperDCAStates.waiting_max_entries)
        await callback.message.answer(
            "✏️ <b>Max DCA Entries</b>\n\n"
            f"Current: <b>{settings.max_entries or 3}</b> (initial buy + add-ins)\n\n"
            "Send a number from 2 to 6, e.g. <code>3</code>.\n"
            "(Only used in Default mode — a Custom ladder sets its own count.)\n\n"
            "Send /cancel to cancel."
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"dca set max prompt error: {e}")
        await callback.answer("Could not start update.", show_alert=True)


@router.message(PaperDCAStates.waiting_max_entries)
async def process_dca_max_entries(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.")
        return
    if text.startswith("/"):
        await state.clear()
        await message.answer("Cancelled. Please resend your command.")
        return

    try:
        value = int(text)
        if value < 2 or value > 6:
            await message.answer("⚠️ Please enter a number between 2 and 6.\n\nTry again, or send /cancel.")
            return

        await update_dca_setting(message.from_user.id, "max_entries", value)
        await state.clear()
        await message.answer(f"✅ Max DCA entries set to {value}.")
    except (ValueError, TypeError):
        await message.answer("⚠️ That doesn't look like a valid number.\n\nTry again, or send /cancel.")
    except Exception as e:
        logger.error(f"process dca max entries error: {e}")
        await state.clear()
        await message.answer("⚠️ Could not update. Please try again from /paper → DCA Strategy.")


@router.callback_query(F.data == "paper:dca_set_trigger")
async def cb_dca_set_trigger_prompt(callback: CallbackQuery, state: FSMContext):
    try:
        settings = await get_or_create_dca_settings(callback.from_user.id)
        await state.set_state(PaperDCAStates.waiting_trigger_pct)
        await callback.message.answer(
            "✏️ <b>Default DCA Trigger %</b>\n\n"
            f"Current: <b>{settings.default_trigger_drop_pct:g}%</b>\n\n"
            "This is how far price must drop below your average entry "
            "before the bot buys more. Send a number, e.g. <code>15</code>.\n\n"
            "Send /cancel to cancel."
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"dca set trigger prompt error: {e}")
        await callback.answer("Could not start update.", show_alert=True)


@router.message(PaperDCAStates.waiting_trigger_pct)
async def process_dca_trigger_pct(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.")
        return
    if text.startswith("/"):
        await state.clear()
        await message.answer("Cancelled. Please resend your command.")
        return

    value, error = _parse_single_filter_input(text.replace("%", ""))
    if error or not value or value <= 0 or value >= 100:
        await message.answer("⚠️ Please enter a percentage between 0 and 100.\n\nTry again, or send /cancel.")
        return

    try:
        await update_dca_setting(message.from_user.id, "default_trigger_drop_pct", value)
        await state.clear()
        await message.answer(f"✅ Default DCA trigger set to {value:g}%.")
    except Exception as e:
        logger.error(f"process dca trigger error: {e}")
        await state.clear()
        await message.answer("⚠️ Could not update. Please try again from /paper → DCA Strategy.")


@router.callback_query(F.data == "paper:dca_set_amount")
async def cb_dca_set_amount_prompt(callback: CallbackQuery, state: FSMContext):
    try:
        settings = await get_or_create_dca_settings(callback.from_user.id)
        await state.set_state(PaperDCAStates.waiting_entry_amount)
        await callback.message.answer(
            "✏️ <b>Default DCA Amount</b>\n\n"
            f"Current: <b>{format_usd(settings.default_entry_amount_usd)}</b>\n\n"
            "USD amount used for each default-mode DCA add-in. Send a "
            "number, e.g. <code>25</code>.\n\n"
            "Send /cancel to cancel."
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"dca set amount prompt error: {e}")
        await callback.answer("Could not start update.", show_alert=True)


@router.message(PaperDCAStates.waiting_entry_amount)
async def process_dca_entry_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.")
        return
    if text.startswith("/"):
        await state.clear()
        await message.answer("Cancelled. Please resend your command.")
        return

    value, error = _parse_single_filter_input(text)
    if error or not value or value <= 0:
        await message.answer("⚠️ Please enter a USD amount greater than zero.\n\nTry again, or send /cancel.")
        return

    try:
        await update_dca_setting(message.from_user.id, "default_entry_amount_usd", value)
        await state.clear()
        await message.answer(f"✅ Default DCA amount set to {format_usd(value)}.")
    except Exception as e:
        logger.error(f"process dca amount error: {e}")
        await state.clear()
        await message.answer("⚠️ Could not update. Please try again from /paper → DCA Strategy.")


@router.callback_query(F.data == "paper:dca_set_custom")
async def cb_dca_set_custom_prompt(callback: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(PaperDCAStates.waiting_custom_levels)
        await callback.message.answer(
            "✏️ <b>Custom DCA Ladder</b>\n\n"
            "Define your own DCA strategy as <code>drop%:amount</code> pairs, "
            "comma-separated — one per add-in, up to 5.\n\n"
            "Example: <code>15:20, 30:20, 50:30</code>\n"
            "→ buys $20 at -15%, another $20 at -30%, and $30 at -50% "
            "below your average entry.\n\n"
            "Send /cancel to cancel."
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"dca set custom prompt error: {e}")
        await callback.answer("Could not start update.", show_alert=True)


@router.message(PaperDCAStates.waiting_custom_levels)
async def process_dca_custom_levels(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.")
        return
    if text.startswith("/"):
        await state.clear()
        await message.answer("Cancelled. Please resend your command.")
        return

    levels, error = parse_custom_dca_levels(text)
    if error:
        await message.answer(f"⚠️ {error}\n\nTry again, or send /cancel.")
        return

    try:
        await set_dca_custom_levels(message.from_user.id, levels)
        await state.clear()
        summary = ", ".join(f"-{lv['drop_pct']:g}%→{format_usd(lv['amount_usd'])}" for lv in levels)
        await message.answer(f"✅ Custom DCA ladder set: {summary}")
    except Exception as e:
        logger.error(f"process dca custom levels error: {e}")
        await state.clear()
        await message.answer("⚠️ Could not update. Please try again from /paper → DCA Strategy.")


@router.callback_query(F.data == "paper:dca_clear_custom")
async def cb_dca_clear_custom(callback: CallbackQuery):
    try:
        await set_dca_custom_levels(callback.from_user.id, None)
        await callback.answer("Custom ladder cleared — using default mode.", show_alert=True)
        text, keyboard = await build_dca_view(callback.from_user.id)
        await callback.message.answer(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"dca clear custom error: {e}")
        await callback.answer("Could not clear custom ladder.", show_alert=True)


# ============================================================
# Custom Buy Amount (manual USD input)
# ============================================================

@router.callback_query(F.data == "paper:custom_buy")
async def cb_custom_buy_prompt(callback: CallbackQuery, state: FSMContext):
    try:
        portfolio = await get_or_create_portfolio(callback.from_user.id)
        await state.set_state(PaperAmountStates.waiting_buy_amount)
        await callback.message.answer(
            "✏️ <b>Custom Buy Amount</b>\n\n"
            f"Your available virtual balance: <b>{format_usd(portfolio.balance)}</b>\n\n"
            "Send the USD amount you want to use for each buy, e.g. <code>15</code> or <code>37.50</code>.\n"
            "Send /cancel to cancel."
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"custom buy prompt error: {e}")
        await callback.answer("Could not start custom buy amount.", show_alert=True)


@router.message(PaperAmountStates.waiting_buy_amount)
async def process_custom_buy_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text.lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.")
        return

    if text.startswith("/"):
        await state.clear()
        await message.answer("Cancelled custom buy amount. Please resend your command.")
        return

    try:
        portfolio = await get_or_create_portfolio(message.from_user.id)
        amount, error = _parse_buy_amount(text, portfolio.balance)

        if error:
            await message.answer(f"⚠️ {error}\n\nTry again, or send /cancel.")
            return

        await update_setting(message.from_user.id, "buy_amount_usd", amount)
        await state.clear()
        await message.answer(f"✅ Buy Amount set to {format_usd(amount)}")
    except Exception as e:
        logger.error(f"process custom buy amount error: {e}")
        await state.clear()
        await message.answer("⚠️ Could not update your buy amount. Please try again from /paper settings.")


# ============================================================
# Custom Sell Amount (manual % or USD input, partial or full)
# ============================================================

@router.callback_query(F.data.startswith("paper_sell_custom:"))
async def cb_custom_sell_prompt(callback: CallbackQuery, state: FSMContext):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Invalid position.", show_alert=True)
        return

    try:
        trade = await get_trade_by_id(trade_id, callback.from_user.id)
        if not trade or trade.status != "open":
            await callback.answer("Position not found or already closed.", show_alert=True)
            return

        data = await get_token_card_info(trade.contract)
        price = _to_float(data.get("price")) if data else (trade.current_price or trade.entry_price)
        remaining = trade.remaining_quantity if trade.remaining_quantity is not None else trade.token_quantity
        current_value = remaining * price

        await state.set_state(PaperAmountStates.waiting_sell_amount)
        await state.update_data(trade_id=trade_id)

        await callback.message.answer(
            f"✏️ <b>Custom Sell — {html.escape(trade.symbol or '???')}</b>\n\n"
            f"Position value: <b>{format_usd(current_value)}</b>\n\n"
            "Send how much you want to sell:\n"
            "• A percentage, e.g. <code>40%</code>\n"
            "• Or a USD amount, e.g. <code>25</code>\n\n"
            "Send /cancel to cancel."
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"custom sell prompt error: {e}")
        await callback.answer("Could not start custom sell.", show_alert=True)


@router.message(PaperAmountStates.waiting_sell_amount)
async def process_custom_sell_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text.lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.")
        return

    if text.startswith("/"):
        await state.clear()
        await message.answer("Cancelled custom sell. Please resend your command.")
        return

    try:
        fsm_data = await state.get_data()
        trade_id = fsm_data.get("trade_id")
        trade = await get_trade_by_id(trade_id, message.from_user.id) if trade_id else None

        if not trade or trade.status != "open":
            await state.clear()
            await message.answer("⚠️ That position is no longer open.")
            return

        token_data = await get_token_card_info(trade.contract)
        price = _to_float(token_data.get("price")) if token_data else (trade.current_price or trade.entry_price)

        if price <= 0:
            await message.answer("⚠️ Could not fetch current price right now. Please try again shortly.")
            return

        remaining = trade.remaining_quantity if trade.remaining_quantity is not None else trade.token_quantity
        current_value = remaining * price

        pct, error = _parse_sell_amount(text, current_value)
        if error:
            await message.answer(f"⚠️ {error}\n\nTry again, or send /cancel.")
            return

        await state.clear()

        ok, result_text = await _execute_sell(trade_id, price, pct)
        if not ok:
            await message.answer(f"⚠️ {result_text}")
            return

        await message.answer(result_text)
    except Exception as e:
        logger.error(f"process custom sell amount error: {e}")
        await state.clear()
        await message.answer("⚠️ Could not process that sell. Please try again from your open positions.")


# ============================================================
# PnL Calendar
# ============================================================

def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    m = month - 1 + delta
    y = year + m // 12
    m = m % 12 + 1
    return y, m


async def build_calendar_view(user_id: int, year: int, month: int) -> tuple[str, InlineKeyboardMarkup]:
    cal = await get_pnl_calendar(user_id, year, month)
    days_in_month = cal["days_in_month"]
    daily = cal["daily_pnl"]

    first_weekday, _ = cal_module.monthrange(year, month)  # Monday=0 .. Sunday=6

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=d, callback_data="paper:noop") for d in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]]
    ]

    week: list[InlineKeyboardButton] = []
    for _ in range(first_weekday):
        week.append(InlineKeyboardButton(text=" ", callback_data="paper:noop"))

    for day in range(1, days_in_month + 1):
        pnl = daily.get(day)
        if pnl is None:
            label = f"{day}"
        elif pnl > 0:
            label = f"🟢{day}"
        elif pnl < 0:
            label = f"🔴{day}"
        else:
            label = f"⚪{day}"

        week.append(InlineKeyboardButton(text=label, callback_data=f"paper:calday:{year}:{month}:{day}"))

        if len(week) == 7:
            rows.append(week)
            week = []

    if week:
        while len(week) < 7:
            week.append(InlineKeyboardButton(text=" ", callback_data="paper:noop"))
        rows.append(week)

    prev_y, prev_m = _shift_month(year, month, -1)
    next_y, next_m = _shift_month(year, month, 1)
    month_label = f"{cal_module.month_name[month]} {year}"

    rows.append([
        InlineKeyboardButton(text="◀️", callback_data=f"paper:calendar:{prev_y}:{prev_m}"),
        InlineKeyboardButton(text=month_label, callback_data="paper:noop"),
        InlineKeyboardButton(text="▶️", callback_data=f"paper:calendar:{next_y}:{next_m}"),
    ])
    rows.append([InlineKeyboardButton(text="🖼️ Image View", callback_data=f"paper:calimg:{year}:{month}")])
    rows.append([InlineKeyboardButton(text="⬅️ Back to Dashboard", callback_data="paper:dashboard")])

    total = cal["total_pnl"]
    total_emoji = "🟢" if total >= 0 else "🔴"

    text = (
        f"🗓️ <b>PnL Calendar — {month_label}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{total_emoji} Month PnL: <b>{format_usd(total)}</b>\n"
        f"🟢 Green Days: <b>{cal['green_days']}</b>   🔴 Red Days: <b>{cal['red_days']}</b>\n\n"
        "Tap a day to see its realized PnL.\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 AlphaPulse Paper Trading"
    )

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("paper:calendar:"))
async def cb_calendar(callback: CallbackQuery):
    try:
        parts = callback.data.split(":")
        year, month = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Invalid calendar request.", show_alert=True)
        return

    try:
        text, keyboard = await build_calendar_view(callback.from_user.id, year, month)
        await callback.message.answer(text, reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"paper calendar error: {e}")
        await callback.answer("Could not load PnL calendar.", show_alert=True)


@router.callback_query(F.data.startswith("paper:calimg:"))
async def cb_calendar_image(callback: CallbackQuery):
    try:
        parts = callback.data.split(":")
        year, month = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Invalid calendar request.", show_alert=True)
        return

    try:
        cal = await get_pnl_calendar(callback.from_user.id, year, month)
        png_bytes = await generate_calendar_image(cal)
        if not png_bytes:
            await callback.answer("Could not render calendar image.", show_alert=True)
            return

        photo = BufferedInputFile(png_bytes, filename="pnl_calendar.png")
        await callback.message.answer_photo(photo)
        await callback.answer()
    except Exception as e:
        logger.error(f"paper calendar image error: {e}")
        await callback.answer("Could not load calendar image.", show_alert=True)


@router.callback_query(F.data.startswith("paper:calday:"))
async def cb_calendar_day(callback: CallbackQuery):
    try:
        _, _, year_str, month_str, day_str = callback.data.split(":")
        year, month, day = int(year_str), int(month_str), int(day_str)
    except ValueError:
        await callback.answer("Invalid day.", show_alert=True)
        return

    try:
        cal = await get_pnl_calendar(callback.from_user.id, year, month)
        pnl = cal["daily_pnl"].get(day)

        if pnl is None:
            await callback.answer(f"{year}-{month:02d}-{day:02d}: No trades closed.", show_alert=True)
        else:
            emoji = "🟢" if pnl >= 0 else "🔴"
            await callback.answer(f"{emoji} {year}-{month:02d}-{day:02d}: {format_usd(pnl)}", show_alert=True)
    except Exception as e:
        logger.error(f"paper calendar day error: {e}")
        await callback.answer("Could not load that day.", show_alert=True)
