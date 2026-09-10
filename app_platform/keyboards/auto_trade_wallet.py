"""app_platform/keyboards/auto_trade_wallet.py

Inline keyboard for the new /wallet (Auto-Trade Engine) command. Mirrors
the button/callback-data conventions already used by
app_platform/keyboards/real_wallet.py (e.g. "rw:auto_toggle",
"rw:auto_filter_edit:<field>") but under its own "atw:" callback-data
namespace so it can never collide with the existing /realwallet router.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def auto_trade_wallet_menu_kb(policy) -> InlineKeyboardMarkup:
    toggle_label = "\U0001F7E2 Auto-Trade: ON (tap to turn off)" if policy.auto_trade_enabled else "\u26AA Auto-Trade: OFF (tap to turn on)"
    kill_label = "\U0001F6D1 Kill Switch: ON (tap to release)" if policy.kill_switch else "\U0001F6D1 Kill Switch (emergency stop)"
    trailing_label = "\U0001F4C9 Trailing Stop: ON" if policy.trailing_stop_enabled else "\U0001F4C9 Trailing Stop: OFF"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_label, callback_data="atw:toggle")],
        [InlineKeyboardButton(text="\U0001F4B5 Buy Amount", callback_data="atw:edit:buy_amount_sol"),
         InlineKeyboardButton(text="\U0001F522 Daily Limit", callback_data="atw:edit:daily_trade_limit")],
        [InlineKeyboardButton(text="\U0001F3AF Take-Profit %", callback_data="atw:edit:take_profit_pct"),
         InlineKeyboardButton(text="\U0001F6E1\uFE0F Stop-Loss %", callback_data="atw:edit:stop_loss_pct")],
        [InlineKeyboardButton(text=trailing_label, callback_data="atw:trailing_toggle"),
         InlineKeyboardButton(text="\u270F\uFE0F Trailing %", callback_data="atw:edit:trailing_stop_pct")],
        [InlineKeyboardButton(text="\U0001F680 Trailing Activation %", callback_data="atw:edit:trailing_activation_pct"),
         InlineKeyboardButton(text="\U0001F4C9 Trailing Retracement %", callback_data="atw:edit:trailing_retracement_pct")],
        [InlineKeyboardButton(text="\U0001F4E6 Max Open Positions", callback_data="atw:edit:max_open_positions")],
        [InlineKeyboardButton(text=kill_label, callback_data="atw:kill_toggle")],
        [InlineKeyboardButton(text="\U0001F501 Refresh", callback_data="atw:refresh")],
    ])
