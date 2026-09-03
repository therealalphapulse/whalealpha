from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from domain.trading.real.solana_wallet import AUTO_DAILY_CAP_PRESETS_SOL

BUY_PRESETS_SOL = [0.1, 0.5, 1.0, 2.0]
SLIPPAGE_LABELS_BPS = {50: "0.5%", 100: "1%", 150: "1.5%", 300: "3%", 500: "5%"}
PRIORITY_LABELS = {"auto": "⚙️ Auto", "fast": "🚀 Fast", "turbo": "⚡ Turbo"}


def real_wallet_onboarding_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Create New Wallet", callback_data="rw:create")],
        [InlineKeyboardButton(text="📥 Import Existing Wallet", callback_data="rw:import")],
        [InlineKeyboardButton(text="ℹ️ How This Works", callback_data="rw:info")],
    ])


def real_wallet_created_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ I've saved my key — continue", callback_data="rw:ack_saved")]])


def real_wallet_menu_kb(auto_trading_enabled: bool) -> InlineKeyboardMarkup:
    auto_label = "🟢 Automation: ON" if auto_trading_enabled else "⚪ Automation: OFF"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Buy", callback_data="rw:buy_start"), InlineKeyboardButton(text="💱 Sell", callback_data="rw:positions")],
        [InlineKeyboardButton(text="📊 Positions", callback_data="rw:positions"), InlineKeyboardButton(text="💼 Portfolio", callback_data="rw:portfolio")],
        [InlineKeyboardButton(text="📜 Trade History", callback_data="rw:history"), InlineKeyboardButton(text="⚙️ Trade Settings", callback_data="rw:settings")],
        [InlineKeyboardButton(text="🏧 Withdraw", callback_data="rw:withdraw"), InlineKeyboardButton(text="🔁 Refresh", callback_data="rw:balance")],
        [InlineKeyboardButton(text="🧬 DCA Schedules", callback_data="rw:dca_list")],
        [InlineKeyboardButton(text=auto_label, callback_data="rw:automation")],
        [InlineKeyboardButton(text="🎯 Limit Orders 💎", callback_data="rw:limit_list")],
        [InlineKeyboardButton(text="🔑 Export Private Key", callback_data="rw:export")],
        [InlineKeyboardButton(text="🔌 Disconnect Wallet", callback_data="rw:disconnect_confirm")],
    ])


def real_wallet_disconnect_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Yes, disconnect", callback_data="rw:disconnect"), InlineKeyboardButton(text="❌ Cancel", callback_data="rw:menu")]])


def real_wallet_export_warning_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚠️ Show my private key", callback_data="rw:export_confirm")], [InlineKeyboardButton(text="⬅️ Back", callback_data="rw:menu")]])


def real_wallet_buy_presets_kb(contract: str) -> InlineKeyboardMarkup:
    preset_row = [InlineKeyboardButton(text=f"{amt} SOL", callback_data=f"rwbuy:exec:{contract}:{amt}") for amt in BUY_PRESETS_SOL]
    return InlineKeyboardMarkup(inline_keyboard=[preset_row[:2], preset_row[2:], [InlineKeyboardButton(text="✏️ Custom amount", callback_data=f"rwbuy:custom:{contract}")], [InlineKeyboardButton(text="❌ Cancel", callback_data="rw:menu")]])


def real_trade_position_kb(trade_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Sell 25%", callback_data=f"rw:sell:{trade_id}:0.25"), InlineKeyboardButton(text="Sell 50%", callback_data=f"rw:sell:{trade_id}:0.5")],
        [InlineKeyboardButton(text="Sell 75%", callback_data=f"rw:sell:{trade_id}:0.75"), InlineKeyboardButton(text="Sell 100%", callback_data=f"rw:sell:{trade_id}:1.0")],
        [InlineKeyboardButton(text="✏️ Custom %", callback_data=f"rw:sell_custom:{trade_id}"), InlineKeyboardButton(text="🔁 Refresh", callback_data=f"rw:position_refresh:{trade_id}")],
        [InlineKeyboardButton(text="📊 Generate PnL Card", callback_data=f"rw:pnlcard:{trade_id}")],
        [InlineKeyboardButton(text="🎯 TP / SL 💎", callback_data=f"rw:exit_menu:{trade_id}")],
    ])


def premium_upsell_kb(back_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 View Premium Benefits", callback_data="premium:refresh")], [InlineKeyboardButton(text="⬅️ Back", callback_data=back_callback)]])


def real_wallet_exit_menu_kb(trade_id: int, rules: list) -> InlineKeyboardMarkup:
    rows = []
    for r in rules:
        if r.status != "active":
            continue
        label_kind = {"tp": "🎯 TP", "sl": "🛑 SL", "ptp": "🎯 Partial TP"}.get(r.kind, r.kind)
        pct_label = f"+{r.trigger_pct:g}%" if r.kind != "sl" else f"-{r.trigger_pct:g}%"
        extra = f" (sell {r.sell_fraction * 100:.0f}%)" if r.kind == "ptp" else ""
        rows.append([InlineKeyboardButton(text=f"❌ {label_kind} {pct_label}{extra}", callback_data=f"rw:exit_cancel:{r.id}:{trade_id}")])
    rows.append([InlineKeyboardButton(text="🎯 Add Take Profit", callback_data=f"rw:exit_add:tp:{trade_id}"), InlineKeyboardButton(text="🛑 Add Stop Loss", callback_data=f"rw:exit_add:sl:{trade_id}")])
    rows.append([InlineKeyboardButton(text="🎯 Add Partial Take Profit", callback_data=f"rw:exit_add:ptp:{trade_id}")])
    rows.append([InlineKeyboardButton(text="🔁 Refresh", callback_data=f"rw:exit_menu:{trade_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Position", callback_data=f"rw:position_refresh:{trade_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def real_wallet_limit_list_kb(orders: list) -> InlineKeyboardMarkup:
    rows = []
    for o in orders:
        arrow = "≤" if o.direction == "buy_below" else "≥"
        label = f"{o.symbol or o.contract[:6]} — buy when {arrow} ${o.trigger_price:.8f}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"rw:limit_view:{o.id}")])
    rows.append([InlineKeyboardButton(text="➕ New Limit Order", callback_data="rw:limit_new")])
    rows.append([InlineKeyboardButton(text="⬅️ Wallet Menu", callback_data="rw:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def real_wallet_limit_detail_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel Order", callback_data=f"rw:limit_cancel:{order_id}")], [InlineKeyboardButton(text="⬅️ Limit Orders", callback_data="rw:limit_list")]])


def real_wallet_limit_direction_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📉 Buy when price drops to...", callback_data="rw:limit_dir:buy_below")], [InlineKeyboardButton(text="📈 Buy when price rises to...", callback_data="rw:limit_dir:buy_above")], [InlineKeyboardButton(text="❌ Cancel setup", callback_data="rw:limit_list")]])


def real_wallet_positions_list_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔁 Refresh All", callback_data="rw:positions")], [InlineKeyboardButton(text="⬅️ Wallet Menu", callback_data="rw:menu")]])


def real_wallet_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔁 Refresh", callback_data="rw:portfolio")], [InlineKeyboardButton(text="⬅️ Wallet Menu", callback_data="rw:menu")]])


def real_wallet_withdraw_asset_kb(tokens: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{t['symbol']} — {t['amount']:,.4f}", callback_data=f"rw:withdraw_asset:{i}")] for i, t in enumerate(tokens)]
    rows.append([InlineKeyboardButton(text="⬅️ Wallet Menu", callback_data="rw:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def real_wallet_withdraw_amount_kb(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="25%", callback_data="rw:withdraw_pct:0.25"), InlineKeyboardButton(text="50%", callback_data="rw:withdraw_pct:0.5")], [InlineKeyboardButton(text="100% (Max)", callback_data="rw:withdraw_pct:1.0"), InlineKeyboardButton(text="✏️ Custom amount", callback_data="rw:withdraw_custom_amount")], [InlineKeyboardButton(text="❌ Cancel", callback_data="rw:menu")]])


def real_wallet_withdraw_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Confirm Withdrawal", callback_data="rw:withdraw_confirm"), InlineKeyboardButton(text="❌ Cancel", callback_data="rw:menu")]])


def real_wallet_automation_kb(auto_enabled: bool, kill_switch: bool, daily_cap_sol: float) -> InlineKeyboardMarkup:
    toggle_label = "🟢 Turn Automation OFF" if auto_enabled else "⚪ Turn Automation ON"
    kill_label = "🛑 Kill Switch: ON (tap to release)" if kill_switch else "🛑 Kill Switch (emergency stop)"
    cap_row = [InlineKeyboardButton(text=("✅ " if cap == daily_cap_sol else "") + f"{cap} SOL/day", callback_data=f"rw:auto_set_cap:{cap}") for cap in AUTO_DAILY_CAP_PRESETS_SOL]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_label, callback_data="rw:auto_toggle")],
        cap_row[:3], cap_row[3:],
        [InlineKeyboardButton(text="🎯 Auto-Buy Settings", callback_data="rw:auto_filters")],
        [InlineKeyboardButton(text=kill_label, callback_data="rw:auto_kill_toggle")],
        [InlineKeyboardButton(text="⬅️ Wallet Menu", callback_data="rw:menu")],
    ])


def real_wallet_automation_filters_kb(signal_source: str = "both") -> InlineKeyboardMarkup:
    # "both" is kept selected (checkmarked) for the New + Redelivered
    # button -- it is the legacy alias for "new_redelivered" and every
    # existing row still defaults to it, so this preserves the exact same
    # button appearing checked as before First Milestone existed.
    _CHECKED_AS_NEW_REDELIVERED = {"both", "new_redelivered"}

    def _src(label: str, value: str) -> InlineKeyboardButton:
        is_checked = signal_source == value or (
            value == "new_redelivered" and signal_source in _CHECKED_AS_NEW_REDELIVERED
        )
        prefix = "✅ " if is_checked else ""
        return InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"rw:auto_set_signal_source:{value}")

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Auto-buy amount (USDT)", callback_data="rw:auto_filter_edit:auto_buy_amount_usdt")],
        [InlineKeyboardButton(text="🎯 Take Profit %", callback_data="rw:auto_filter_edit:take_profit_pct"), InlineKeyboardButton(text="🛑 Stop Loss %", callback_data="rw:auto_filter_edit:stop_loss_pct")],
        [InlineKeyboardButton(text="🔢 Daily auto-buy limit (1–20)", callback_data="rw:auto_filter_edit:daily_auto_buy_limit")],
        [InlineKeyboardButton(text="📊 Min conviction score", callback_data="rw:auto_filter_edit:min_score")],
        [InlineKeyboardButton(text="🏦 Min market cap", callback_data="rw:auto_filter_edit:min_market_cap")],
        [InlineKeyboardButton(text="🏦 Max market cap", callback_data="rw:auto_filter_edit:max_market_cap")],
        [InlineKeyboardButton(text="💧 Min liquidity (USD)", callback_data="rw:auto_filter_edit:min_liquidity_usd")],
        [InlineKeyboardButton(text="📦 Max bundle %", callback_data="rw:auto_filter_edit:max_bundle_pct")],
        [InlineKeyboardButton(text="👤 Max dev holding %", callback_data="rw:auto_filter_edit:max_dev_holding_pct")],
        [_src("🆕 New only", "new"), _src("🔁 Redelivered only", "redelivered")],
        [_src("⚡ First Milestone only", "first_milestone")],
        [_src("🔀 New + Redelivered", "new_redelivered")],
        [_src("🔀 New + First Milestone", "new_first_milestone")],
        [_src("🔀 Redelivered + First Milestone", "redelivered_first_milestone")],
        [_src("🔀 New + Redelivered + First Milestone", "new_redelivered_first_milestone")],
        [InlineKeyboardButton(text="🧹 Clear all filters", callback_data="rw:auto_filters_clear")],
        [InlineKeyboardButton(text="⬅️ Automation", callback_data="rw:automation")],
    ])


def real_wallet_dca_list_kb(schedules: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{s.symbol or s.contract[:6]} — {s.orders_filled}/{s.total_orders} ({s.status})", callback_data=f"rw:dca_view:{s.id}")] for s in schedules]
    rows.append([InlineKeyboardButton(text="➕ New DCA Schedule", callback_data="rw:dca_new")])
    rows.append([InlineKeyboardButton(text="⬅️ Wallet Menu", callback_data="rw:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def real_wallet_dca_detail_kb(schedule_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status == "active": rows.append([InlineKeyboardButton(text="⏸️ Pause", callback_data=f"rw:dca_pause:{schedule_id}")])
    elif status == "paused": rows.append([InlineKeyboardButton(text="▶️ Resume", callback_data=f"rw:dca_resume:{schedule_id}")])
    if status in ("active", "paused"): rows.append([InlineKeyboardButton(text="❌ Cancel Schedule", callback_data=f"rw:dca_cancel:{schedule_id}")])
    rows.append([InlineKeyboardButton(text="🔁 Refresh", callback_data=f"rw:dca_view:{schedule_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ DCA Schedules", callback_data="rw:dca_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def real_wallet_dca_cancel_confirm_kb(schedule_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Yes, cancel it", callback_data=f"rw:dca_cancel_confirm:{schedule_id}"), InlineKeyboardButton(text="❌ No, keep it", callback_data=f"rw:dca_view:{schedule_id}")]])


def real_wallet_dca_skip_optional_kb(step: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭️ Skip — no limit", callback_data=f"rw:dca_new_skip:{step}")], [InlineKeyboardButton(text="❌ Cancel setup", callback_data="rw:menu")]])


def real_wallet_settings_kb(slippage_bps: int, priority_tier: str) -> InlineKeyboardMarkup:
    slippage_row = [InlineKeyboardButton(text=("✅ " if bps == slippage_bps else "") + label, callback_data=f"rw:set_slippage:{bps}") for bps, label in SLIPPAGE_LABELS_BPS.items()]
    priority_row = [InlineKeyboardButton(text=("✅ " if tier == priority_tier else "") + label, callback_data=f"rw:set_priority:{tier}") for tier, label in PRIORITY_LABELS.items()]
    return InlineKeyboardMarkup(inline_keyboard=[slippage_row[:3], slippage_row[3:], priority_row, [InlineKeyboardButton(text="⬅️ Wallet Menu", callback_data="rw:menu")]])