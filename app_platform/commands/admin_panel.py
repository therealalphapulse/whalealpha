"""
In-bot Admin Panel — Owner-only administrator management (Module 2).

/admin           - panel: list admins, activity log (viewable by any active admin)
/addadmin        - add an administrator. Three ways, matching the spec:
                     1. Reply to /addadmin to a message FROM the target user
                        (message.reply_to_message.from_user)
                     2. Forward a message FROM the target user, with the
                        /addadmin caption on the forwarded message
                     3. Plain /addadmin with no reply/forward -> bot asks
                        for the Telegram ID as plain text
/removeadmin <id> - remove an administrator (confirmation required)

All mutating actions (add/remove/change role) are Owner-only
(manage_admins is an owner-only permission — see services/admin_rbac.py).
Any active administrator can view the panel and admin list.
"""

import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config.settings import OWNER_ID, ADMIN_IDS
from domain.admin import admin_rbac
from domain.payments import premium_plans
from domain.payments import payment_methods
from domain.admin.admin_rbac import ROLES, ROLE_LABELS

logger = logging.getLogger("AlphaPulse.AdminPanel")
router = Router()


class AdminPanelStates(StatesGroup):
    waiting_admin_id = State()


def _is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in ADMIN_IDS


def _role_picker_kb(prefix: str, target_id: int) -> InlineKeyboardMarkup:
    assignable = [r for r in ROLES if r != "owner"]
    rows = [[InlineKeyboardButton(text=ROLE_LABELS[r], callback_data=f"{prefix}:{target_id}:{r}")] for r in assignable]
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 View Admins", callback_data="adm:list")],
        [InlineKeyboardButton(text="📜 Activity Log", callback_data="adm:activity")],
    ])


def _admin_row_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Change Role", callback_data=f"adm:changerole:{user_id}"),
            InlineKeyboardButton(text="🗑️ Remove", callback_data=f"adm:remove_confirm:{user_id}"),
        ],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="adm:list")],
    ])


@router.message(Command("admin"))
async def cmd_admin_panel(message: Message):
    if not await admin_rbac.is_admin(message.from_user.id):
        return  # silent — don't reveal admin surface to non-admins
    await message.answer("🛠️ <b>AlphaPulse Admin Panel</b>", reply_markup=_panel_kb())


@router.callback_query(F.data == "adm:menu")
async def cb_admin_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await admin_rbac.is_admin(callback.from_user.id):
        await callback.answer("Not authorized.", show_alert=True)
        return
    await callback.message.edit_text("🛠️ <b>AlphaPulse Admin Panel</b>", reply_markup=_panel_kb())
    await callback.answer()


@router.callback_query(F.data == "adm:list")
async def cb_admin_list(callback: CallbackQuery):
    if not await admin_rbac.has_permission(callback.from_user.id, "view_admins") and not _is_owner(callback.from_user.id):
        await callback.answer("Not authorized.", show_alert=True)
        return

    admins = await admin_rbac.list_admins()
    lines = ["👥 <b>Administrators</b>\n"]
    rows = []
    if OWNER_ID:
        lines.append(f"👑 Owner — <code>{OWNER_ID}</code>")
    for a in admins:
        if a.user_id == OWNER_ID:
            continue
        status = "" if a.is_active else " (inactive)"
        lines.append(f"{ROLE_LABELS.get(a.role, a.role)} — <code>{a.user_id}</code>{status}")
        if a.is_active and _is_owner(callback.from_user.id):
            rows.append([InlineKeyboardButton(
                text=f"{ROLE_LABELS.get(a.role, a.role)} {a.user_id}",
                callback_data=f"adm:manage:{a.user_id}",
            )])

    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="adm:menu")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:manage:"))
async def cb_admin_manage(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    target_id = int(callback.data.split(":")[-1])
    await callback.message.edit_text(f"Managing administrator <code>{target_id}</code>", reply_markup=_admin_row_kb(target_id))
    await callback.answer()


@router.callback_query(F.data == "adm:activity")
async def cb_admin_activity(callback: CallbackQuery):
    if not await admin_rbac.has_permission(callback.from_user.id, "view_admins") and not _is_owner(callback.from_user.id):
        await callback.answer("Not authorized.", show_alert=True)
        return
    logs = await admin_rbac.get_recent_activity(20)
    if not logs:
        text = "📜 <b>Activity Log</b>\n\nNo administrator actions recorded yet."
    else:
        lines = ["📜 <b>Activity Log</b> (most recent 20)\n"]
        for l in logs:
            when = l.created_at.strftime("%Y-%m-%d %H:%M UTC") if l.created_at else "?"
            target = f" -> {l.target_user_id}" if l.target_user_id else ""
            detail = f" ({l.detail})" if l.detail else ""
            lines.append(f"• {when} — admin {l.admin_user_id}: {l.action}{target}{detail}")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back", callback_data="adm:menu")]
    ]))
    await callback.answer()


# ---------------------------------------------------------------------------
# Add admin — reply / forward / manual ID (Owner only)
# ---------------------------------------------------------------------------

@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return  # silent

    target_user = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
    elif message.forward_from:
        target_user = message.forward_from

    if target_user:
        if target_user.is_bot:
            await message.answer("❌ Can't make a bot an administrator.")
            return
        await state.update_data(pending_admin_id=target_user.id, pending_admin_username=target_user.username)
        await message.answer(
            f"Add <b>{target_user.full_name}</b> (<code>{target_user.id}</code>) as an administrator.\n\nSelect their role:",
            reply_markup=_role_picker_kb("adm:setrole_new", target_user.id),
        )
        return

    await state.set_state(AdminPanelStates.waiting_admin_id)
    await message.answer(
        "Send the Telegram ID of the user to add as an administrator.\n\n"
        "(Or reply to /addadmin on one of their messages, or forward one of "
        "their messages with /addadmin as the caption, to skip this step.)\n\n"
        "Send /cancel to back out."
    )


@router.message(AdminPanelStates.waiting_admin_id)
async def on_admin_id_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return
    if not raw.isdigit():
        await message.answer("❌ That doesn't look like a Telegram ID. Send a numeric ID, or /cancel.")
        return

    target_id = int(raw)
    await state.update_data(pending_admin_id=target_id, pending_admin_username=None)
    await state.clear()  # role pick below doesn't need FSM, it's carried in callback_data
    await message.answer(
        f"Add <code>{target_id}</code> as an administrator.\n\nSelect their role:",
        reply_markup=_role_picker_kb("adm:setrole_new", target_id),
    )


@router.callback_query(F.data.startswith("adm:setrole_new:"))
async def cb_set_role_new(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    _, _, target_id, role = callback.data.split(":")
    target_id = int(target_id)

    ok, msg = await admin_rbac.add_admin(target_id, role, username=None, added_by=callback.from_user.id)
    await callback.message.edit_text(("✅ " if ok else "❌ ") + msg)
    await callback.answer()


# ---------------------------------------------------------------------------
# Remove admin (Owner only, confirmation required)
# ---------------------------------------------------------------------------

@router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message):
    if not _is_owner(message.from_user.id):
        return  # silent

    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Usage: <code>/removeadmin &lt;telegram_id&gt;</code>")
        return

    target_id = int(parts[1])
    await message.answer(
        f"Remove administrator <code>{target_id}</code>? This cannot be undone from here — they'd need to be re-added.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm Remove", callback_data=f"adm:remove:{target_id}"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="adm:menu"),
            ]
        ]),
    )


@router.callback_query(F.data.startswith("adm:remove_confirm:"))
async def cb_remove_confirm(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    target_id = int(callback.data.split(":")[-1])
    await callback.message.edit_text(
        f"Remove administrator <code>{target_id}</code>? This cannot be undone from here — they'd need to be re-added.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm Remove", callback_data=f"adm:remove:{target_id}"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="adm:menu"),
            ]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:remove:"))
async def cb_remove_execute(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    target_id = int(callback.data.split(":")[-1])
    ok, msg = await admin_rbac.remove_admin(target_id, removed_by=callback.from_user.id)
    await callback.message.edit_text(("✅ " if ok else "❌ ") + msg)
    await callback.answer()


# ---------------------------------------------------------------------------
# Change role (Owner only)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("adm:changerole:"))
async def cb_change_role_menu(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    target_id = int(callback.data.split(":")[-1])
    await callback.message.edit_text(
        f"Select new role for <code>{target_id}</code>:",
        reply_markup=_role_picker_kb("adm:setrole", target_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:setrole:"))
async def cb_change_role_execute(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    _, _, target_id, role = callback.data.split(":")
    target_id = int(target_id)
    ok, msg = await admin_rbac.change_role(target_id, role, changed_by=callback.from_user.id)
    await callback.message.edit_text(("✅ " if ok else "❌ ") + msg)
    await callback.answer()


# ---------------------------------------------------------------------------
# Subscription plan config (Owner only — configure_subscription_plans)
# ---------------------------------------------------------------------------

@router.message(Command("plans"))
async def cmd_list_plans(message: Message):
    if not await admin_rbac.is_admin(message.from_user.id):
        return
    plans = await premium_plans.get_all_plans()
    if not plans:
        await message.answer("No plans configured yet.")
        return
    lines = ["💎 <b>Premium Plans</b>\n"]
    for p in plans:
        duration = f"{p.duration_days}d" if p.duration_days else "Lifetime"
        status = "🟢" if p.is_active else "⚪"
        prices = f"${p.price_usd}"
        if p.price_sol:
            prices += f" / {p.price_sol} SOL"
        if p.price_usdc:
            prices += f" / {p.price_usdc} USDC"
        if p.price_usdt:
            prices += f" / {p.price_usdt} USDT"
        lines.append(f"{status} <b>{p.key}</b> — {p.name} ({duration}) — {prices}")
    await message.answer("\n".join(lines))


@router.message(Command("addplan"))
async def cmd_add_plan(message: Message):
    """/addplan <key> <name> <duration_days|lifetime> <price_usd>"""
    if not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=4)
    if len(parts) < 5:
        await message.answer("Usage: <code>/addplan &lt;key&gt; &lt;name&gt; &lt;duration_days|lifetime&gt; &lt;price_usd&gt;</code>")
        return
    _, key, name, duration_raw, price_raw = parts
    duration_days = None if duration_raw.lower() == "lifetime" else int(duration_raw)
    try:
        price_usd = float(price_raw)
    except ValueError:
        await message.answer("⚠️ Invalid price.")
        return

    ok, msg = await premium_plans.create_plan(key, name, duration_days, price_usd)
    if ok:
        await admin_rbac.log_action(message.from_user.id, "create_plan", detail=key)
    await message.answer(("✅ " if ok else "❌ ") + msg)


@router.message(Command("setplanprice"))
async def cmd_set_plan_price(message: Message):
    """/setplanprice <key> <usd|sol|usdc|usdt> <value|none>"""
    if not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 4:
        await message.answer("Usage: <code>/setplanprice &lt;key&gt; &lt;usd|sol|usdc|usdt&gt; &lt;value|none&gt;</code>")
        return
    _, key, coin, value_raw = parts
    field = {"usd": "price_usd", "sol": "price_sol", "usdc": "price_usdc", "usdt": "price_usdt"}.get(coin.lower())
    if not field:
        await message.answer("⚠️ Coin must be usd, sol, usdc, or usdt.")
        return
    value = None if value_raw.lower() == "none" else float(value_raw)
    ok = await premium_plans.set_plan_price(key, field, value)
    if ok:
        await admin_rbac.log_action(message.from_user.id, "set_plan_price", detail=f"{key}.{field}={value}")
    await message.answer("✅ Updated." if ok else "❌ Plan not found.")


@router.message(Command("toggleplan"))
async def cmd_toggle_plan(message: Message):
    """/toggleplan <key> <on|off>"""
    if not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or parts[2].lower() not in ("on", "off"):
        await message.answer("Usage: <code>/toggleplan &lt;key&gt; &lt;on|off&gt;</code>")
        return
    ok = await premium_plans.set_plan_active(parts[1], parts[2].lower() == "on")
    if ok:
        await admin_rbac.log_action(message.from_user.id, "toggle_plan", detail=f"{parts[1]}={parts[2]}")
    await message.answer("✅ Updated." if ok else "❌ Plan not found.")


# ---------------------------------------------------------------------------
# Payment method config (Owner only — configure_payment_methods)
# ---------------------------------------------------------------------------

@router.message(Command("paymentmethods"))
async def cmd_list_payment_methods(message: Message):
    if not await admin_rbac.is_admin(message.from_user.id):
        return
    methods = await payment_methods.get_all_methods()
    if not methods:
        await message.answer("No payment methods configured yet.")
        return
    lines = ["💳 <b>Payment Methods</b>\n"]
    for m in methods:
        status = "🟢" if m.is_active else "⚪"
        if m.method_type == "crypto":
            lines.append(f"{status} <b>{m.key}</b> — {m.label} ({m.asset}) — <code>{m.receive_address}</code>")
        else:
            lines.append(f"{status} <b>{m.key}</b> — {m.label} (manual)")
    await message.answer("\n".join(lines))


@router.message(Command("addcryptomethod"))
async def cmd_add_crypto_method(message: Message):
    """/addcryptomethod <key> <label> <SOL|USDC|USDT> <receive_address>"""
    if not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=4)
    if len(parts) < 5:
        await message.answer("Usage: <code>/addcryptomethod &lt;key&gt; &lt;label&gt; &lt;SOL|USDC|USDT&gt; &lt;receive_address&gt;</code>")
        return
    _, key, label, asset, address = parts
    ok, msg = await payment_methods.create_crypto_method(key, label, asset.upper(), address.strip())
    if ok:
        await admin_rbac.log_action(message.from_user.id, "add_crypto_method", detail=key)
    await message.answer(("✅ " if ok else "❌ ") + msg)


@router.message(Command("addmanualmethod"))
async def cmd_add_manual_method(message: Message):
    """/addmanualmethod <key> <label> | <instructions>"""
    if not _is_owner(message.from_user.id):
        return
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2 or "|" not in raw[1]:
        await message.answer("Usage: <code>/addmanualmethod &lt;key&gt; &lt;label&gt; | &lt;instructions&gt;</code>")
        return
    rest = raw[1]
    left, instructions = rest.split("|", 1)
    left_parts = left.strip().split(maxsplit=1)
    if len(left_parts) < 2:
        await message.answer("Usage: <code>/addmanualmethod &lt;key&gt; &lt;label&gt; | &lt;instructions&gt;</code>")
        return
    key, label = left_parts[0], left_parts[1]
    ok, msg = await payment_methods.create_manual_method(key.strip(), label.strip(), instructions.strip())
    if ok:
        await admin_rbac.log_action(message.from_user.id, "add_manual_method", detail=key)
    await message.answer(("✅ " if ok else "❌ ") + msg)


@router.message(Command("togglemethod"))
async def cmd_toggle_method(message: Message):
    """/togglemethod <key> <on|off>"""
    if not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or parts[2].lower() not in ("on", "off"):
        await message.answer("Usage: <code>/togglemethod &lt;key&gt; &lt;on|off&gt;</code>")
        return
    ok = await payment_methods.set_method_active(parts[1], parts[2].lower() == "on")
    if ok:
        await admin_rbac.log_action(message.from_user.id, "toggle_method", detail=f"{parts[1]}={parts[2]}")
    await message.answer("✅ Updated." if ok else "❌ Payment method not found.")
