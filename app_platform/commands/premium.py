import logging

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config.settings import ADMIN_IDS
from domain.admin import admin_rbac
from domain.payments import premium_plans
from domain.payments import payment_methods
from domain.payments import premium_payments
from app_platform.keyboards.premium_payment import (
    plans_kb, methods_kb, crypto_payment_kb, manual_payment_kb, admin_review_kb,
)
from domain.payments.premium_service import (
    get_status,
    activate_premium,
    renew_premium,
    extend_premium,
    revoke_premium,
    list_premium_users,
    set_signal_alerts,
    get_signal_alerts_enabled,
    get_premium_engine_stats,
    get_recent_premium_signals,
    trigger_manual_discovery_cycle,
    is_premium,
    PREMIUM_BENEFITS,
    PREMIUM_REQUIRED_MESSAGE,
    PREMIUM_TRADING_SUITE,
    PREMIUM_INTELLIGENCE_FEATURES,
    format_premium_header,
    format_premium_badge,
)

router = Router()
logger = logging.getLogger("AlphaPulse.PremiumCmd")


class PremiumPaymentStates(StatesGroup):
    waiting_tx_signature = State()
    waiting_manual_proof = State()


async def _is_admin(user_id: int) -> bool:
    """Legacy name kept for minimal call-site churn. Now backed by RBAC
    (Owner + legacy ADMIN_IDS always pass; premium_manager/super_admin
    pass the specific permission checks used below at each call site)."""
    return await admin_rbac.is_admin(user_id)


async def _require_permission(message: Message, permission: str) -> bool:
    """Returns True and does nothing if authorized; otherwise replies
    with an admin-only notice and returns False."""
    if await admin_rbac.has_permission(message.from_user.id, permission):
        return True
    await message.answer("⛔ You don't have permission to do that.")
    return False


def _format_remaining(delta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "< 1 minute"
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if not days and minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "< 1 minute"


def _benefits_block() -> str:
    return "\n".join(f"• {b}" for b in PREMIUM_BENEFITS)


def _trading_suite_block() -> str:
    return "\n".join(f"• {b}" for b in PREMIUM_TRADING_SUITE)


def _intelligence_block() -> str:
    return "\n".join(f"• {b}" for b in PREMIUM_INTELLIGENCE_FEATURES)


async def build_premium_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    status = await get_status(user_id)
    state = status["state"]

    if state == "active":
        remaining = _format_remaining(status["remaining"])
        expires = status["expires_at"]
        header = (
            "💎 <b>Premium Status: ACTIVE</b> ✅\n\n"
            f"⏳ Time remaining: <b>{remaining}</b>\n"
            f"📅 Expires: <b>{expires.strftime('%Y-%m-%d %H:%M UTC')}</b>"
        )
    elif state == "active_lifetime":
        header = "💎 <b>Premium Status: ACTIVE</b> ✅\n\n♾️ Lifetime membership — never expires."
    elif state == "revoked":
        header = "💎 <b>Premium Status: REVOKED</b> ❌\n\nContact support if you believe this is a mistake."
    elif state == "expired":
        header = "💎 <b>Premium Status: EXPIRED</b> ⏸️\n\nRenew to regain your Premium benefits."
    else:
        header = "💎 <b>Premium Status: Not active</b>\n\nYou're currently on the free tier."

    active = state in ("active", "active_lifetime")

    text = (
        f"{header}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <b>Premium Benefits</b>\n"
        f"{_benefits_block()}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🛠️ <b>Premium Trading Suite</b>\n"
        f"{_trading_suite_block()}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🧠 <b>Premium Intelligence</b>\n"
        f"{_intelligence_block()}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💳 <b>Subscription Management</b>\n"
        + ("Manage or renew your plan any time from this screen.\n\n" if active else
           "Payment integration is built-in — pick a plan below to activate instantly.\n\n") +
        "📊 AlphaPulse Premium"
    )

    trading_suite_row = (
        [InlineKeyboardButton(text="🛠️ Open Trading Suite (Automation)", callback_data="rw:automation")]
        if active else
        [InlineKeyboardButton(text="🛠️ Preview Trading Suite", callback_data="premium:trading_suite_preview")]
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        trading_suite_row,
        [InlineKeyboardButton(text="👛 Smart Wallet Leaderboard", callback_data="premium:wallets")],
        [InlineKeyboardButton(text="💎 Recent Premium Signals", callback_data="premium:signals")],
        [InlineKeyboardButton(text="💳 Upgrade to Premium" if not active else "💳 Manage Subscription", callback_data="premium:upgrade_info")],
        [InlineKeyboardButton(text="🔄 Refresh Status", callback_data="premium:refresh")],
    ])

    return text, keyboard


@router.message(Command("premium"))
async def cmd_premium(message: Message):
    try:
        text, keyboard = await build_premium_view(message.from_user.id)
        await message.answer(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"/premium error: {e}")
        await message.answer("⚠️ Could not load Premium status right now.")


@router.callback_query(F.data == "premium:refresh")
async def cb_premium_refresh(callback: CallbackQuery):
    try:
        text, keyboard = await build_premium_view(callback.from_user.id)
        await callback.message.answer(text, reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"premium refresh error: {e}")
        await callback.answer("Could not refresh status.", show_alert=True)


@router.callback_query(F.data == "premium:trading_suite_preview")
async def cb_trading_suite_preview(callback: CallbackQuery):
    """Free-user preview of what unlocking the Trading Suite gets them —
    part of the Upgrade Experience: explain, show benefits, offer to
    upgrade, without touching their normal (Free) trading flow at all."""
    text = (
        "🛠️ <b>Premium Trading Suite</b>\n\n"
        f"{_trading_suite_block()}\n\n"
        "Your Free trading — wallet, manual buy/sell, basic DCA, portfolio, "
        "trade history — keeps working exactly as it does today. Premium "
        "just adds automation and advanced tooling on top of it."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Upgrade to Premium", callback_data="premium:upgrade_info")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="premium:refresh")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "premium:upgrade_info")
async def cb_premium_upgrade_info(callback: CallbackQuery):
    plans = await premium_plans.get_active_plans()
    if not plans:
        await callback.answer("No plans are available right now — check back soon.", show_alert=True)
        return
    await callback.message.edit_text(
        "💳 <b>Upgrade to Premium</b>\n\nChoose a plan:",
        reply_markup=plans_kb(plans),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("premium:plan:"))
async def cb_premium_pick_plan(callback: CallbackQuery):
    plan_key = callback.data.split(":")[-1]
    plan = await premium_plans.get_plan(plan_key)
    if not plan or not plan.is_active:
        await callback.answer("That plan is no longer available.", show_alert=True)
        return

    methods = await payment_methods.get_active_methods()
    if not methods:
        await callback.answer("No payment methods are configured yet — contact an admin.", show_alert=True)
        return

    duration = f"{plan.duration_days} days" if plan.duration_days else "Lifetime (never expires)"
    text = (
        f"💎 <b>{plan.name}</b>\n\n"
        f"Duration: {duration}\n"
        f"Price: ${plan.price_usd:.2f}\n\n"
        "Choose how you'd like to pay:"
    )
    await callback.message.edit_text(text, reply_markup=methods_kb(plan_key, methods))
    await callback.answer()


@router.callback_query(F.data.startswith("premium:method:"))
async def cb_premium_pick_method(callback: CallbackQuery):
    _, _, plan_key, method_key = callback.data.split(":")
    ok, msg, payment = await premium_payments.create_payment_request(callback.from_user.id, plan_key, method_key)
    if not ok:
        await callback.answer(msg, show_alert=True)
        return

    method = await payment_methods.get_method(method_key)
    if method.method_type == "crypto":
        text = (
            f"🪙 <b>Pay with {method.asset}</b>\n\n"
            f"Send exactly <b>{payment.expected_amount} {method.asset}</b> to:\n"
            f"<code>{method.receive_address}</code>\n\n"
            "Once sent, tap the button below and paste the transaction signature — "
            "Premium activates automatically the moment it's verified on-chain, no waiting on an admin.\n\n"
            f"Payment request #{payment.id}"
        )
        await callback.message.edit_text(text, reply_markup=crypto_payment_kb(payment.id))
    else:
        text = (
            f"🏦 <b>{method.label}</b>\n\n"
            f"{method.instructions}\n\n"
            f"Amount: <b>${payment.expected_amount:.2f}</b>\n\n"
            "Once paid, tap the button below and send your payment reference/receipt "
            "(text or a screenshot) — an admin will review and activate Premium.\n\n"
            f"Payment request #{payment.id}"
        )
        await callback.message.edit_text(text, reply_markup=manual_payment_kb(payment.id))
    await callback.answer()


@router.callback_query(F.data.startswith("premium:submit_tx:"))
async def cb_submit_tx_prompt(callback: CallbackQuery, state: FSMContext):
    payment_id = int(callback.data.split(":")[-1])
    await state.set_state(PremiumPaymentStates.waiting_tx_signature)
    await state.update_data(payment_id=payment_id)
    await callback.message.edit_text("Paste the transaction signature now.\n\nSend /cancel to back out.")
    await callback.answer()


@router.message(PremiumPaymentStates.waiting_tx_signature)
async def on_tx_signature_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    data = await state.get_data()
    payment_id = data.get("payment_id")
    await state.clear()
    if not payment_id:
        await message.answer("❌ Lost track of this payment request — start over with /premium.")
        return

    await message.answer("🔍 Verifying transaction on-chain...")
    ok, msg = await premium_payments.verify_crypto_payment(payment_id, raw)
    await message.answer(("✅ " if ok else "❌ ") + msg)


@router.callback_query(F.data.startswith("premium:submit_proof:"))
async def cb_submit_proof_prompt(callback: CallbackQuery, state: FSMContext):
    payment_id = int(callback.data.split(":")[-1])
    await state.set_state(PremiumPaymentStates.waiting_manual_proof)
    await state.update_data(payment_id=payment_id)
    await callback.message.edit_text(
        "Send your payment reference/receipt now — text, or a screenshot photo.\n\nSend /cancel to back out."
    )
    await callback.answer()


@router.message(PremiumPaymentStates.waiting_manual_proof)
async def on_manual_proof_message(message: Message, state: FSMContext, bot=None):
    if (message.text or "").strip().lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.")
        return

    data = await state.get_data()
    payment_id = data.get("payment_id")
    await state.clear()
    if not payment_id:
        await message.answer("❌ Lost track of this payment request — start over with /premium.")
        return

    proof_text = message.text or message.caption
    proof_file_id = message.photo[-1].file_id if message.photo else None

    ok, msg = await premium_payments.submit_manual_proof(payment_id, proof_text, proof_file_id)
    await message.answer(("✅ " if ok else "❌ ") + msg)
    if not ok:
        return

    payment = await premium_payments.get_payment(payment_id)
    admin_ids = await admin_rbac.get_admin_ids_with_permission("approve_premium")
    review_text = (
        "🔔 <b>Manual Payment Awaiting Review</b>\n\n"
        f"Payment #{payment.id} — user <code>{payment.user_id}</code>\n"
        f"Plan: {payment.plan_key} — ${payment.expected_amount:.2f}\n"
        f"Proof: {proof_text or '(photo only)'}"
    )
    for admin_id in admin_ids:
        try:
            if proof_file_id:
                await message.bot.send_photo(admin_id, proof_file_id, caption=review_text, reply_markup=admin_review_kb(payment.id))
            else:
                await message.bot.send_message(admin_id, review_text, reply_markup=admin_review_kb(payment.id))
        except Exception as e:
            logger.warning(f"Could not notify admin {admin_id} of pending payment {payment.id}: {e}")


@router.callback_query(F.data.startswith("premium:approve:"))
async def cb_admin_approve_payment(callback: CallbackQuery):
    if not await admin_rbac.has_permission(callback.from_user.id, "approve_premium"):
        await callback.answer("Not authorized.", show_alert=True)
        return
    payment_id = int(callback.data.split(":")[-1])
    ok, msg = await premium_payments.approve_manual_payment(payment_id, callback.from_user.id)
    await callback.message.edit_caption(caption=("✅ " if ok else "❌ ") + msg) if callback.message.photo else \
        await callback.message.edit_text(("✅ " if ok else "❌ ") + msg)
    if ok:
        payment = await premium_payments.get_payment(payment_id)
        try:
            await callback.bot.send_message(payment.user_id, "🎉 Your Premium payment was approved — you're all set!")
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data.startswith("premium:reject:"))
async def cb_admin_reject_payment(callback: CallbackQuery):
    if not await admin_rbac.has_permission(callback.from_user.id, "reject_premium"):
        await callback.answer("Not authorized.", show_alert=True)
        return
    payment_id = int(callback.data.split(":")[-1])
    ok, msg = await premium_payments.reject_manual_payment(payment_id, callback.from_user.id, reason="Rejected by admin")
    await callback.message.edit_caption(caption=("✅ " if ok else "❌ ") + msg) if callback.message.photo else \
        await callback.message.edit_text(("✅ " if ok else "❌ ") + msg)
    if ok:
        payment = await premium_payments.get_payment(payment_id)
        try:
            await callback.bot.send_message(payment.user_id, "❌ Your Premium payment submission was rejected. Contact support if you believe this is a mistake.")
        except Exception:
            pass
    await callback.answer()


@router.message(Command("premium_history"))
async def cmd_premium_history(message: Message):
    history = await premium_payments.get_user_payment_history(message.from_user.id)
    if not history:
        await message.answer("No payment history yet.")
        return
    lines = ["📜 <b>Your Payment History</b>\n"]
    for p in history:
        when = p.created_at.strftime("%Y-%m-%d %H:%M UTC") if p.created_at else "?"
        lines.append(f"• {when} — {p.plan_key} via {p.method_key}: <b>{p.status}</b>")
    await message.answer("\n".join(lines))


@router.message(Command("premium_payments_queue"))
async def cmd_premium_payments_queue(message: Message):
    if not await admin_rbac.has_permission(message.from_user.id, "view_payment_requests"):
        await message.answer("⛔ Not authorized.")
        return
    pending = await premium_payments.get_pending_manual_reviews()
    if not pending:
        await message.answer("No manual payments awaiting review.")
        return
    for p in pending:
        text = (
            f"Payment #{p.id} — user <code>{p.user_id}</code>\n"
            f"Plan: {p.plan_key} — ${p.expected_amount:.2f}\n"
            f"Proof: {p.proof_text or '(photo only)'}"
        )
        if p.proof_file_id:
            await message.answer_photo(p.proof_file_id, caption=text, reply_markup=admin_review_kb(p.id))
        else:
            await message.answer(text, reply_markup=admin_review_kb(p.id))


# ============================================================
# Admin management (ADMIN_IDS only — see config/settings.py)
# ============================================================

def _parse_user_id(raw: str) -> int | None:
    try:
        return int(raw.strip())
    except (ValueError, TypeError):
        return None


@router.message(Command("premium_grant"))
async def cmd_premium_grant(message: Message):
    """/premium_grant <user_id> [days] — days omitted = lifetime."""
    if not await _require_permission(message, "approve_premium"):
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: <code>/premium_grant &lt;user_id&gt; [days]</code>")
        return

    target_id = _parse_user_id(parts[1])
    if target_id is None:
        await message.answer("⚠️ Invalid user_id.")
        return

    days = None
    if len(parts) >= 3:
        try:
            days = int(parts[2])
        except ValueError:
            await message.answer("⚠️ Invalid days value.")
            return

    try:
        await activate_premium(target_id, duration_days=days, granted_by=str(message.from_user.id))
        duration_label = f"{days} day(s)" if days else "lifetime"
        await admin_rbac.log_action(message.from_user.id, "approve_premium", target_user_id=target_id, detail=duration_label)
        await message.answer(f"✅ Premium granted to <code>{target_id}</code> ({duration_label}).")
    except Exception as e:
        logger.error(f"premium_grant error: {e}")
        await message.answer("⚠️ Could not grant Premium.")


@router.message(Command("premium_renew"))
async def cmd_premium_renew(message: Message):
    """/premium_renew <user_id> <days>"""
    if not await _require_permission(message, "extend_premium"):
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Usage: <code>/premium_renew &lt;user_id&gt; &lt;days&gt;</code>")
        return

    target_id = _parse_user_id(parts[1])
    try:
        days = int(parts[2])
    except ValueError:
        target_id = None
        days = None

    if target_id is None or not days:
        await message.answer("⚠️ Invalid user_id or days.")
        return

    try:
        membership = await renew_premium(target_id, days, granted_by=str(message.from_user.id))
        expires = membership.expires_at.strftime("%Y-%m-%d %H:%M UTC") if membership.expires_at else "lifetime"
        await admin_rbac.log_action(message.from_user.id, "renew_premium", target_user_id=target_id, detail=f"expires={expires}")
        await message.answer(f"✅ Premium renewed for <code>{target_id}</code>. New expiry: {expires}")
    except Exception as e:
        logger.error(f"premium_renew error: {e}")
        await message.answer("⚠️ Could not renew Premium.")


@router.message(Command("premium_extend"))
async def cmd_premium_extend(message: Message):
    """/premium_extend <user_id> <days>"""
    if not await _require_permission(message, "extend_premium"):
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Usage: <code>/premium_extend &lt;user_id&gt; &lt;days&gt;</code>")
        return

    target_id = _parse_user_id(parts[1])
    try:
        days = int(parts[2])
    except ValueError:
        target_id = None
        days = None

    if target_id is None or not days:
        await message.answer("⚠️ Invalid user_id or days.")
        return

    try:
        membership = await extend_premium(target_id, days, granted_by=str(message.from_user.id))
        expires = membership.expires_at.strftime("%Y-%m-%d %H:%M UTC") if membership.expires_at else "lifetime"
        await admin_rbac.log_action(message.from_user.id, "extend_premium", target_user_id=target_id, detail=f"+{days}d, expires={expires}")
        await message.answer(f"✅ Premium extended for <code>{target_id}</code>. New expiry: {expires}")
    except Exception as e:
        logger.error(f"premium_extend error: {e}")
        await message.answer("⚠️ Could not extend Premium.")


@router.message(Command("premium_revoke"))
async def cmd_premium_revoke(message: Message):
    """/premium_revoke <user_id>"""
    if not await _require_permission(message, "cancel_premium"):
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: <code>/premium_revoke &lt;user_id&gt;</code>")
        return

    target_id = _parse_user_id(parts[1])
    if target_id is None:
        await message.answer("⚠️ Invalid user_id.")
        return

    try:
        membership = await revoke_premium(target_id, revoked_by=str(message.from_user.id))
        if not membership:
            await message.answer("⚠️ That user has no Premium record.")
            return
        await admin_rbac.log_action(message.from_user.id, "revoke_premium", target_user_id=target_id)
        await message.answer(f"✅ Premium revoked for <code>{target_id}</code>.")
    except Exception as e:
        logger.error(f"premium_revoke error: {e}")
        await message.answer("⚠️ Could not revoke Premium.")


@router.message(Command("premium_list"))
async def cmd_premium_list(message: Message):
    if not await _require_permission(message, "view_premium_users"):
        return

    try:
        active = await list_premium_users(status="active", limit=50)
        if not active:
            await message.answer("📭 No active Premium users.")
            return

        lines = ["💎 <b>Active Premium Users</b>", "━━━━━━━━━━━━━━━━━━━━━"]
        for m in active:
            expires = m.expires_at.strftime("%Y-%m-%d") if m.expires_at else "lifetime"
            lines.append(f"• <code>{m.user_id}</code> — expires: {expires}")

        await message.answer("\n".join(lines))
    except Exception as e:
        logger.error(f"premium_list error: {e}")
        await message.answer("⚠️ Could not load Premium user list.")


# ============================================================
# Premium Intelligence Engine — Smart Wallet Intelligence status,
# Premium Signals feed, engine stats, and signal-alert opt-in.
#
# Internal Security requirement: the Smart Wallet database (addresses,
# rankings, reputation scores, tiers, classifications) is confidential.
# Neither Free nor Premium users may view or query it — only the final
# Premium signals it helps produce. This view intentionally exposes
# nothing beyond a coarse, non-identifying database-size indicator.
# ============================================================

async def build_wallets_view() -> str:
    try:
        stats = await get_premium_engine_stats()
    except Exception as e:
        logger.error(f"premium wallets view error: {e}")
        return "⚠️ Could not load Smart Wallet Intelligence status right now."

    total = stats.get("total_wallets", 0)

    lines = [
        "🧠 <b>Smart Wallet Intelligence</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📊 Wallets under active monitoring: <b>{total}</b>",
        "",
        "Our engine continuously discovers, validates, scores, and retires "
        "elite Solana wallets to power Premium signal consensus. "
        "Individual wallets, rankings, and scores are kept confidential — "
        "you receive only the resulting Premium Signals.",
        "",
        "⚡ Fully autonomous — discovered, scored, and pruned continuously.",
    ]
    return "\n".join(lines)


async def build_signals_view(user_id: int) -> str:
    if not await is_premium(user_id):
        return PREMIUM_REQUIRED_MESSAGE

    try:
        signals = await get_recent_premium_signals(limit=10)
    except Exception as e:
        logger.error(f"premium signals view error: {e}")
        return "⚠️ Could not load Premium Signals right now."

    if not signals:
        return (
            "💎 <b>Premium Signals</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📭 No Premium Signals yet — these only fire when a token clears "
            "BOTH the AI conviction gate and Smart Wallet consensus, so they're rare "
            "by design. You'll be notified the moment one appears."
        )

    lines = ["💎 <b>Recent Premium Signals</b>", "━━━━━━━━━━━━━━━━━━━━━", ""]
    for s in signals:
        lines.append(
            f"🪙 <b>{s.token_symbol or '?'}</b> — confidence <b>{s.confidence_score:.0f}</b>/100\n"
            f"   🧠 AI {s.ai_score:.0f} • 🤝 {s.consensus_wallet_count} smart wallets\n"
            f"   🕒 {s.signaled_at.strftime('%Y-%m-%d %H:%M UTC') if s.signaled_at else 'N/A'}\n"
        )
    return "\n".join(lines)


@router.message(Command("premium_wallets"))
async def cmd_premium_wallets(message: Message):
    try:
        await message.answer(await build_wallets_view())
    except Exception as e:
        logger.error(f"/premium_wallets error: {e}")
        await message.answer("⚠️ Could not load the Smart Wallet leaderboard right now.")


@router.callback_query(F.data == "premium:wallets")
async def cb_premium_wallets(callback: CallbackQuery):
    try:
        await callback.message.answer(await build_wallets_view())
        await callback.answer()
    except Exception as e:
        logger.error(f"premium wallets callback error: {e}")
        await callback.answer("Could not load leaderboard.", show_alert=True)


@router.message(Command("premium_signals"))
async def cmd_premium_signals(message: Message):
    try:
        await message.answer(await build_signals_view(message.from_user.id))
    except Exception as e:
        logger.error(f"/premium_signals error: {e}")
        await message.answer("⚠️ Could not load Premium Signals right now.")


@router.callback_query(F.data == "premium:signals")
async def cb_premium_signals(callback: CallbackQuery):
    try:
        await callback.message.answer(await build_signals_view(callback.from_user.id))
        await callback.answer()
    except Exception as e:
        logger.error(f"premium signals callback error: {e}")
        await callback.answer("Could not load Premium Signals.", show_alert=True)


@router.message(Command("premium_signals_toggle"))
async def cmd_premium_signals_toggle(message: Message):
    """Lets a Premium member opt in/out of Premium Signal broadcasts (on by default)."""
    if not await is_premium(message.from_user.id):
        await message.answer(PREMIUM_REQUIRED_MESSAGE)
        return

    try:
        current = await get_signal_alerts_enabled(message.from_user.id)
        await set_signal_alerts(message.from_user.id, not current)
        new_state = "enabled ✅" if not current else "disabled ⛔"
        await message.answer(f"💎 Premium Signal alerts are now <b>{new_state}</b>.")
    except Exception as e:
        logger.error(f"premium_signals_toggle error: {e}")
        await message.answer("⚠️ Could not update your alert preference.")


@router.message(Command("premium_stats"))
async def cmd_premium_stats(message: Message):
    """Public transparency view into how the autonomous engine is doing."""
    try:
        stats = await get_premium_engine_stats()
        by_status = stats.get("by_status") or {}
        by_tier = stats.get("by_tier") or {}

        stage = stats.get("operational_stage", "bootstrapping")
        stage_label = {
            "bootstrapping": "🚀 Bootstrapping (below minimum threshold — Premium Signals paused)",
            "operational": "🟢 Operational (generating Premium Signals)",
            "recommended": "🟢 Recommended Capacity (improved confidence)",
            "optimal": "🌟 Optimal Capacity (highest-quality consensus)",
        }.get(stage, stage)

        lines = [
            "🧠 <b>Premium Intelligence Engine — Status</b>",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"📊 Total wallets tracked: <b>{stats.get('total_wallets', 0)}</b>",
            f"✅ Active (qualified) wallets: <b>{stats.get('active_wallets', 0)}</b>",
            f"🎯 Engine status: {stage_label}",
            f"🎯 Long-term target: <b>{stats.get('initial_target')}</b> "
            f"(hard cap {stats.get('hard_cap')})",
        ]
        if stats.get("avg_active_score") is not None:
            lines.append(f"📈 Avg. active reputation score: <b>{stats['avg_active_score']}</b>/100")

        lines.append("")
        lines.append("<b>By status:</b>")
        for k in ("active", "watch", "candidate"):
            if k in by_status:
                lines.append(f"   • {k}: {by_status[k]}")

        lines.append("")
        lines.append("<b>By tier:</b>")
        for k in ("elite", "core", "watch", "candidate"):
            if k in by_tier:
                lines.append(f"   • {TIER_EMOJI.get(k, '')} {k}: {by_tier[k]}")

        lines.append("")
        lines.append("⚙️ Discovery, scoring, monitoring, and pruning all run continuously — no manual admin required.")

        await message.answer("\n".join(lines))
    except Exception as e:
        logger.error(f"/premium_stats error: {e}")
        await message.answer("⚠️ Could not load engine stats right now.")


@router.message(Command("premium_discover"))
async def cmd_premium_discover(message: Message):
    """Admin-only manual trigger — purely an operational convenience, the engine never needs this."""
    if not await _is_admin(message.from_user.id):
        await message.answer("⛔ Admin only.")
        return

    try:
        await message.answer("🔎 Running a manual Smart Wallet discovery cycle...")
        stats = await trigger_manual_discovery_cycle()
        await message.answer(
            "✅ Discovery cycle complete.\n"
            f"Sources: {stats.get('sources')}\n"
            f"Validated: {stats.get('validated')} • Inserted: {stats.get('inserted')}"
        )
    except Exception as e:
        logger.error(f"premium_discover error: {e}")
        await message.answer("⚠️ Discovery cycle failed.")


# ============================================================
# Premium Token Snapshot
#
# A Premium-exclusive analysis card for any single token, on demand.
# Deliberately self-contained inside the Premium module: it calls the
# same already-integrated data services premium_signal_engine.py uses
# for its own AI gate (dexscreener, goplus, holders) rather than
# touching bot/commands/token.py or the free Signal Engine at all, so
# "Basic Token Snapshot" for Free users is completely unaffected.
# ============================================================

async def _build_premium_snapshot(contract: str) -> str:
    from providers.marketdata.dexscreener import get_token_card_info
    from providers.marketdata.goplus import check_token_security
    from domain.intelligence.holders import get_holder_analysis
    from domain.signals.scoring import hard_reject_reasons, score_candidate

    data = await get_token_card_info(contract)
    if not data:
        return "❌ Couldn't find that token. Double-check the contract address."

    sec = await check_token_security(contract)
    dev_address = (sec or {}).get("creator_address")
    holder_analysis = await get_holder_analysis(contract, dev_address=dev_address)
    holders = holder_analysis.get("total_holders") if holder_analysis else None

    reject_reasons = hard_reject_reasons(data, sec, holder_analysis, contract)
    result = None if reject_reasons else score_candidate(data, sec, holder_analysis, holders, contract)

    name = data.get("name") or "Unknown"
    symbol = data.get("symbol") or "?"
    price = data.get("price")
    mcap = data.get("market_cap") or data.get("fdv")
    liq = data.get("liquidity")
    pair_url = data.get("pair_url") or ""

    lines = [
        format_premium_header(),
        "",
        f"🪙 <b>{name} ({symbol})</b>",
        f"<code>{contract}</code>",
        "",
        f"💵 Price: <b>${price}</b>" if price not in (None, "N/A") else "💵 Price: <b>N/A</b>",
        f"📊 Market Cap: <b>${float(mcap):,.0f}</b>" if mcap not in (None, "N/A") else "📊 Market Cap: <b>N/A</b>",
        f"💧 Liquidity: <b>${float(liq):,.0f}</b>" if liq not in (None, "N/A") else "💧 Liquidity: <b>N/A</b>",
        f"👥 Holders: <b>{holders}</b>" if holders is not None else "👥 Holders: <b>N/A</b>",
        "",
    ]

    if reject_reasons:
        lines.append("⚠️ <b>Risk Analysis:</b> flagged — " + ", ".join(reject_reasons[:4]))
    elif result:
        lines.append(f"🧠 AI Conviction Score: <b>{result.get('final_score', 0):.0f}/100</b> ({result.get('tier', 'N/A')})")
        lines.append("✅ <b>Risk Analysis:</b> no hard-reject flags")
    else:
        lines.append("🧠 AI Conviction Score: <b>N/A</b>")

    if pair_url:
        lines.append("")
        lines.append(f'🔗 <a href="{pair_url}">View Chart</a>')

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(format_premium_badge())
    return "\n".join(lines)


@router.message(Command("premium_snapshot"))
async def cmd_premium_snapshot(message: Message):
    if not await is_premium(message.from_user.id):
        await message.answer(PREMIUM_REQUIRED_MESSAGE)
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Usage: <code>/premium_snapshot &lt;contract address&gt;</code>")
        return

    contract = parts[1].strip()
    try:
        await message.answer(await _build_premium_snapshot(contract))
    except Exception as e:
        logger.error(f"/premium_snapshot error: {e}")
        await message.answer("⚠️ Could not build a Premium Token Snapshot right now.")
