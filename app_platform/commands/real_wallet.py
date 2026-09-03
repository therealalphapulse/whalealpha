import asyncio
import html
import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from domain.admin.user_service import get_or_create_user
from providers.marketdata.dexscreener import get_token_card_info
from domain.trading.real.solana_wallet import (
    get_real_wallet,
    create_wallet,
    import_wallet,
    export_wallet_secret,
    disconnect_wallet,
    get_wallet_settings,
    set_wallet_slippage,
    set_wallet_priority_tier,
    get_automation_status,
    set_auto_trading,
    set_auto_kill_switch,
    set_auto_daily_cap,
    WalletImportError,
)
from domain.trading.real.jupiter_swap import get_sol_balance, get_mint_decimals, WRAPPED_SOL_MINT
from domain.trading.real import real_trade_engine
from domain.trading.real import wallet_withdraw
from domain.trading.real import real_dca_engine
from domain.trading.real import real_automation_engine
from domain.trading.real import real_exit_engine
from domain.trading.real import real_limit_order_engine
from domain.intelligence.wallet_portfolio import (
    fetch_wallet_fungible_tokens,
    build_wallet_portfolio_report,
    get_wallet_portfolio_value,
    format_usd,
)
from app_platform.keyboards.real_wallet import (
    real_wallet_onboarding_kb,
    real_wallet_created_kb,
    real_wallet_menu_kb,
    real_wallet_disconnect_confirm_kb,
    real_wallet_export_warning_kb,
    real_wallet_buy_presets_kb,
    real_wallet_positions_list_kb,
    real_wallet_settings_kb,
    real_trade_position_kb,
    real_wallet_back_kb,
    real_wallet_withdraw_asset_kb,
    real_wallet_withdraw_amount_kb,
    real_wallet_withdraw_confirm_kb,
    real_wallet_automation_kb,
    real_wallet_automation_filters_kb,
    real_wallet_dca_list_kb,
    real_wallet_dca_detail_kb,
    real_wallet_dca_cancel_confirm_kb,
    real_wallet_dca_skip_optional_kb,
    real_wallet_exit_menu_kb,
    real_wallet_limit_list_kb,
    real_wallet_limit_detail_kb,
    real_wallet_limit_direction_kb,
    BUY_PRESETS_SOL,
)

logger = logging.getLogger("AlphaPulse.RealWalletCmd")
router = Router()

# Every user gets the full DCA engine (see on_dca_total_orders_message()
# below): up to real_dca_engine.MAX_TOTAL_ORDERS, with the optional price
# floor/ceiling guard-rail steps. No Premium cap.


class RealWalletStates(StatesGroup):
    waiting_import_key = State()
    waiting_buy_contract = State()
    waiting_custom_buy_amount = State()
    waiting_custom_sell_pct = State()
    waiting_withdraw_address = State()
    waiting_withdraw_custom_amount = State()
    waiting_auto_filter_value = State()
    waiting_dca_contract = State()
    waiting_dca_amount = State()
    waiting_dca_interval = State()
    waiting_dca_total_orders = State()
    waiting_dca_price_floor = State()
    waiting_dca_price_ceiling = State()
    waiting_exit_trigger_pct = State()
    waiting_exit_sell_fraction = State()
    waiting_limit_contract = State()
    waiting_limit_price = State()
    waiting_limit_amount = State()


ONBOARDING_TEXT = (
    "🔐 <b>Real Wallet</b>\n\n"
    "Trade with real funds directly from Telegram — same experience as "
    "Paper Trade, but every buy/sell is a real Solana transaction.\n\n"
    "Choose how you'd like to set up your wallet:"
)

INFO_TEXT = (
    "ℹ️ <b>How Real Wallet works</b>\n\n"
    "• Your private key is <b>encrypted</b> before it's ever stored — "
    "AlphaPulse never keeps it in plain text.\n"
    "• The key is only decrypted for a split second, in memory, to sign "
    "a trade you initiated — then discarded.\n"
    "• You can export your key or disconnect your wallet at any time from "
    "the Real Wallet menu.\n"
    "• This is real money — trade sizes you're comfortable with, "
    "especially while you're getting used to it. Solana transactions "
    "are irreversible once confirmed.\n"
    "• Automation and DCA both spend unattended once turned on — set a "
    "daily spend cap you're comfortable with, and use the kill switch "
    "any time to stop everything instantly."
)


def _menu_text(
    public_key: str,
    sol_balance: float | None,
    portfolio_value_usd: float | None,
    auto_enabled: bool,
) -> str:
    bal_line = f"{sol_balance:.4f} SOL" if sol_balance is not None else "—"
    value_line = format_usd(portfolio_value_usd) if portfolio_value_usd is not None else "—"
    auto_line = "🟢 ON" if auto_enabled else "⚪ OFF (manual trading only)"
    return (
        "💼 <b>AlphaPulse Real Wallet</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👛 <b>Address</b> <i>(tap to copy)</i>:\n<code>{public_key}</code>\n\n"
        f"💰 <b>SOL Balance:</b> {bal_line}\n"
        f"📈 <b>Portfolio Value:</b> {value_line}\n"
        f"🤖 <b>Automation:</b> {auto_line}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Manage your wallet below."
    )


async def _fetch_balance_safe(public_key: str) -> float | None:
    try:
        return await get_sol_balance(public_key)
    except Exception as e:
        # Root-cause investigation (see chat): this used to be a bare
        # `except Exception: return None` with zero logging — any SOL
        # balance failure (bad RPC response, Helius format change,
        # timeout) was completely invisible in Railway logs. Return
        # value/behavior is unchanged (still None -> "—" displayed);
        # only the diagnostic trail is added.
        logger.error(f"SOL balance fetch failed for {public_key}: {e}")
        return None


async def _fetch_portfolio_value_safe(public_key: str) -> float | None:
    result = await get_wallet_portfolio_value(public_key)
    return result["total_value_usd"] if result else None


def _menu_text_loading(public_key: str, auto_enabled: bool) -> str:
    # Placeholder shown the instant the menu opens, before the SOL
    # balance + portfolio value RPC/API calls resolve. Keeps the handler
    # from ever blocking Telegram on network I/O (see performance
    # requirements: respond immediately, then update in place).
    auto_line = "🟢 ON" if auto_enabled else "⚪ OFF (manual trading only)"
    return (
        "💼 <b>AlphaPulse Real Wallet</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👛 <b>Address</b> <i>(tap to copy)</i>:\n<code>{public_key}</code>\n\n"
        "💰 <b>SOL Balance:</b> ⏳ Loading...\n"
        "📈 <b>Portfolio Value:</b> ⏳ Loading...\n"
        f"🤖 <b>Automation:</b> {auto_line}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Manage your wallet below."
    )


async def _send_or_edit(target, text: str, kb, edit: bool):
    """Sends a new message (edit=False) or edits target in place
    (edit=True). Returns a Message object callers can edit again later
    once the slow data has loaded."""
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
        return target
    return await target.answer(text, reply_markup=kb)


async def _show_menu(target, user_id: int, edit: bool):
    # get_real_wallet() is a DB read only — safe to await inline. The
    # SOL balance + portfolio value calls are the slow part (live
    # RPC/Helius), so those must never delay the first response the
    # user sees.
    wallet = await get_real_wallet(user_id)
    logger.info(f"[TEMP-DEBUG] _show_menu: user={user_id} wallet_found={bool(wallet)}")
    if not wallet:
        await _send_or_edit(target, ONBOARDING_TEXT, real_wallet_onboarding_kb(), edit)
        return

    kb = real_wallet_menu_kb(wallet.auto_trading_enabled)
    loading_text = _menu_text_loading(wallet.public_key, wallet.auto_trading_enabled)

    # Phase 1: respond immediately with a loading placeholder — no RPC
    # calls made yet, so this is effectively instant.
    message_to_update = await _send_or_edit(target, loading_text, kb, edit)
    logger.info(f"[TEMP-DEBUG] _show_menu: loading placeholder sent for {wallet.public_key}")

    # Phase 2: fetch the slow data concurrently in the background.
    balance, portfolio_value = await asyncio.gather(
        _fetch_balance_safe(wallet.public_key),
        _fetch_portfolio_value_safe(wallet.public_key),
    )
    logger.info(
        f"[TEMP-DEBUG] _show_menu: fetch results for {wallet.public_key} -> "
        f"balance={balance!r} portfolio_value={portfolio_value!r}"
    )
    final_text = _menu_text(wallet.public_key, balance, portfolio_value, wallet.auto_trading_enabled)

    # Phase 3: update the message in place once data is ready.
    try:
        await message_to_update.edit_text(final_text, reply_markup=kb)
        logger.info(f"[TEMP-DEBUG] _show_menu: edit_text succeeded for {wallet.public_key}")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"real wallet menu update failed: {e}")


@router.message(Command("realwallet", "rw"))
async def cmd_real_wallet(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    await _show_menu(message, message.from_user.id, edit=False)


@router.callback_query(F.data == "rw:menu")
async def cb_menu(callback: CallbackQuery):
    # Ack Telegram immediately — _show_menu() does a live SOL balance +
    # portfolio value fetch (Helius-backed), which can take long enough
    # under load that answering afterward risks "query is too old"
    # (Telegram callback queries expire; see aiogram TelegramBadRequest).
    await callback.answer()
    await _show_menu(callback.message, callback.from_user.id, edit=True)


@router.callback_query(F.data == "rw:info")
async def cb_info(callback: CallbackQuery):
    await callback.message.edit_text(INFO_TEXT, reply_markup=real_wallet_onboarding_kb())
    await callback.answer()


@router.callback_query(F.data == "rw:create")
async def cb_create(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        wallet = await create_wallet(user_id)
    except WalletImportError as e:
        await callback.answer(str(e), show_alert=True)
        return

    try:
        secret = await export_wallet_secret(user_id)
    except Exception:
        secret = None

    text = (
        "✅ <b>Wallet created</b>\n\n"
        f"<b>Address:</b>\n<code>{wallet.public_key}</code>\n\n"
    )
    if secret:
        text += (
            "⚠️ <b>Save this private key somewhere safe right now.</b> "
            "This is the ONLY time it's shown automatically. Anyone with "
            "this key has full control of the wallet's funds.\n\n"
            f"<code>{secret}</code>\n\n"
            "This message will still be visible in your chat history — "
            "consider deleting it once you've saved the key elsewhere."
        )
    await callback.message.edit_text(text, reply_markup=real_wallet_created_kb())
    await callback.answer()


@router.callback_query(F.data == "rw:ack_saved")
async def cb_ack_saved(callback: CallbackQuery):
    await callback.answer()
    await _show_menu(callback.message, callback.from_user.id, edit=True)


@router.callback_query(F.data == "rw:import")
async def cb_import_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RealWalletStates.waiting_import_key)
    await callback.message.edit_text(
        "📥 <b>Import Wallet</b>\n\n"
        "Send your private key now, as its own message — either the "
        "base58 string most wallet apps export, or a [1,2,3,...] key array.\n\n"
        "⚠️ Only paste it here in this chat with AlphaPulse. I'll delete "
        "your message immediately after reading it.\n\n"
        "Send /cancel to back out."
    )
    await callback.answer()


@router.message(RealWalletStates.waiting_import_key)
async def on_import_key_message(message: Message, state: FSMContext):
    raw = message.text or ""

    # Get rid of the plaintext key in the chat as fast as possible.
    try:
        await message.delete()
    except Exception:
        pass

    if raw.strip().lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    try:
        wallet = await import_wallet(message.from_user.id, raw)
    except WalletImportError as e:
        await message.answer(f"❌ {html.escape(str(e))}\n\nTry again, or send /cancel.")
        return
    except Exception:
        logger.exception("Unexpected error importing wallet")
        await message.answer("❌ Something went wrong importing that key. Please try again.")
        return

    await state.clear()
    await message.answer(
        "✅ <b>Wallet imported</b>\n\n"
        f"<b>Address:</b>\n<code>{wallet.public_key}</code>",
        reply_markup=real_wallet_menu_kb(wallet.auto_trading_enabled),
    )


@router.callback_query(F.data == "rw:balance")
async def cb_balance(callback: CallbackQuery):
    await callback.answer("Refreshed")
    await _show_menu(callback.message, callback.from_user.id, edit=True)


async def _show_automation_panel(target, user_id: int, edit: bool):
    status = await get_automation_status(user_id)
    if status is None:
        msg = "No active wallet. Set one up first."
        if edit:
            await target.answer(msg, show_alert=True)
        return

    text = (
        "🤖 <b>Real Trade Automation</b>\n\n"
        "When ON, AlphaPulse scans fresh signals against your filters "
        "below and buys automatically — no per-trade confirmation. Both "
        "Automation and DCA share the same daily spend cap and kill "
        "switch, since both spend unattended.\n\n"
        f"<b>Status:</b> {'🟢 ON' if status['auto_trading_enabled'] else '⚪ OFF'}\n"
        f"<b>Kill switch:</b> {'🛑 ENGAGED' if status['kill_switch'] else '✅ off'}\n"
        f"<b>Daily cap:</b> {status['daily_cap_sol']:.2f} SOL\n"
        f"<b>Spent today:</b> {status['spent_today_sol']:.4f} SOL "
        f"({status['remaining_today_sol']:.4f} SOL remaining)"
    )
    kb = real_wallet_automation_kb(status["auto_trading_enabled"], status["kill_switch"], status["daily_cap_sol"])
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "rw:automation")
async def cb_automation_panel(callback: CallbackQuery):
    await _show_automation_panel(callback.message, callback.from_user.id, edit=True)
    await callback.answer()


@router.callback_query(F.data == "rw:auto_toggle")
async def cb_auto_toggle(callback: CallbackQuery):
    status = await get_automation_status(callback.from_user.id)
    if status is None:
        await callback.answer("No active wallet.", show_alert=True)
        return
    new_state = not status["auto_trading_enabled"]

    # Auto Trade is available to every user (no Premium gate).
    await set_auto_trading(callback.from_user.id, new_state)
    await _show_automation_panel(callback.message, callback.from_user.id, edit=True)
    await callback.answer(f"Automation turned {'ON' if new_state else 'OFF'}")


@router.callback_query(F.data == "rw:auto_kill_toggle")
async def cb_auto_kill_toggle(callback: CallbackQuery):
    status = await get_automation_status(callback.from_user.id)
    if status is None:
        await callback.answer("No active wallet.", show_alert=True)
        return
    new_state = not status["kill_switch"]
    await set_auto_kill_switch(callback.from_user.id, new_state)
    await _show_automation_panel(callback.message, callback.from_user.id, edit=True)
    await callback.answer("🛑 Kill switch ENGAGED — automation and DCA halted." if new_state else "Kill switch released.")


@router.callback_query(F.data.startswith("rw:auto_set_cap:"))
async def cb_auto_set_cap(callback: CallbackQuery):
    cap = float(callback.data.split(":")[-1])
    ok = await set_auto_daily_cap(callback.from_user.id, cap)
    if not ok:
        await callback.answer("No active wallet.", show_alert=True)
        return
    await _show_automation_panel(callback.message, callback.from_user.id, edit=True)
    await callback.answer(f"Daily cap set to {cap} SOL")


_SIGNAL_SOURCE_LABELS = {
    "new": "🆕 New only",
    "redelivered": "🔁 Redelivered only",
    "first_milestone": "⚡ First Milestone only",
    "both": "🔀 New + Redelivered",
    "new_redelivered": "🔀 New + Redelivered",
    "new_first_milestone": "🔀 New + First Milestone",
    "redelivered_first_milestone": "🔀 Redelivered + First Milestone",
    "new_redelivered_first_milestone": "🔀 New + Redelivered + First Milestone",
}


async def _show_auto_filters_panel(target, user_id: int, edit: bool):
    filt = await real_automation_engine.get_or_create_filter(user_id)
    text = (
        "🎯 <b>Auto-Buy Filters</b>\n\n"
        "Every field left unset means \"no constraint\" — with none set, "
        "automation only requires basic auto-buy eligibility.\n\n"
        f"💵 Auto-buy amount (USDT): <b>{filt.auto_buy_amount_usdt if filt.auto_buy_amount_usdt is not None else '—'}</b>\n"
        f"🎯 Take Profit %: <b>{filt.take_profit_pct if filt.take_profit_pct is not None else '—'}</b>\n"
        f"🛑 Stop Loss %: <b>{filt.stop_loss_pct if filt.stop_loss_pct is not None else '—'}</b>\n"
        f"🔢 Daily auto-buy limit (1–20): <b>{filt.daily_auto_buy_limit if filt.daily_auto_buy_limit is not None else '—'}</b>\n"
        f"💰 SOL per trade: <b>{filt.sol_per_trade if filt.sol_per_trade is not None else '—'}</b>\n"
        f"📊 Min conviction score: <b>{filt.min_score if filt.min_score is not None else '—'}</b>\n"
        f"🏦 Market cap range: <b>{filt.min_market_cap if filt.min_market_cap is not None else '—'} to {filt.max_market_cap if filt.max_market_cap is not None else '—'}</b>\n"
        f"💧 Min liquidity (USD): <b>{filt.min_liquidity_usd if filt.min_liquidity_usd is not None else '—'}</b>\n"
        f"📦 Max bundle %: <b>{filt.max_bundle_pct if filt.max_bundle_pct is not None else '—'}</b>\n"
        f"👤 Max dev holding %: <b>{filt.max_dev_holding_pct if filt.max_dev_holding_pct is not None else '—'}</b>\n"
        f"🆕 Auto-buy on: <b>{_SIGNAL_SOURCE_LABELS.get(filt.auto_buy_signal_source or 'both', _SIGNAL_SOURCE_LABELS['both'])}</b>\n\n"
        "Tap a field to edit it."
    )
    kb = real_wallet_automation_filters_kb(filt.auto_buy_signal_source or "both")
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "rw:auto_filters")
async def cb_auto_filters(callback: CallbackQuery):
    await _show_auto_filters_panel(callback.message, callback.from_user.id, edit=True)
    await callback.answer()


_FILTER_FIELD_LABELS = {
    "auto_buy_amount_usdt": "auto-buy amount in USDT, e.g. 25",
    "take_profit_pct": "Take Profit percentage from entry, e.g. 100 for +100%",
    "stop_loss_pct": "Stop Loss percentage from entry, e.g. 20 for -20%",
    "daily_auto_buy_limit": "daily auto-buy limit, a whole number from 1 to 20",
    "sol_per_trade": "SOL per trade (e.g. 0.1)",
    "min_score": "minimum conviction score, 0-100",
    "min_market_cap": "minimum market cap in USD",
    "max_market_cap": "maximum market cap in USD",
    "min_liquidity_usd": "minimum liquidity in USD",
    "max_bundle_pct": "maximum bundle %, 0-100",
    "max_dev_holding_pct": "maximum dev holding %, 0-100",
}

# Fields that must always hold a value and can't be cleared to "no constraint".
_FILTER_FIELDS_NOT_CLEARABLE = {"sol_per_trade", "daily_auto_buy_limit"}

# Fields validated and stored as whole numbers rather than floats.
_FILTER_FIELDS_INT = {"daily_auto_buy_limit"}


@router.callback_query(F.data.startswith("rw:auto_filter_edit:"))
async def cb_auto_filter_edit(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split(":")[-1]
    label = _FILTER_FIELD_LABELS.get(field)
    if not label:
        await callback.answer("Unknown filter field.", show_alert=True)
        return
    await state.set_state(RealWalletStates.waiting_auto_filter_value)
    await state.update_data(filter_field=field)
    if field in _FILTER_FIELDS_NOT_CLEARABLE:
        prompt = f"✏️ Send the {label}.\n\nSend /cancel to back out."
    else:
        prompt = f"✏️ Send the {label}.\n\nSend <code>clear</code> to remove this constraint, or /cancel to back out."
    await callback.message.edit_text(prompt)
    await callback.answer()


@router.message(RealWalletStates.waiting_auto_filter_value)
async def on_auto_filter_value_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    data = await state.get_data()
    field = data.get("filter_field")
    await state.clear()
    if not field:
        await message.answer("❌ Lost track of which filter this was for — tap Auto-Buy Filters again.")
        return

    if raw.lower() == "clear" and field not in _FILTER_FIELDS_NOT_CLEARABLE:
        ok = await real_automation_engine.update_filter(message.from_user.id, field, None)
        if not ok:
            await message.answer("❌ Could not clear that setting. Please try again.")
            return
        await message.answer("✅ Constraint cleared.")
        await _show_auto_filters_panel(message, message.from_user.id, edit=False)
        return

    try:
        if field in _FILTER_FIELDS_INT:
            value = int(raw)
            if field == "daily_auto_buy_limit" and not 1 <= value <= 20:
                raise ValueError
        else:
            value = float(raw)
            if field == "sol_per_trade" and value <= 0:
                raise ValueError
    except ValueError:
        if field in _FILTER_FIELDS_INT:
            await message.answer("❌ Enter a whole number from 1 to 20, or /cancel.")
        else:
            await message.answer("❌ Enter a number, or /cancel.")
        return

    ok = await real_automation_engine.update_filter(message.from_user.id, field, value)
    if not ok:
        await message.answer("❌ Could not save that setting — check the allowed range, or /cancel.")
        return
    await message.answer("✅ Filter updated.")
    await _show_auto_filters_panel(message, message.from_user.id, edit=False)


@router.callback_query(F.data == "rw:auto_filters_clear")
async def cb_auto_filters_clear(callback: CallbackQuery):
    for field in ("min_score", "min_market_cap", "max_market_cap", "min_liquidity_usd", "max_bundle_pct", "max_dev_holding_pct"):
        await real_automation_engine.update_filter(callback.from_user.id, field, None)
    await callback.answer("All filter constraints cleared.")
    await cb_auto_filters(callback)


@router.callback_query(F.data.startswith("rw:auto_set_signal_source:"))
async def cb_auto_set_signal_source(callback: CallbackQuery):
    value = callback.data.split(":")[-1]
    if value not in real_automation_engine.ALL_SIGNAL_SOURCE_VALUES:
        await callback.answer("Unknown option.", show_alert=True)
        return
    ok = await real_automation_engine.update_filter(callback.from_user.id, "auto_buy_signal_source", value)
    if not ok:
        await callback.answer("❌ Could not update that setting.", show_alert=True)
        return
    await _show_auto_filters_panel(callback.message, callback.from_user.id, edit=True)
    await callback.answer(f"Auto-buy source set to: {_SIGNAL_SOURCE_LABELS.get(value, value)}")


@router.callback_query(F.data == "rw:history")
async def cb_history(callback: CallbackQuery):
    trades = await real_trade_engine.get_real_trade_history(callback.from_user.id)
    if not trades:
        await callback.answer("No closed trades yet.", show_alert=True)
        return

    lines = ["📜 <b>Recent Real Trades</b>\n"]
    for t in trades[:10]:
        pnl_sign = "+" if (t.realized_pnl_sol or 0) >= 0 else ""
        lines.append(
            f"• <b>{html.escape(t.symbol or '???')}</b> — "
            f"{pnl_sign}{t.realized_pnl_sol:.4f} SOL "
            f"({t.status})"
        )

    wallet = await get_real_wallet(callback.from_user.id)
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=real_wallet_menu_kb(wallet.auto_trading_enabled if wallet else False),
    )
    await callback.answer()


@router.callback_query(F.data == "rw:export")
async def cb_export_warn(callback: CallbackQuery):
    await callback.message.edit_text(
        "⚠️ <b>Export Private Key</b>\n\n"
        "Anyone who sees this key can take everything in this wallet. "
        "Only continue if you're somewhere private, and delete the "
        "message afterward.",
        reply_markup=real_wallet_export_warning_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "rw:export_confirm")
async def cb_export_confirm(callback: CallbackQuery):
    try:
        secret = await export_wallet_secret(callback.from_user.id)
    except WalletImportError as e:
        await callback.answer(str(e), show_alert=True)
        return

    await callback.message.answer(
        f"🔑 <code>{secret}</code>\n\n"
        "Delete this message once you've saved it somewhere secure."
    )
    await callback.answer()


@router.callback_query(F.data == "rw:disconnect_confirm")
async def cb_disconnect_confirm(callback: CallbackQuery):
    await callback.message.edit_text(
        "🔌 <b>Disconnect Wallet?</b>\n\n"
        "This permanently deletes AlphaPulse's copy of your encrypted key. "
        "If you haven't exported/backed up your key elsewhere first, "
        "you could lose access to these funds. This can't be undone.",
        reply_markup=real_wallet_disconnect_confirm_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "rw:disconnect")
async def cb_disconnect(callback: CallbackQuery):
    await disconnect_wallet(callback.from_user.id)
    await callback.message.edit_text(
        ONBOARDING_TEXT, reply_markup=real_wallet_onboarding_kb()
    )
    await callback.answer("Wallet disconnected.")


# ---------------------------------------------------------------------------
# Trade settings (slippage / priority fee)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "rw:settings")
async def cb_settings(callback: CallbackQuery):
    wallet = await get_real_wallet(callback.from_user.id)
    if not wallet:
        await callback.answer("No active wallet. Set one up first.", show_alert=True)
        return

    settings = await get_wallet_settings(callback.from_user.id)
    await callback.message.edit_text(
        "⚙️ <b>Trade Settings</b>\n\n"
        "Applied to every manual buy/sell from this wallet.\n\n"
        "<b>Slippage:</b> how much price movement to tolerate before a "
        "swap fails rather than fill at a worse price.\n"
        "<b>Priority Fee:</b> how much extra you pay validators for "
        "faster inclusion — higher tiers land faster in busy conditions.",
        reply_markup=real_wallet_settings_kb(settings["slippage_bps"], settings["priority_fee_tier"]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rw:set_slippage:"))
async def cb_set_slippage(callback: CallbackQuery):
    bps = int(callback.data.split(":")[-1])
    ok = await set_wallet_slippage(callback.from_user.id, bps)
    if not ok:
        await callback.answer("No active wallet.", show_alert=True)
        return
    settings = await get_wallet_settings(callback.from_user.id)
    await callback.message.edit_reply_markup(
        reply_markup=real_wallet_settings_kb(settings["slippage_bps"], settings["priority_fee_tier"])
    )
    await callback.answer(f"Slippage set to {bps / 100:.1f}%")


@router.callback_query(F.data.startswith("rw:set_priority:"))
async def cb_set_priority(callback: CallbackQuery):
    tier = callback.data.split(":")[-1]
    ok = await set_wallet_priority_tier(callback.from_user.id, tier)
    if not ok:
        await callback.answer("No active wallet.", show_alert=True)
        return
    settings = await get_wallet_settings(callback.from_user.id)
    await callback.message.edit_reply_markup(
        reply_markup=real_wallet_settings_kb(settings["slippage_bps"], settings["priority_fee_tier"])
    )
    await callback.answer(f"Priority fee set to {tier}")


# ---------------------------------------------------------------------------
# Manual real buy — preset amounts (Trojan-style quick buy) + custom
# ---------------------------------------------------------------------------

def _confirmation_note(confirmation: str) -> str:
    if confirmation == "timeout":
        return (
            "\n\n⏳ <i>Broadcast succeeded but AlphaPulse couldn't confirm "
            "on-chain landing in time — check the tx on Solscan; it may "
            "still land or may have expired.</i>"
        )
    return ""


async def _post_trade_sync_line(user_id: int) -> str:
    """A fresh SOL balance line appended right onto the trade
    confirmation, so balances/holdings visibly update the instant a
    trade lands instead of requiring a separate manual Refresh tap."""
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return ""
    balance = await _fetch_balance_safe(wallet.public_key)
    if balance is None:
        return ""
    return f"\n\n🔄 <b>Wallet balance:</b> {balance:.4f} SOL"


async def _execute_and_report_buy(target_message, user_id: int, contract: str, sol_amount: float, edit: bool):
    wallet = await get_real_wallet(user_id)
    if not wallet:
        text = "⚠️ You need a Real Wallet first. Use /realwallet to set one up."
        if edit:
            await target_message.edit_text(text, reply_markup=real_wallet_onboarding_kb())
        else:
            await target_message.answer(text, reply_markup=real_wallet_onboarding_kb())
        return

    info = await get_token_card_info(contract)
    if not info:
        text = "❌ Couldn't fetch fresh token data. Try again in a moment."
        (await target_message.edit_text(text)) if edit else (await target_message.answer(text))
        return

    try:
        price = float(info["price"])
    except (TypeError, ValueError):
        text = "❌ Couldn't read a valid price for that token right now."
        (await target_message.edit_text(text)) if edit else (await target_message.answer(text))
        return

    settings = await get_wallet_settings(user_id)
    status_text = f"⏳ Submitting buy: {sol_amount} SOL → {html.escape(info['symbol'])}..."
    status_msg = (
        await target_message.edit_text(status_text) if edit
        else await target_message.answer(status_text)
    )

    result = await real_trade_engine.execute_real_buy(
        user_id=user_id,
        contract=contract,
        name=info["name"],
        symbol=info["symbol"],
        current_price=price,
        sol_amount=sol_amount,
        slippage_bps=settings["slippage_bps"],
        priority_fee_tier=settings["priority_fee_tier"],
    )

    if not result["ok"]:
        await status_msg.edit_text(f"❌ Buy failed: {html.escape(result['reason'])}")
        return

    trade = result["trade"]
    confirmed_line = "✅" if result["confirmation"] == "confirmed" else "📡"
    sync_line = await _post_trade_sync_line(user_id)
    await status_msg.edit_text(
        f"{confirmed_line} <b>Bought {html.escape(trade.symbol)}</b>\n\n"
        f"Spent: {trade.sol_spent:.4f} SOL\n"
        f"Received: {trade.token_quantity:,.2f} {html.escape(trade.symbol)}\n"
        f"Tx: <code>{result['signature']}</code>"
        f"{_confirmation_note(result['confirmation'])}"
        f"{sync_line}",
        reply_markup=real_trade_position_kb(trade.id),
    )


async def _show_buy_presets(target_message, user_id: int, contract: str):
    """Shared by the token-action entry point (rwbuy:menu:<contract>) and
    the Real Wallet dashboard's Buy button (which collects the contract
    first, then reuses this same preset-buy flow)."""
    wallet = await get_real_wallet(user_id)
    if not wallet:
        await target_message.answer(
            "⚠️ You need a Real Wallet first to buy in-app.\n\n"
            "Use /realwallet to create or import one — takes a minute.",
            reply_markup=real_wallet_onboarding_kb(),
        )
        return

    info = await get_token_card_info(contract)
    symbol = html.escape(info["symbol"]) if info and info.get("symbol") else "this token"
    preset_line = " / ".join(f"{a} SOL" for a in BUY_PRESETS_SOL)
    await target_message.answer(
        f"⚡ <b>Buy {symbol}</b>\n\nPick an amount ({preset_line}) or enter a custom one:",
        reply_markup=real_wallet_buy_presets_kb(contract),
    )


@router.callback_query(F.data.startswith("rwbuy:menu:"))
async def cb_rwbuy_menu(callback: CallbackQuery):
    contract = callback.data.split(":", 2)[2]
    await callback.answer()
    await _show_buy_presets(callback.message, callback.from_user.id, contract)


@router.callback_query(F.data == "rw:buy_start")
async def cb_rw_buy_start(callback: CallbackQuery, state: FSMContext):
    """Dashboard Buy button — unlike the token-action entry point, the
    dashboard has no contract in context yet, so ask for one first and
    then hand off to the same preset-buy flow used everywhere else."""
    wallet = await get_real_wallet(callback.from_user.id)
    if not wallet:
        await callback.answer("No active wallet. Set one up first.", show_alert=True)
        return

    await state.set_state(RealWalletStates.waiting_buy_contract)
    await callback.message.answer(
        "🛒 <b>Buy</b>\n\n"
        "Send the token's contract address (mint) to buy.\n\n"
        "Send /cancel to back out."
    )
    await callback.answer()


@router.message(RealWalletStates.waiting_buy_contract)
async def on_buy_contract_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    info = await get_token_card_info(raw)
    if not info:
        await message.answer(
            "❌ Couldn't find that token. Double-check the contract address, or /cancel."
        )
        return

    await state.clear()
    await _show_buy_presets(message, message.from_user.id, raw)


@router.callback_query(F.data.startswith("rwbuy:exec:"))
async def cb_rwbuy_exec(callback: CallbackQuery):
    _, _, contract, amount_str = callback.data.split(":")
    await callback.answer("Submitting buy...")
    await _execute_and_report_buy(
        callback.message, callback.from_user.id, contract, float(amount_str), edit=True
    )


@router.callback_query(F.data.startswith("rwbuy:custom:"))
async def cb_rwbuy_custom(callback: CallbackQuery, state: FSMContext):
    contract = callback.data.split(":", 2)[2]
    await state.set_state(RealWalletStates.waiting_custom_buy_amount)
    await state.update_data(contract=contract)
    await callback.message.edit_text(
        "✏️ <b>Custom Buy Amount</b>\n\nSend the amount of SOL to spend (e.g. <code>0.75</code>).\n\nSend /cancel to back out."
    )
    await callback.answer()


@router.message(RealWalletStates.waiting_custom_buy_amount)
async def on_custom_buy_amount_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    try:
        sol_amount = float(raw)
        if sol_amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a positive number of SOL, e.g. <code>0.75</code>, or /cancel.")
        return

    data = await state.get_data()
    contract = data.get("contract")
    await state.clear()

    if not contract:
        await message.answer("❌ Lost track of which token this was for — please tap Buy again.")
        return

    await _execute_and_report_buy(message, message.from_user.id, contract, sol_amount, edit=False)


# ---------------------------------------------------------------------------
# Manual real buy — /rbuy command (unchanged entry point, now settings-aware)
# ---------------------------------------------------------------------------

@router.message(Command("rbuy"))
async def cmd_real_buy(message: Message):
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "⚠️ <b>Usage:</b> <code>/rbuy &lt;contract&gt; &lt;sol_amount&gt;</code>\n\n"
            "Executes a REAL swap using your Real Wallet. Set one up first with /realwallet."
        )
        return

    contract, amount_str = parts[1].strip(), parts[2].strip()
    try:
        sol_amount = float(amount_str)
    except ValueError:
        await message.answer("❌ SOL amount must be a number.")
        return

    await _execute_and_report_buy(message, message.from_user.id, contract, sol_amount, edit=False)


# ---------------------------------------------------------------------------
# Manual real sell — presets (25/50/75/100) + custom %
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("rw:sell:"))
async def cb_real_sell(callback: CallbackQuery):
    _, _, trade_id_str, fraction_str = callback.data.split(":")
    trade_id, fraction = int(trade_id_str), float(fraction_str)
    await _execute_and_report_sell(callback, trade_id, fraction)


@router.callback_query(F.data.startswith("rw:sell_custom:"))
async def cb_sell_custom(callback: CallbackQuery, state: FSMContext):
    trade_id = int(callback.data.split(":")[-1])
    await state.set_state(RealWalletStates.waiting_custom_sell_pct)
    await state.update_data(trade_id=trade_id)
    await callback.message.answer(
        "✏️ <b>Custom Sell %</b>\n\nSend a percentage to sell, 1–100 (e.g. <code>60</code>).\n\nSend /cancel to back out."
    )
    await callback.answer()


@router.message(RealWalletStates.waiting_custom_sell_pct)
async def on_custom_sell_pct_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip().rstrip("%")
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    try:
        pct = float(raw)
        if not (0 < pct <= 100):
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a number between 1 and 100, e.g. <code>60</code>, or /cancel.")
        return

    data = await state.get_data()
    trade_id = data.get("trade_id")
    await state.clear()

    if trade_id is None:
        await message.answer("❌ Lost track of which position this was for — please tap Custom % again.")
        return

    await _execute_and_report_sell(message, trade_id, pct / 100.0, is_callback=False)


async def _execute_and_report_sell(target, trade_id: int, fraction: float, is_callback: bool = True):
    user_id = target.from_user.id
    reply_target = target.message if is_callback else target

    trades = await real_trade_engine.get_open_real_trades(user_id)
    trade = next((t for t in trades if t.id == trade_id), None)
    if not trade:
        msg = "Trade not found or already closed."
        if is_callback:
            await target.answer(msg, show_alert=True)
        else:
            await reply_target.answer(f"❌ {msg}")
        return

    # Acknowledge the tap before the price lookup — get_token_card_info is an
    # external DexScreener call and can be slow under upstream rate limits,
    # so answering first keeps the Telegram spinner from sitting through it.
    if is_callback:
        await target.answer("Submitting sell...")

    try:
        info = await get_token_card_info(trade.contract)
        price = float(info["price"]) if info and info.get("price") not in (None, "N/A") else trade.entry_price
        settings = await get_wallet_settings(user_id)

        result = await real_trade_engine.execute_real_sell(
            user_id=user_id,
            trade_id=trade_id,
            current_price=price,
            fraction=fraction,
            slippage_bps=settings["slippage_bps"],
            priority_fee_tier=settings["priority_fee_tier"],
        )

        if not result["ok"]:
            await reply_target.answer(f"❌ Sell failed: {html.escape(result['reason'])}")
            return

        confirmed_line = "✅" if result["confirmation"] == "confirmed" else "📡"
        sync_line = await _post_trade_sync_line(user_id)
        updated_trade = result["trade"]
        positions_hint = (
            "\n\nPosition fully closed." if updated_trade.status != "open"
            else f"\n\nRemaining: {updated_trade.remaining_quantity:,.2f} {html.escape(trade.symbol)}"
        )
        await reply_target.answer(
            f"{confirmed_line} Sold {fraction * 100:.0f}% of {html.escape(trade.symbol)} — "
            f"received {result['sol_received']:.4f} SOL\n"
            f"Tx: <code>{result['signature']}</code>"
            f"{_confirmation_note(result['confirmation'])}"
            f"{positions_hint}"
            f"{sync_line}",
            reply_markup=real_wallet_positions_list_kb() if updated_trade.status != "open" else real_trade_position_kb(updated_trade.id),
        )
    except Exception as e:
        logger.error(f"execute_and_report_sell error (trade {trade_id}): {e}")
        try:
            await reply_target.answer(
                "⚠️ Something went wrong while processing that sell (price lookup or blockchain "
                "data provider unreachable). Please check /positions before retrying, to avoid a "
                "duplicate sell."
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Position Management — live open positions with entry/current/PnL/ROI
# ---------------------------------------------------------------------------

def _format_position_text(pos: dict) -> str:
    t = pos["trade"]
    pnl = pos["unrealized_pnl_sol"]
    pnl_sign = "+" if pnl >= 0 else ""
    stale_note = " <i>(cached price)</i>" if pos["price_stale"] else ""
    return (
        f"📌 <b>{html.escape(t.symbol or '???')}</b>\n"
        f"Entry: ${pos['entry_price']:.8f}   Current: ${pos['current_price']:.8f}{stale_note}\n"
        f"Remaining: {pos['remaining_quantity']:,.2f} {html.escape(t.symbol or '')}\n"
        f"Value: {pos['current_value_sol']:.4f} SOL\n"
        f"Unrealized PnL: {pnl_sign}{pnl:.4f} SOL ({pnl_sign}{pos['roi_pct']:.1f}% ROI)"
    )


@router.callback_query(F.data == "rw:positions")
async def cb_positions(callback: CallbackQuery):
    await callback.answer("Refreshing...")
    positions = await real_trade_engine.get_real_positions_view(callback.from_user.id)

    if not positions:
        await callback.message.answer(
            "📊 No open Real Wallet positions right now.",
            reply_markup=real_wallet_positions_list_kb(),
        )
        return

    total_value = sum(p["current_value_sol"] for p in positions)
    total_pnl = sum(p["unrealized_pnl_sol"] for p in positions)
    pnl_sign = "+" if total_pnl >= 0 else ""
    header = (
        f"📊 <b>Open Positions ({len(positions)})</b>\n"
        f"Total value: {total_value:.4f} SOL   Unrealized PnL: {pnl_sign}{total_pnl:.4f} SOL\n"
    )
    await callback.message.answer(header, reply_markup=real_wallet_positions_list_kb())

    for pos in positions:
        await callback.message.answer(
            _format_position_text(pos),
            reply_markup=real_trade_position_kb(pos["trade"].id),
        )


@router.callback_query(F.data.startswith("rw:position_refresh:"))
async def cb_position_refresh(callback: CallbackQuery):
    # Acknowledge immediately, before parsing and before the price-fetching
    # get_real_positions_view call (one DexScreener call per open trade,
    # which can take a while under upstream rate limits). A callback can
    # only be answered once, so every failure branch below reports through
    # the message text instead of a toast alert.
    await callback.answer()

    try:
        trade_id = int(callback.data.split(":")[-1])
        positions = await real_trade_engine.get_real_positions_view(callback.from_user.id)
        pos = next((p for p in positions if p["trade"].id == trade_id), None)

        if not pos:
            await callback.message.edit_text(
                "Position not found or already closed.",
                reply_markup=real_wallet_positions_list_kb(),
            )
            return

        await callback.message.edit_text(
            _format_position_text(pos),
            reply_markup=real_trade_position_kb(trade_id),
        )
    except TelegramBadRequest as e:
        # Harmless: the refreshed text/markup was identical to what's
        # already on screen (price hadn't moved) — nothing to report.
        if "message is not modified" not in str(e).lower():
            logger.error(f"position refresh edit failed: {e}")
    except Exception as e:
        logger.error(f"position refresh error: {e}")
        try:
            await callback.message.answer("⚠️ Could not refresh this position right now. Please try again.")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Premium — Take Profit / Stop Loss / Partial Take Profit
# ---------------------------------------------------------------------------

_EXIT_KIND_LABELS = {"tp": "Take Profit", "sl": "Stop Loss", "ptp": "Partial Take Profit"}


@router.callback_query(F.data.startswith("rw:exit_menu:"))
async def cb_exit_menu(callback: CallbackQuery):
    trade_id = int(callback.data.split(":")[-1])

    rules = await real_exit_engine.get_rules_for_trade(callback.from_user.id, trade_id)
    active_rules = [r for r in rules if r.status == "active"]
    lines = ["🎯 <b>Take Profit / Stop Loss</b>\n"]
    if active_rules:
        for r in active_rules:
            label = _EXIT_KIND_LABELS[r.kind]
            direction = "-" if r.kind == "sl" else "+"
            extra = f" (sell {r.sell_fraction * 100:.0f}%)" if r.kind == "ptp" else ""
            lines.append(f"• {label}: {direction}{r.trigger_pct:g}% from entry{extra}")
    else:
        lines.append("No active rules on this position yet.")
    lines.append("\nTap a rule below to remove it, or add a new one.")

    await callback.message.edit_text("\n".join(lines), reply_markup=real_wallet_exit_menu_kb(trade_id, rules))
    await callback.answer()


@router.callback_query(F.data.startswith("rw:exit_add:"))
async def cb_exit_add(callback: CallbackQuery, state: FSMContext):
    _, _, kind, trade_id = callback.data.split(":")
    trade_id = int(trade_id)

    await state.set_state(RealWalletStates.waiting_exit_trigger_pct)
    await state.update_data(exit_kind=kind, exit_trade_id=trade_id)
    label = _EXIT_KIND_LABELS[kind]
    direction = "drops" if kind == "sl" else "rises"
    await callback.message.edit_text(
        f"✏️ <b>{label}</b>\n\nHow many % should price {direction} from entry to trigger this? "
        f"(e.g. <code>50</code> for 50%)\n\nSend /cancel to back out."
    )
    await callback.answer()


@router.message(RealWalletStates.waiting_exit_trigger_pct)
async def on_exit_trigger_pct_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    try:
        trigger_pct = float(raw)
        if trigger_pct <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a positive number, e.g. <code>50</code>, or /cancel.")
        return

    data = await state.get_data()
    kind = data.get("exit_kind")
    trade_id = data.get("exit_trade_id")

    if kind == "ptp":
        await state.update_data(exit_trigger_pct=trigger_pct)
        await state.set_state(RealWalletStates.waiting_exit_sell_fraction)
        await message.answer(
            "✏️ <b>Sell fraction</b>\n\nWhat % of the position should this rung sell when it triggers? "
            "(e.g. <code>25</code> for 25%)\n\nSend /cancel to back out."
        )
        return

    await state.clear()
    try:
        rule = await real_exit_engine.create_rule(
            user_id=message.from_user.id, trade_id=trade_id, kind=kind, trigger_pct=trigger_pct, sell_fraction=1.0,
        )
    except real_exit_engine.ExitRuleValidationError as e:
        await message.answer(f"❌ {html.escape(str(e))}")
        return

    rules = await real_exit_engine.get_rules_for_trade(message.from_user.id, trade_id)
    await message.answer(
        f"✅ {_EXIT_KIND_LABELS[kind]} set at {trigger_pct:g}% from entry.",
        reply_markup=real_wallet_exit_menu_kb(trade_id, rules),
    )


@router.message(RealWalletStates.waiting_exit_sell_fraction)
async def on_exit_sell_fraction_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    try:
        pct = float(raw)
        if not (0 < pct <= 100):
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a number between 1 and 100, or /cancel.")
        return

    data = await state.get_data()
    trade_id = data.get("exit_trade_id")
    trigger_pct = data.get("exit_trigger_pct")
    await state.clear()
    if trade_id is None or trigger_pct is None:
        await message.answer("❌ Lost track of this setup — open TP/SL again from the position.")
        return

    try:
        await real_exit_engine.create_rule(
            user_id=message.from_user.id, trade_id=trade_id, kind="ptp",
            trigger_pct=trigger_pct, sell_fraction=pct / 100,
        )
    except real_exit_engine.ExitRuleValidationError as e:
        await message.answer(f"❌ {html.escape(str(e))}")
        return

    rules = await real_exit_engine.get_rules_for_trade(message.from_user.id, trade_id)
    await message.answer(
        f"✅ Partial Take Profit set: sell {pct:g}% at +{trigger_pct:g}% from entry.",
        reply_markup=real_wallet_exit_menu_kb(trade_id, rules),
    )


@router.callback_query(F.data.startswith("rw:exit_cancel:"))
async def cb_exit_cancel(callback: CallbackQuery):
    _, _, rule_id, trade_id = callback.data.split(":")
    ok = await real_exit_engine.cancel_rule(callback.from_user.id, int(rule_id))
    if not ok:
        await callback.answer("Rule not found or already gone.", show_alert=True)
        return
    rules = await real_exit_engine.get_rules_for_trade(callback.from_user.id, int(trade_id))
    await callback.message.edit_reply_markup(reply_markup=real_wallet_exit_menu_kb(int(trade_id), rules))
    await callback.answer("Removed.")


# ---------------------------------------------------------------------------
# Premium — Limit Orders
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "rw:limit_list")
async def cb_limit_list(callback: CallbackQuery):
    orders = await real_limit_order_engine.get_open_orders(callback.from_user.id)
    text = "🎯 <b>Limit Orders</b>\n\n" + (
        "No open limit orders." if not orders else f"You have {len(orders)} open order(s)."
    )
    await callback.message.edit_text(text, reply_markup=real_wallet_limit_list_kb(orders))
    await callback.answer()


@router.callback_query(F.data == "rw:limit_new")
async def cb_limit_new(callback: CallbackQuery, state: FSMContext):
    wallet = await get_real_wallet(callback.from_user.id)
    if not wallet:
        await callback.answer("No active Real Wallet. Set one up first.", show_alert=True)
        return

    await callback.message.edit_text(
        "🎯 <b>New Limit Order</b>\n\nSend the token's contract address (mint).\n\nSend /cancel to back out."
    )
    await state.set_state(RealWalletStates.waiting_limit_contract)
    await callback.answer()


@router.message(RealWalletStates.waiting_limit_contract)
async def on_limit_contract_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    info = await get_token_card_info(raw)
    if not info:
        await message.answer("❌ Couldn't find that token. Double-check the contract address, or /cancel.")
        return

    await state.update_data(limit_contract=raw, limit_name=info.get("name"), limit_symbol=info.get("symbol"))
    price = float(info["price"]) if info.get("price") not in (None, "N/A") else None
    price_line = f"Current price: <b>${price:.8f}</b>\n\n" if price is not None else ""
    await message.answer(
        f"{price_line}When should this order fire?",
        reply_markup=real_wallet_limit_direction_kb(),
    )


@router.callback_query(F.data.startswith("rw:limit_dir:"))
async def cb_limit_direction(callback: CallbackQuery, state: FSMContext):
    direction = callback.data.split(":")[-1]
    await state.update_data(limit_direction=direction)
    await state.set_state(RealWalletStates.waiting_limit_price)
    verb = "drops to" if direction == "buy_below" else "rises to"
    await callback.message.edit_text(f"✏️ Send the target price ($) — buy when price {verb} this.\n\nSend /cancel to back out.")
    await callback.answer()


@router.message(RealWalletStates.waiting_limit_price)
async def on_limit_price_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    try:
        price = float(raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a positive price, or /cancel.")
        return

    await state.update_data(limit_price=price)
    await state.set_state(RealWalletStates.waiting_limit_amount)
    await message.answer("✏️ How much SOL should this order spend when it fires? (e.g. <code>0.1</code>)\n\nSend /cancel to back out.")


@router.message(RealWalletStates.waiting_limit_amount)
async def on_limit_amount_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    try:
        sol_amount = float(raw)
        if sol_amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a positive number of SOL, or /cancel.")
        return

    data = await state.get_data()
    await state.clear()
    required = ("limit_contract", "limit_direction", "limit_price")
    if any(data.get(k) is None for k in required):
        await message.answer("❌ Lost track of this setup — tap New Limit Order again.")
        return

    try:
        order = await real_limit_order_engine.create_order(
            user_id=message.from_user.id,
            contract=data["limit_contract"],
            name=data.get("limit_name"),
            symbol=data.get("limit_symbol"),
            direction=data["limit_direction"],
            trigger_price=data["limit_price"],
            sol_amount=sol_amount,
        )
    except real_limit_order_engine.LimitOrderValidationError as e:
        await message.answer(f"❌ {html.escape(str(e))}")
        return

    arrow = "≤" if order.direction == "buy_below" else "≥"
    orders = await real_limit_order_engine.get_open_orders(message.from_user.id)
    await message.answer(
        f"✅ <b>Limit order created</b>\n\n{order.symbol or order.contract[:6]} — buy {sol_amount} SOL "
        f"when price {arrow} ${order.trigger_price:.8f}",
        reply_markup=real_wallet_limit_list_kb(orders),
    )


@router.callback_query(F.data.startswith("rw:limit_view:"))
async def cb_limit_view(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[-1])
    orders = await real_limit_order_engine.get_open_orders(callback.from_user.id)
    order = next((o for o in orders if o.id == order_id), None)
    if not order:
        await callback.answer("Order not found or already gone.", show_alert=True)
        return

    arrow = "≤" if order.direction == "buy_below" else "≥"
    text = (
        f"🎯 <b>{order.symbol or order.contract[:6]}</b>\n"
        f"<code>{order.contract}</code>\n\n"
        f"Buy {order.sol_amount} SOL when price {arrow} ${order.trigger_price:.8f}\n"
        f"Status: {order.status}"
    )
    await callback.message.edit_text(text, reply_markup=real_wallet_limit_detail_kb(order.id))
    await callback.answer()


@router.callback_query(F.data.startswith("rw:limit_cancel:"))
async def cb_limit_cancel(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[-1])
    ok = await real_limit_order_engine.cancel_order(callback.from_user.id, order_id)
    if not ok:
        await callback.answer("Order not found or already gone.", show_alert=True)
        return
    await callback.answer("Order cancelled.")
    orders = await real_limit_order_engine.get_open_orders(callback.from_user.id)
    await callback.message.edit_text(
        "🎯 <b>Limit Orders</b>\n\n" + ("No open limit orders." if not orders else f"You have {len(orders)} open order(s)."),
        reply_markup=real_wallet_limit_list_kb(orders),
    )


# ---------------------------------------------------------------------------
# Portfolio — unified wallet value across SOL + every SPL token held
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "rw:portfolio")
async def cb_portfolio(callback: CallbackQuery):
    wallet = await get_real_wallet(callback.from_user.id)
    if not wallet:
        await callback.answer("No active Real Wallet. Use /realwallet to set one up.", show_alert=True)
        return

    await callback.answer("Loading portfolio...")
    report = await build_wallet_portfolio_report(wallet.public_key)
    await callback.message.edit_text(report, reply_markup=real_wallet_back_kb())


# ---------------------------------------------------------------------------
# Withdraw — SOL or any SPL token held, to an external address
# ---------------------------------------------------------------------------

WITHDRAW_INTRO = (
    "🏧 <b>Withdraw</b>\n\n"
    "Choose what you want to withdraw. This sends funds OUT of AlphaPulse "
    "to an address you control — Solana transactions are irreversible "
    "once confirmed, so double-check the address before confirming.\n"
)


@router.callback_query(F.data == "rw:withdraw")
async def cb_withdraw_start(callback: CallbackQuery, state: FSMContext):
    wallet = await get_real_wallet(callback.from_user.id)
    if not wallet:
        await callback.answer("No active Real Wallet. Use /realwallet to set one up.", show_alert=True)
        return

    await callback.answer("Loading balances...")
    tokens = await fetch_wallet_fungible_tokens(wallet.public_key)
    if tokens is None:
        await callback.message.edit_text(
            "⚠️ Couldn't load your wallet balances right now (blockchain data "
            "provider unreachable). This does NOT mean your balance is zero — "
            "please try again in a minute.",
            reply_markup=real_wallet_back_kb(),
        )
        return
    if not tokens:
        await callback.message.edit_text(
            "🏧 Nothing to withdraw — this wallet has no SOL or token balance right now.",
            reply_markup=real_wallet_back_kb(),
        )
        return

    await state.update_data(withdraw_tokens=tokens)
    await callback.message.edit_text(WITHDRAW_INTRO, reply_markup=real_wallet_withdraw_asset_kb(tokens))


@router.callback_query(F.data.startswith("rw:withdraw_asset:"))
async def cb_withdraw_pick_asset(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[-1])
    data = await state.get_data()
    tokens = data.get("withdraw_tokens") or []
    if idx >= len(tokens):
        await callback.answer("That list expired — tap Withdraw again.", show_alert=True)
        return

    asset = tokens[idx]
    await state.update_data(withdraw_asset=asset)
    await state.set_state(RealWalletStates.waiting_withdraw_address)
    await callback.message.edit_text(
        f"🏧 <b>Withdraw {html.escape(asset['symbol'])}</b>\n\n"
        f"Available: <b>{asset['amount']:,.6f} {html.escape(asset['symbol'])}</b>\n\n"
        "Send the destination Solana address now, as its own message.\n\n"
        "Send /cancel to back out."
    )
    await callback.answer()


@router.message(RealWalletStates.waiting_withdraw_address)
async def on_withdraw_address_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    if not wallet_withdraw.validate_withdraw_address(raw):
        await message.answer("❌ That doesn't look like a valid Solana address. Try again, or /cancel.")
        return

    data = await state.get_data()
    asset = data.get("withdraw_asset")
    if not asset:
        await state.clear()
        await message.answer("❌ Lost track of which asset this was for — tap Withdraw again.")
        return

    await state.update_data(withdraw_address=raw)
    await message.answer(
        f"Destination:\n<code>{raw}</code>\n\nHow much {html.escape(asset['symbol'])} do you want to send?",
        reply_markup=real_wallet_withdraw_amount_kb(asset["symbol"]),
    )


async def _resolve_withdraw_amount(user_id: int, asset: dict, fraction: float) -> float:
    """Applies a % preset against the live withdrawable balance — for
    SOL specifically at 100% this accounts for the rent/fee reserve
    (see services.wallet_withdraw.SOL_WITHDRAW_RESERVE) rather than just
    multiplying the raw balance, so "Max" can't accidentally try to
    drain the account below rent-exempt."""
    if asset["mint"] == WRAPPED_SOL_MINT and fraction >= 1.0:
        return await wallet_withdraw.get_max_sol_withdrawable(user_id)
    return asset["amount"] * fraction


@router.callback_query(F.data.startswith("rw:withdraw_pct:"))
async def cb_withdraw_pct(callback: CallbackQuery, state: FSMContext):
    fraction = float(callback.data.split(":")[-1])
    data = await state.get_data()
    asset = data.get("withdraw_asset")
    address = data.get("withdraw_address")
    if not asset or not address:
        await callback.answer("Lost track of this withdrawal — tap Withdraw again.", show_alert=True)
        return

    # _resolve_withdraw_amount's "Max SOL" path makes a live get_sol_balance
    # RPC call, which can be slow under upstream rate limits — answer first
    # so the spinner clears immediately instead of waiting on it. A callback
    # can only be answered once, so every failure branch below is now
    # reported via the message text instead of a toast alert.
    await callback.answer()

    try:
        amount = await _resolve_withdraw_amount(callback.from_user.id, asset, fraction)
        if amount <= 0:
            await callback.message.edit_text(
                "Nothing available to withdraw.",
                reply_markup=real_wallet_back_kb(),
            )
            return

        await state.update_data(withdraw_amount=amount)
        await _show_withdraw_confirmation(callback.message, asset, address, amount, edit=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"withdraw pct edit failed: {e}")
    except Exception as e:
        logger.error(f"withdraw pct error: {e}")
        try:
            await callback.message.edit_text(
                "⚠️ Couldn't check your balance right now (blockchain data provider unreachable). "
                "Please try again in a minute.",
                reply_markup=real_wallet_back_kb(),
            )
        except Exception:
            pass


@router.callback_query(F.data == "rw:withdraw_custom_amount")
async def cb_withdraw_custom_amount(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RealWalletStates.waiting_withdraw_custom_amount)
    await callback.message.edit_text(
        "✏️ <b>Custom Amount</b>\n\nSend the amount to withdraw (e.g. <code>0.5</code>).\n\nSend /cancel to back out."
    )
    await callback.answer()


@router.message(RealWalletStates.waiting_withdraw_custom_amount)
async def on_withdraw_custom_amount_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a positive number, e.g. <code>0.5</code>, or /cancel.")
        return

    data = await state.get_data()
    asset = data.get("withdraw_asset")
    address = data.get("withdraw_address")
    if not asset or not address:
        await state.clear()
        await message.answer("❌ Lost track of this withdrawal — tap Withdraw again.")
        return

    if amount > asset["amount"] * 1.0001:  # small tolerance for float noise
        await message.answer(
            f"❌ You only have {asset['amount']:,.6f} {html.escape(asset['symbol'])} available. Enter a smaller amount, or /cancel."
        )
        return

    await state.update_data(withdraw_amount=amount)
    await _show_withdraw_confirmation(message, asset, address, amount, edit=False)


async def _show_withdraw_confirmation(target, asset: dict, address: str, amount: float, edit: bool):
    text = (
        "🏧 <b>Confirm Withdrawal</b>\n\n"
        f"Asset: <b>{html.escape(asset['symbol'])}</b>\n"
        f"Amount: <b>{amount:,.6f} {html.escape(asset['symbol'])}</b>\n"
        f"To:\n<code>{address}</code>\n\n"
        "⚠️ This is irreversible once confirmed on-chain. Double-check the address."
    )
    kb = real_wallet_withdraw_confirm_kb()
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "rw:withdraw_confirm")
async def cb_withdraw_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    asset = data.get("withdraw_asset")
    address = data.get("withdraw_address")
    amount = data.get("withdraw_amount")
    await state.clear()

    if not asset or not address or not amount:
        await callback.answer("Lost track of this withdrawal — tap Withdraw again.", show_alert=True)
        return

    await callback.answer("Submitting withdrawal...")

    if asset["mint"] == WRAPPED_SOL_MINT:
        result = await wallet_withdraw.execute_sol_withdrawal(callback.from_user.id, address, amount)
    else:
        try:
            decimals = await get_mint_decimals(asset["mint"])
        except Exception:
            await callback.message.edit_text(
                f"❌ Couldn't verify {html.escape(asset['symbol'])}'s decimals on-chain — try again shortly.",
                reply_markup=real_wallet_back_kb(),
            )
            return
        result = await wallet_withdraw.execute_spl_withdrawal(
            callback.from_user.id, asset["mint"], asset["symbol"], amount, decimals, address
        )

    if not result["ok"]:
        await callback.message.edit_text(
            f"❌ Withdrawal failed: {html.escape(result['reason'])}",
            reply_markup=real_wallet_back_kb(),
        )
        return

    confirmed_line = "✅" if result["confirmation"] == "confirmed" else "📡"
    note = _confirmation_note(result["confirmation"])
    await callback.message.edit_text(
        f"{confirmed_line} Withdrew {amount:,.6f} {html.escape(asset['symbol'])}\n"
        f"Tx: <code>{result['signature']}</code>{note}",
        reply_markup=real_wallet_back_kb(),
    )


# ---------------------------------------------------------------------------
# Real Wallet DCA — fully customizable interval-based schedules
# ---------------------------------------------------------------------------

def _dca_schedule_text(schedule) -> str:
    guard_lines = []
    if schedule.price_floor is not None:
        guard_lines.append(f"Price floor: ${schedule.price_floor:.10f}".rstrip("0").rstrip("."))
    if schedule.price_ceiling is not None:
        guard_lines.append(f"Price ceiling: ${schedule.price_ceiling:.10f}".rstrip("0").rstrip("."))
    guard_text = ("\n" + "\n".join(guard_lines)) if guard_lines else ""
    error_text = f"\n\n⚠️ Last note: {html.escape(schedule.last_error)}" if schedule.last_error else ""

    return (
        f"🧬 <b>DCA — {html.escape(schedule.symbol or schedule.contract[:8])}</b>\n\n"
        f"Status: <b>{schedule.status}</b>\n"
        f"Progress: <b>{schedule.orders_filled}/{schedule.total_orders}</b> orders\n"
        f"Amount per order: <b>{schedule.amount_per_order_sol} SOL</b>\n"
        f"Interval: <b>{schedule.interval_seconds}s</b>"
        f"{guard_text}"
        f"{error_text}"
    )


@router.callback_query(F.data == "rw:dca_list")
async def cb_dca_list(callback: CallbackQuery):
    wallet = await get_real_wallet(callback.from_user.id)
    if not wallet:
        await callback.answer("No active Real Wallet. Use /realwallet to set one up.", show_alert=True)
        return

    schedules = await real_dca_engine.list_schedules(callback.from_user.id)
    text = (
        "🧬 <b>Real Wallet DCA Schedules</b>\n\n"
        "Buy a fixed SOL amount of a token on a repeating interval, "
        "fully on your own terms — amount, interval, total orders, and "
        "optional price floor/ceiling.\n\n"
        + (f"You have {len(schedules)} active/paused schedule(s)." if schedules else "No schedules yet.")
    )
    await callback.message.edit_text(text, reply_markup=real_wallet_dca_list_kb(schedules))
    await callback.answer()


@router.callback_query(F.data.startswith("rw:dca_view:"))
async def cb_dca_view(callback: CallbackQuery):
    schedule_id = int(callback.data.split(":")[-1])
    schedule = await real_dca_engine.get_schedule(schedule_id, callback.from_user.id)
    if not schedule:
        await callback.answer("Schedule not found.", show_alert=True)
        return
    await callback.message.edit_text(
        _dca_schedule_text(schedule),
        reply_markup=real_wallet_dca_detail_kb(schedule.id, schedule.status),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rw:dca_pause:"))
async def cb_dca_pause(callback: CallbackQuery):
    schedule_id = int(callback.data.split(":")[-1])
    ok = await real_dca_engine.pause_schedule(schedule_id, callback.from_user.id)
    if not ok:
        await callback.answer("Couldn't pause — it may already be paused or finished.", show_alert=True)
        return
    schedule = await real_dca_engine.get_schedule(schedule_id, callback.from_user.id)
    await callback.message.edit_text(_dca_schedule_text(schedule), reply_markup=real_wallet_dca_detail_kb(schedule.id, schedule.status))
    await callback.answer("Paused.")


@router.callback_query(F.data.startswith("rw:dca_resume:"))
async def cb_dca_resume(callback: CallbackQuery):
    schedule_id = int(callback.data.split(":")[-1])
    ok = await real_dca_engine.resume_schedule(schedule_id, callback.from_user.id)
    if not ok:
        await callback.answer("Couldn't resume — it may not be paused.", show_alert=True)
        return
    schedule = await real_dca_engine.get_schedule(schedule_id, callback.from_user.id)
    await callback.message.edit_text(_dca_schedule_text(schedule), reply_markup=real_wallet_dca_detail_kb(schedule.id, schedule.status))
    await callback.answer("Resumed — next order will be evaluated shortly.")


@router.callback_query(F.data.startswith("rw:dca_cancel:"))
async def cb_dca_cancel_confirm_prompt(callback: CallbackQuery):
    schedule_id = int(callback.data.split(":")[-1])
    await callback.message.edit_text(
        "❌ <b>Cancel this DCA schedule?</b>\n\nRemaining orders will not be placed. This can't be undone.",
        reply_markup=real_wallet_dca_cancel_confirm_kb(schedule_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rw:dca_cancel_confirm:"))
async def cb_dca_cancel(callback: CallbackQuery):
    schedule_id = int(callback.data.split(":")[-1])
    await real_dca_engine.cancel_schedule(schedule_id, callback.from_user.id)
    schedules = await real_dca_engine.list_schedules(callback.from_user.id)
    await callback.message.edit_text(
        "✅ Schedule cancelled.", reply_markup=real_wallet_dca_list_kb(schedules)
    )
    await callback.answer()


# --- DCA creation wizard ---------------------------------------------------

@router.callback_query(F.data == "rw:dca_new")
async def cb_dca_new_start(callback: CallbackQuery, state: FSMContext):
    wallet = await get_real_wallet(callback.from_user.id)
    if not wallet:
        await callback.answer("No active Real Wallet. Set one up first.", show_alert=True)
        return
    await state.set_state(RealWalletStates.waiting_dca_contract)
    await callback.message.edit_text(
        "➕ <b>New DCA Schedule</b>\n\nSend the token's contract address (mint) to DCA into.\n\nSend /cancel to back out."
    )
    await callback.answer()


@router.message(RealWalletStates.waiting_dca_contract)
async def on_dca_contract_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    info = await get_token_card_info(raw)
    if not info:
        await message.answer("❌ Couldn't find that token. Double-check the contract address, or /cancel.")
        return

    await state.update_data(dca_contract=raw, dca_name=info.get("name"), dca_symbol=info.get("symbol"))
    await state.set_state(RealWalletStates.waiting_dca_amount)
    await message.answer(
        f"✏️ <b>Amount per order</b>\n\nHow much SOL should each order spend on {html.escape(info.get('symbol') or 'this token')}? (e.g. <code>0.1</code>)\n\nSend /cancel to back out."
    )


@router.message(RealWalletStates.waiting_dca_amount)
async def on_dca_amount_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a positive number of SOL, e.g. <code>0.1</code>, or /cancel.")
        return

    await state.update_data(dca_amount=amount)
    await state.set_state(RealWalletStates.waiting_dca_interval)
    await message.answer(
        "✏️ <b>Interval</b>\n\nHow often should an order fire, in minutes? (e.g. <code>60</code> for hourly, "
        f"minimum {real_dca_engine.MIN_INTERVAL_SECONDS // 60} minute(s))\n\nSend /cancel to back out."
    )


@router.message(RealWalletStates.waiting_dca_interval)
async def on_dca_interval_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    try:
        minutes = float(raw)
        interval_seconds = int(minutes * 60)
        if interval_seconds < real_dca_engine.MIN_INTERVAL_SECONDS:
            raise ValueError
    except ValueError:
        await message.answer(
            f"❌ Enter a number of minutes, at least {real_dca_engine.MIN_INTERVAL_SECONDS // 60}, or /cancel."
        )
        return

    await state.update_data(dca_interval_seconds=interval_seconds)
    await state.set_state(RealWalletStates.waiting_dca_total_orders)
    await message.answer(
        f"✏️ <b>Total orders</b>\n\nHow many total orders should this schedule place (1-{real_dca_engine.MAX_TOTAL_ORDERS})?\n\nSend /cancel to back out."
    )


@router.message(RealWalletStates.waiting_dca_total_orders)
async def on_dca_total_orders_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    max_orders = real_dca_engine.MAX_TOTAL_ORDERS

    try:
        total_orders = int(raw)
        if not (1 <= total_orders <= max_orders):
            raise ValueError
    except ValueError:
        await message.answer(f"❌ Enter a whole number between 1 and {max_orders}, or /cancel.")
        return

    await state.update_data(dca_total_orders=total_orders)

    # Full DCA engine (including price floor/ceiling guard rails) is
    # available to every user — no Premium gate.
    await state.set_state(RealWalletStates.waiting_dca_price_floor)
    await message.answer(
        "✏️ <b>Price floor (optional)</b>\n\nSkip an order if price drops below this ($). Send a number, or skip.",
        reply_markup=real_wallet_dca_skip_optional_kb("floor"),
    )


@router.message(RealWalletStates.waiting_dca_price_floor)
async def on_dca_price_floor_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    try:
        floor = float(raw)
        if floor <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a positive number, or use the Skip button, or /cancel.")
        return

    await state.update_data(dca_price_floor=floor)
    await state.set_state(RealWalletStates.waiting_dca_price_ceiling)
    await message.answer(
        "✏️ <b>Price ceiling (optional)</b>\n\nSkip an order if price rises above this ($). Send a number, or skip.",
        reply_markup=real_wallet_dca_skip_optional_kb("ceiling"),
    )


@router.message(RealWalletStates.waiting_dca_price_ceiling)
async def on_dca_price_ceiling_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    try:
        ceiling = float(raw)
        if ceiling <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a positive number, or use the Skip button, or /cancel.")
        return

    await state.update_data(dca_price_ceiling=ceiling)
    await _finalize_dca_schedule(message, message.from_user.id, state, is_callback=False)


@router.callback_query(F.data.startswith("rw:dca_new_skip:"))
async def cb_dca_new_skip(callback: CallbackQuery, state: FSMContext):
    step = callback.data.split(":")[-1]
    await callback.answer()
    if step == "floor":
        await state.set_state(RealWalletStates.waiting_dca_price_ceiling)
        await callback.message.edit_text(
            "✏️ <b>Price ceiling (optional)</b>\n\nSkip an order if price rises above this ($). Send a number, or skip.",
            reply_markup=real_wallet_dca_skip_optional_kb("ceiling"),
        )
    elif step == "ceiling":
        await _finalize_dca_schedule(callback.message, callback.from_user.id, state, is_callback=True)


async def _finalize_dca_schedule(target, user_id: int, state: FSMContext, is_callback: bool):
    data = await state.get_data()
    await state.clear()

    required = ("dca_contract", "dca_amount", "dca_interval_seconds", "dca_total_orders")
    if any(data.get(k) is None for k in required):
        await target.answer("❌ Lost track of this setup — tap New DCA Schedule again.")
        return

    try:
        schedule = await real_dca_engine.create_schedule(
            user_id=user_id,
            contract=data["dca_contract"],
            name=data.get("dca_name"),
            symbol=data.get("dca_symbol"),
            amount_per_order_sol=data["dca_amount"],
            interval_seconds=data["dca_interval_seconds"],
            total_orders=data["dca_total_orders"],
            price_floor=data.get("dca_price_floor"),
            price_ceiling=data.get("dca_price_ceiling"),
        )
    except real_dca_engine.DCAValidationError as e:
        await target.answer(f"❌ {html.escape(str(e))}")
        return

    text = (
        f"✅ <b>DCA schedule created</b>\n\n{_dca_schedule_text(schedule)}\n\n"
        "First order will be evaluated on the next scheduler tick."
    )
    kb = real_wallet_dca_detail_kb(schedule.id, schedule.status)
    if is_callback:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)
