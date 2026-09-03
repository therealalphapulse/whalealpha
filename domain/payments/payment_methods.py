"""
Admin-configured payment methods (crypto receiving wallets + manual
instructions). See models/payment_method.py.
"""

import logging
from sqlalchemy import select
from infra.db.session import async_session
from models.payment_method import PaymentMethod

logger = logging.getLogger("AlphaPulse.PaymentMethods")


async def get_active_methods(method_type: str | None = None) -> list[PaymentMethod]:
    async with async_session() as session:
        query = select(PaymentMethod).where(PaymentMethod.is_active == True)  # noqa: E712
        if method_type:
            query = query.where(PaymentMethod.method_type == method_type)
        result = await session.execute(query.order_by(PaymentMethod.sort_order))
        return result.scalars().all()


async def get_all_methods() -> list[PaymentMethod]:
    async with async_session() as session:
        result = await session.execute(select(PaymentMethod).order_by(PaymentMethod.sort_order))
        return result.scalars().all()


async def get_method(key: str) -> PaymentMethod | None:
    async with async_session() as session:
        result = await session.execute(select(PaymentMethod).where(PaymentMethod.key == key))
        return result.scalar_one_or_none()


async def create_crypto_method(key: str, label: str, asset: str, receive_address: str) -> tuple[bool, str]:
    if asset not in ("SOL", "USDC", "USDT"):
        return False, "Asset must be SOL, USDC, or USDT."
    if await get_method(key):
        return False, "A payment method with that key already exists."
    async with async_session() as session:
        session.add(PaymentMethod(
            key=key, label=label, method_type="crypto",
            asset=asset, receive_address=receive_address,
        ))
        await session.commit()
    return True, f"Crypto payment method '{label}' ({asset}) created."


async def create_manual_method(key: str, label: str, instructions: str) -> tuple[bool, str]:
    if await get_method(key):
        return False, "A payment method with that key already exists."
    async with async_session() as session:
        session.add(PaymentMethod(
            key=key, label=label, method_type="manual", instructions=instructions,
        ))
        await session.commit()
    return True, f"Manual payment method '{label}' created."


async def set_method_active(key: str, active: bool) -> bool:
    async with async_session() as session:
        result = await session.execute(select(PaymentMethod).where(PaymentMethod.key == key))
        method = result.scalar_one_or_none()
        if not method:
            return False
        method.is_active = active
        await session.commit()
        return True


async def delete_method(key: str) -> bool:
    async with async_session() as session:
        result = await session.execute(select(PaymentMethod).where(PaymentMethod.key == key))
        method = result.scalar_one_or_none()
        if not method:
            return False
        await session.delete(method)
        await session.commit()
        return True
