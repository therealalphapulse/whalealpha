from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def plans_kb(plans: list) -> InlineKeyboardMarkup:
    rows = []
    for p in plans:
        duration = f"{p.duration_days}d" if p.duration_days else "Lifetime"
        rows.append([InlineKeyboardButton(
            text=f"{p.name} — ${p.price_usd:.2f} ({duration})",
            callback_data=f"premium:plan:{p.key}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="premium:refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def methods_kb(plan_key: str, methods: list) -> InlineKeyboardMarkup:
    rows = []
    for m in methods:
        icon = "🪙" if m.method_type == "crypto" else "🏦"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {m.label}",
            callback_data=f"premium:method:{plan_key}:{m.key}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="premium:upgrade_info")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def crypto_payment_kb(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ I've Paid — Submit Transaction", callback_data=f"premium:submit_tx:{payment_id}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="premium:refresh")],
    ])


def manual_payment_kb(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Submit Payment Proof", callback_data=f"premium:submit_proof:{payment_id}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="premium:refresh")],
    ])


def admin_review_kb(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"premium:approve:{payment_id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"premium:reject:{payment_id}"),
        ],
    ])
