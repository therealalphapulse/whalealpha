"""
Orchestrates the hybrid payment flow: creating a payment request against
a plan + method, verifying crypto payments on-chain, and the manual
proof -> admin review -> approve/reject pipeline. Activates Premium
(via services.premium_service) the moment a payment is confirmed valid,
automatically for crypto, only after admin approval for manual.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from infra.db.session import async_session
from models.premium_payment import PremiumPayment
from domain.payments.premium_plans import get_plan
from domain.payments.payment_methods import get_method
from domain.payments.premium_service import activate_premium, renew_premium
from domain.payments import solana_payment_verify

logger = logging.getLogger("AlphaPulse.PremiumPayments")

# A pending request nobody ever pays or submits proof for is auto-marked
# expired after this long, mostly for the admin queue / /premium history
# view to stay clean rather than accumulating abandoned requests.
EXPIRE_AFTER_MINUTES = 180


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def create_payment_request(user_id: int, plan_key: str, method_key: str) -> tuple[bool, str, PremiumPayment | None]:
    plan = await get_plan(plan_key)
    if not plan or not plan.is_active:
        return False, "That plan isn't available.", None
    method = await get_method(method_key)
    if not method or not method.is_active:
        return False, "That payment method isn't available.", None

    if method.method_type == "crypto":
        price_field = {"SOL": plan.price_sol, "USDC": plan.price_usdc, "USDT": plan.price_usdt}.get(method.asset)
        if price_field is None:
            return False, f"This plan isn't configured for payment in {method.asset} yet.", None
        expected_amount = price_field
        currency = method.asset
    else:
        expected_amount = plan.price_usd
        currency = "USD"

    async with async_session() as session:
        payment = PremiumPayment(
            user_id=user_id, plan_key=plan_key, method_key=method_key,
            method_type=method.method_type, expected_amount=expected_amount, currency=currency,
            status="pending",
        )
        session.add(payment)
        await session.commit()
        await session.refresh(payment)

    return True, "Payment request created.", payment


async def get_payment(payment_id: int) -> PremiumPayment | None:
    async with async_session() as session:
        result = await session.execute(select(PremiumPayment).where(PremiumPayment.id == payment_id))
        return result.scalar_one_or_none()


async def get_user_payment_history(user_id: int, limit: int = 10) -> list[PremiumPayment]:
    async with async_session() as session:
        result = await session.execute(
            select(PremiumPayment).where(PremiumPayment.user_id == user_id)
            .order_by(PremiumPayment.created_at.desc()).limit(limit)
        )
        return result.scalars().all()


async def get_pending_manual_reviews() -> list[PremiumPayment]:
    async with async_session() as session:
        result = await session.execute(
            select(PremiumPayment).where(PremiumPayment.status == "awaiting_review")
            .order_by(PremiumPayment.created_at)
        )
        return result.scalars().all()


async def _signature_already_used(signature: str) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(PremiumPayment).where(
                PremiumPayment.tx_signature == signature,
                PremiumPayment.status == "approved",
            )
        )
        return result.first() is not None


async def _activate_from_plan(user_id: int, plan_key: str, granted_by: str) -> None:
    plan = await get_plan(plan_key)
    if plan.duration_days is None:
        await activate_premium(user_id, duration_days=None, granted_by=granted_by)
    else:
        await renew_premium(user_id, plan.duration_days, granted_by=granted_by)


async def verify_crypto_payment(payment_id: int, tx_signature: str) -> tuple[bool, str]:
    """
    The "automatic" path. Never requires manual approval when this
    returns True — Premium is activated inline, right here.
    """
    payment = await get_payment(payment_id)
    if not payment:
        return False, "Payment request not found."
    if payment.status == "approved":
        return False, "This payment request was already approved."
    if payment.method_type != "crypto":
        return False, "This payment request isn't a crypto payment."

    if await _signature_already_used(tx_signature):
        return False, "That transaction has already been used to activate Premium (for this or another account)."

    method = await get_method(payment.method_key)
    if not method:
        return False, "Payment method no longer exists."

    async with async_session() as session:
        result = await session.execute(select(PremiumPayment).where(PremiumPayment.id == payment_id))
        row = result.scalar_one_or_none()
        row.tx_signature = tx_signature
        row.status = "verifying"
        await session.commit()

    try:
        if method.asset == "SOL":
            actual = await solana_payment_verify.verify_sol_payment(
                tx_signature, method.receive_address, payment.expected_amount
            )
        else:
            actual = await solana_payment_verify.verify_spl_payment(
                tx_signature, method.receive_address, method.asset, payment.expected_amount
            )
    except solana_payment_verify.VerificationError as e:
        await _mark_status(payment_id, "rejected", reject_reason=str(e))
        return False, str(e)
    except Exception as e:
        logger.error(f"Unexpected error verifying payment {payment_id}: {e}")
        await _mark_status(payment_id, "pending")  # back to pending, not rejected — this was our failure, not theirs
        return False, "Couldn't verify that transaction right now (network issue) — try again shortly."

    # Re-check duplicate use right before activating too (covers the
    # narrow race where two verification attempts for the same
    # signature ran concurrently) before committing to "approved".
    if await _signature_already_used(tx_signature):
        await _mark_status(payment_id, "rejected", reject_reason="Duplicate transaction signature.")
        return False, "That transaction has already been used to activate Premium."

    await _activate_from_plan(payment.user_id, payment.plan_key, granted_by=f"crypto:{tx_signature[:12]}")
    await _mark_status(payment_id, "approved", reviewed_by="system(auto-verified)")

    logger.info(f"Premium auto-activated for user {payment.user_id} via crypto payment {payment_id} (tx={tx_signature})")
    return True, f"Payment verified — {actual:.4f} {method.asset} received. Premium is now active!"


async def _mark_status(payment_id: int, status: str, reject_reason: str | None = None, reviewed_by: str | None = None) -> None:
    async with async_session() as session:
        result = await session.execute(select(PremiumPayment).where(PremiumPayment.id == payment_id))
        row = result.scalar_one_or_none()
        if not row:
            return
        row.status = status
        if reject_reason is not None:
            row.reject_reason = reject_reason
        if reviewed_by is not None:
            row.reviewed_by = reviewed_by
            row.reviewed_at = _now()
        await session.commit()


async def submit_manual_proof(payment_id: int, proof_text: str | None, proof_file_id: str | None) -> tuple[bool, str]:
    payment = await get_payment(payment_id)
    if not payment:
        return False, "Payment request not found."
    if payment.method_type != "manual":
        return False, "This isn't a manual payment request."
    if payment.status not in ("pending",):
        return False, "This payment request has already been submitted or resolved."

    async with async_session() as session:
        result = await session.execute(select(PremiumPayment).where(PremiumPayment.id == payment_id))
        row = result.scalar_one_or_none()
        row.proof_text = proof_text
        row.proof_file_id = proof_file_id
        row.status = "awaiting_review"
        await session.commit()

    return True, "Submitted for admin review."


async def approve_manual_payment(payment_id: int, admin_id: int) -> tuple[bool, str]:
    payment = await get_payment(payment_id)
    if not payment:
        return False, "Payment request not found."
    if payment.status != "awaiting_review":
        return False, "This payment isn't awaiting review."

    await _activate_from_plan(payment.user_id, payment.plan_key, granted_by=f"manual:{admin_id}")
    await _mark_status(payment_id, "approved", reviewed_by=str(admin_id))
    return True, "Approved — Premium activated for the user."


async def reject_manual_payment(payment_id: int, admin_id: int, reason: str | None = None) -> tuple[bool, str]:
    payment = await get_payment(payment_id)
    if not payment:
        return False, "Payment request not found."
    if payment.status != "awaiting_review":
        return False, "This payment isn't awaiting review."

    await _mark_status(payment_id, "rejected", reject_reason=reason, reviewed_by=str(admin_id))
    return True, "Rejected."


async def expire_stale_requests() -> int:
    """Run periodically (see main.py) — marks abandoned pending requests
    as expired. Never touches anything already submitted for review or
    already resolved."""
    cutoff = _now() - timedelta(minutes=EXPIRE_AFTER_MINUTES)
    count = 0
    async with async_session() as session:
        result = await session.execute(
            select(PremiumPayment).where(
                PremiumPayment.status == "pending",
                PremiumPayment.created_at < cutoff,
            )
        )
        for row in result.scalars().all():
            row.status = "expired"
            count += 1
        if count:
            await session.commit()
    return count


async def payment_expiry_sweep_loop(interval_seconds: int = 900) -> None:
    import asyncio
    while True:
        try:
            count = await expire_stale_requests()
            if count:
                logger.info(f"Expired {count} stale/abandoned Premium payment requests.")
        except Exception as e:
            logger.error(f"payment_expiry_sweep_loop error: {e}")
        await asyncio.sleep(interval_seconds)
