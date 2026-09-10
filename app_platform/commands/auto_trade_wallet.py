"""app_platform/commands/auto_trade_wallet.py

The new `/wallet` Telegram command (task requirement, spec Sec 36-39): a
single-screen readout of the Auto-Trade Engine's view of the user's
wallet -- balance, open positions, Auto-Trade status, buy amount, and
TP/SL settings -- plus inline buttons to manage those settings directly.

This is a NEW, separate command from the existing /wallet ("rw")
menu (app_platform/commands/real_wallet.py), which remains completely
untouched. /wallet only reads the user's RealWallet (existing, shared
infra) for address/balance, plus this engine's own AutoTradePolicy /
AutoTradePosition tables -- it does not read or write anything the
existing AutoBuy implementation owns.

The settings buttons below mirror the exact edit-flow convention already
used by /wallet's Auto-Buy Filters panel (app_platform/commands/
real_wallet.py's "rw:auto_filter_edit:<field>" -> FSM state -> validate
-> update_filter()), just under this command's own "atw:" callback-data
namespace so it can never collide with that router.
"""

from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from domain.trading.real.robinhood_wallet import get_real_wallet
from domain.trading.real.robinhood_swap import get_eth_balance
from domain.intelligence.robinhood_wallet_portfolio import get_wallet_portfolio_value, format_usd

from domain.trading.auto_trade import policy_service, position_manager, pnl
from app_platform.keyboards.auto_trade_wallet import auto_trade_wallet_menu_kb
from app_platform.keyboards.real_wallet import real_wallet_onboarding_kb

logger = logging.getLogger("WhaleAlpha.AutoTradeWalletCmd")
router = Router()


class AutoTradeWalletStates(StatesGroup):
    waiting_field_value = State()


# Labels shown in the "send a value" prompt for each editable field.
_EDIT_FIELD_LABELS = {
    "buy_amount_eth": "buy amount in ETH (e.g. 0.1)",
    "take_profit_pct": "take-profit percent (e.g. 50)",
    "stop_loss_pct": "stop-loss percent (e.g. 30)",
    "trailing_stop_pct": "trailing-stop percent (e.g. 15)",
    "trailing_activation_pct": "trailing activation percent — gain from entry required before trailing arms (e.g. 20)",
    "trailing_retracement_pct": "trailing retracement percent — pullback from the peak that triggers the sell (e.g. 10)",
    "daily_trade_limit": "daily trade limit, a whole number from 1 to 50",
    "max_open_positions": "max open positions, a whole number of 1 or more",
}

# Nullable policy fields that may be reset to "no constraint" by sending "clear".
_EDIT_FIELDS_CLEARABLE = {"take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "trailing_activation_pct", "trailing_retracement_pct"}

# Fields validated and stored as whole numbers rather than floats.
_EDIT_FIELDS_INT = {"daily_trade_limit", "max_open_positions"}


def _fmt_pct(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "\u2014"
    sign = "+" if (signed and value >= 0) else ""
    return f"{sign}{value:.1f}%"


def _status_line(policy) -> str:
    if policy.kill_switch:
        return "\U0001F6D1 KILL SWITCH ACTIVE"
    return "\U0001F7E2 ON" if policy.auto_trade_enabled else "\u26AA OFF"


async def _build_wallet_text(user_id: int) -> str:
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return (
            "\U0001F4BC <b>Auto-Trade Wallet</b>\n\n"
            "You don't have an active Real Wallet yet. Set one up with "
            "/wallet first, then come back here to configure Auto-Trade."
        )

    policy = await policy_service.get_or_create_policy(user_id)

    try:
        eth_balance = await get_eth_balance(wallet.public_key)
    except Exception as e:
        logger.error("[AutoTradeWallet] ETH balance fetch failed for %s: %s", wallet.public_key, e)
        eth_balance = None

    try:
        portfolio = await get_wallet_portfolio_value(wallet.public_key)
        portfolio_value_usd = portfolio["total_value_usd"] if portfolio else None
    except Exception as e:
        logger.error("[AutoTradeWallet] portfolio value fetch failed for %s: %s", wallet.public_key, e)
        portfolio_value_usd = None

    live_positions = await position_manager.get_live_positions_view(user_id)
    summary = await pnl.get_realized_pnl_summary(user_id)

    bal_line = f"{eth_balance:.4f} ETH" if eth_balance is not None else "\u2014"
    value_line = format_usd(portfolio_value_usd) if portfolio_value_usd is not None else "\u2014"

    tp_line = f"+{policy.take_profit_pct:g}%" if policy.take_profit_pct else "\u2014"
    sl_line = f"-{policy.stop_loss_pct:g}%" if policy.stop_loss_pct else "\u2014"
    trailing_line = f"{policy.trailing_stop_pct:g}%" if (policy.trailing_stop_enabled and policy.trailing_stop_pct) else "OFF"

    trailing_detail_line = None
    if policy.trailing_stop_enabled:
        activation_txt = f"{policy.trailing_activation_pct:g}% gain" if policy.trailing_activation_pct else "immediate"
        effective_retracement = policy.trailing_retracement_pct if policy.trailing_retracement_pct else policy.trailing_stop_pct
        retracement_txt = f"{effective_retracement:g}% pullback" if effective_retracement else "\u2014"
        trailing_detail_line = f"   \u21B3 Activation: {activation_txt}  |  Retracement: {retracement_txt}"

    lines = [
        "\U0001F916 <b>Auto-Trade Wallet</b>",
        "\u2501" * 21,
        "",
        f"\U0001F45B <b>Address:</b>\n<code>{wallet.public_key}</code>",
        "",
        f"\U0001F4B0 <b>ETH Balance:</b> {bal_line}",
        f"\U0001F4C8 <b>Portfolio Value:</b> {value_line}",
        "",
        f"\u2699\uFE0F <b>Auto-Trade:</b> {_status_line(policy)}",
        f"\U0001F4B5 <b>Buy Amount:</b> {policy.buy_amount_sol:.4f} ETH per trade",
        f"\U0001F3AF <b>Take-Profit:</b> {tp_line}  |  \U0001F6E1\uFE0F <b>Stop-Loss:</b> {sl_line}",
        f"\U0001F4C9 <b>Trailing Stop:</b> {trailing_line}",
    ]
    if trailing_detail_line:
        lines.append(trailing_detail_line)
    lines.extend([
        f"\U0001F4CA <b>Daily Limit:</b> {policy.daily_trade_count or 0}/{policy.daily_trade_limit} trades used today",
        f"\U0001F4E6 <b>Open Positions:</b> {len(live_positions)}/{policy.max_open_positions}",
        "",
        "\u2501" * 21,
    ])

    if live_positions:
        lines.append("<b>Open Positions</b>")
        for view in live_positions[:10]:
            position = view["position"]
            roi = _fmt_pct(view["roi_pct"], signed=True)
            stale = " (price stale)" if view["price_stale"] else ""
            lines.append(
                f"\u2022 {position.symbol or position.contract[:6]}: "
                f"{view['current_value_sol']:.4f} ETH  ({roi}){stale}"
            )
        if len(live_positions) > 10:
            lines.append(f"\u2026and {len(live_positions) - 10} more.")
    else:
        lines.append("No open Auto-Trade positions right now.")

    lines.append("")
    lines.append(
        f"\U0001F4D2 <b>Realized P&amp;L:</b> {summary.realized_pnl_sol:+.4f} ETH "
        f"({_fmt_pct(summary.realized_pnl_pct)}) across {summary.closed_trade_count} closed trades"
    )
    if summary.closed_trade_count:
        lines.append(f"\U0001F3C6 <b>Win Rate:</b> {_fmt_pct(summary.win_rate_pct)} ({summary.win_count}W / {summary.loss_count}L)")

    lines.append("")
    lines.append(
        "Use the buttons below to manage Auto-Trade, or /wallet to manage your wallet."
    )

    return "\n".join(lines)


async def _render_wallet(user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    text = await _build_wallet_text(user_id)
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return text, real_wallet_onboarding_kb()
    policy = await policy_service.get_or_create_policy(user_id)
    return text, auto_trade_wallet_menu_kb(policy)


async def _refresh_message(message: Message, user_id: int) -> None:
    text, kb = await _render_wallet(user_id)
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        pass  # message content/markup unchanged -- not an error


async def _send_fresh(message: Message, user_id: int) -> None:
    text, kb = await _render_wallet(user_id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("wallet"))
async def cmd_wallet(message: Message) -> None:
    """Sec 36 -- new /wallet command: balance, open positions, Auto-Trade
    status, buy amount, and TP/SL, all in one place, with inline buttons
    to manage every setting directly."""
    user_id = message.from_user.id
    try:
        text, kb = await _render_wallet(user_id)
    except Exception as e:
        logger.error("[AutoTradeWallet] failed to build /wallet view for user=%s: %s", user_id, e)
        text, kb = "\u26A0\uFE0F Couldn't load your Auto-Trade wallet right now. Please try again in a moment.", None
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "atw:refresh")
async def cb_refresh(callback: CallbackQuery) -> None:
    try:
        await _refresh_message(callback.message, callback.from_user.id)
    except Exception as e:
        logger.error("[AutoTradeWallet] refresh failed for user=%s: %s", callback.from_user.id, e)
        await callback.answer("\u26A0\uFE0F Couldn't refresh right now.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "atw:toggle")
async def cb_toggle(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    wallet = await get_real_wallet(user_id)
    if not wallet:
        await callback.answer("Set up /wallet first, then come back to turn Auto-Trade on.", show_alert=True)
        return
    policy = await policy_service.get_or_create_policy(user_id)
    new_value = not policy.auto_trade_enabled
    if new_value and policy.kill_switch:
        await callback.answer("Release the kill switch first, then turn Auto-Trade on.", show_alert=True)
        return
    await policy_service.update_policy_field(user_id, "auto_trade_enabled", new_value)
    await _refresh_message(callback.message, user_id)
    await callback.answer("\u2705 Auto-Trade turned ON." if new_value else "Auto-Trade turned OFF.")


@router.callback_query(F.data == "atw:kill_toggle")
async def cb_kill_toggle(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    policy = await policy_service.get_or_create_policy(user_id)
    new_value = not policy.kill_switch
    await policy_service.update_policy_field(user_id, "kill_switch", new_value)
    await _refresh_message(callback.message, user_id)
    await callback.answer(
        "\U0001F6D1 Kill switch engaged -- Auto-Trade won't open or manage any trades until you release it."
        if new_value else "Kill switch released."
    )


@router.callback_query(F.data == "atw:trailing_toggle")
async def cb_trailing_toggle(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    policy = await policy_service.get_or_create_policy(user_id)
    new_value = not policy.trailing_stop_enabled
    if new_value and not (policy.trailing_stop_pct or policy.trailing_retracement_pct):
        await callback.answer("Set a Trailing % or Trailing Retracement % first, then turn this on.", show_alert=True)
        return
    await policy_service.update_policy_field(user_id, "trailing_stop_enabled", new_value)
    await _refresh_message(callback.message, user_id)
    await callback.answer("Trailing stop turned ON." if new_value else "Trailing stop turned OFF.")


@router.callback_query(F.data.startswith("atw:edit:"))
async def cb_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 2)[-1]
    label = _EDIT_FIELD_LABELS.get(field)
    if not label:
        await callback.answer("Unknown setting.", show_alert=True)
        return
    await state.set_state(AutoTradeWalletStates.waiting_field_value)
    await state.update_data(field=field)
    if field in _EDIT_FIELDS_CLEARABLE:
        prompt = f"\u270F\uFE0F Send the {label}.\n\nSend <code>clear</code> to remove this, or /cancel to back out."
    else:
        prompt = f"\u270F\uFE0F Send the {label}.\n\nSend /cancel to back out."
    await callback.message.answer(prompt, parse_mode="HTML")
    await callback.answer()


@router.message(AutoTradeWalletStates.waiting_field_value)
async def on_field_value_message(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    data = await state.get_data()
    field = data.get("field")
    await state.clear()
    if not field:
        await message.answer("\u274C Lost track of which setting this was for \u2014 tap the button again.")
        return

    user_id = message.from_user.id

    if raw.lower() == "clear" and field in _EDIT_FIELDS_CLEARABLE:
        await policy_service.update_policy_field(user_id, field, None)
        await message.answer("\u2705 Cleared.")
        await _send_fresh(message, user_id)
        return

    try:
        if field in _EDIT_FIELDS_INT:
            value = int(raw)
            if field == "daily_trade_limit" and not 1 <= value <= 50:
                raise ValueError
            if field == "max_open_positions" and value < 1:
                raise ValueError
        else:
            value = float(raw)
            if field == "buy_amount_eth" and value <= 0:
                raise ValueError
            if field in ("take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "trailing_activation_pct", "trailing_retracement_pct") and value <= 0:
                raise ValueError
    except ValueError:
        if field in _EDIT_FIELDS_INT:
            await message.answer("\u274C Enter a whole number in range, or /cancel.")
        else:
            await message.answer("\u274C Enter a positive number, or /cancel.")
        return

    await policy_service.update_policy_field(user_id, field, value)
    await message.answer("\u2705 Setting updated.")
    await _send_fresh(message, user_id)
