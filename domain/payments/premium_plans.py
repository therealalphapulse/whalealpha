"""
Admin-configurable Premium subscription plans. See models/premium_plan.py.
"""

import logging
from sqlalchemy import select
from infra.db.session import async_session
from models.premium_plan import PremiumPlan

logger = logging.getLogger("AlphaPulse.PremiumPlans")

# Seeded once, only if the table is empty, so a fresh deploy has
# something payable immediately. All values are then fully editable
# from the Admin Panel — nothing here is hardcoded into behavior.
_DEFAULT_PLANS = [
    dict(key="monthly", name="Monthly", duration_days=30, price_usd=19.0, price_sol=None, sort_order=1),
    dict(key="quarterly", name="Quarterly", duration_days=90, price_usd=49.0, price_sol=None, sort_order=2),
    dict(key="yearly", name="Yearly", duration_days=365, price_usd=149.0, price_sol=None, sort_order=3),
    dict(key="lifetime", name="Lifetime", duration_days=None, price_usd=399.0, price_sol=None, sort_order=4),
]


async def ensure_default_plans_seeded() -> None:
    async with async_session() as session:
        result = await session.execute(select(PremiumPlan))
        if result.first():
            return
        for p in _DEFAULT_PLANS:
            session.add(PremiumPlan(**p))
        await session.commit()
        logger.info("Seeded default Premium plans (monthly/quarterly/yearly/lifetime).")


async def get_active_plans() -> list[PremiumPlan]:
    async with async_session() as session:
        result = await session.execute(
            select(PremiumPlan).where(PremiumPlan.is_active == True).order_by(PremiumPlan.sort_order)  # noqa: E712
        )
        return result.scalars().all()


async def get_all_plans() -> list[PremiumPlan]:
    async with async_session() as session:
        result = await session.execute(select(PremiumPlan).order_by(PremiumPlan.sort_order))
        return result.scalars().all()


async def get_plan(key: str) -> PremiumPlan | None:
    async with async_session() as session:
        result = await session.execute(select(PremiumPlan).where(PremiumPlan.key == key))
        return result.scalar_one_or_none()


async def create_plan(key: str, name: str, duration_days: int | None, price_usd: float) -> tuple[bool, str]:
    if await get_plan(key):
        return False, "A plan with that key already exists."
    async with async_session() as session:
        session.add(PremiumPlan(key=key, name=name, duration_days=duration_days, price_usd=price_usd))
        await session.commit()
    return True, f"Plan '{name}' created."


async def set_plan_active(key: str, active: bool) -> bool:
    async with async_session() as session:
        result = await session.execute(select(PremiumPlan).where(PremiumPlan.key == key))
        plan = result.scalar_one_or_none()
        if not plan:
            return False
        plan.is_active = active
        await session.commit()
        return True


async def set_plan_price(key: str, field: str, value: float | None) -> bool:
    """field is one of: price_usd, price_sol, price_usdc, price_usdt"""
    if field not in ("price_usd", "price_sol", "price_usdc", "price_usdt"):
        return False
    async with async_session() as session:
        result = await session.execute(select(PremiumPlan).where(PremiumPlan.key == key))
        plan = result.scalar_one_or_none()
        if not plan:
            return False
        setattr(plan, field, value)
        await session.commit()
        return True


async def delete_plan(key: str) -> bool:
    async with async_session() as session:
        result = await session.execute(select(PremiumPlan).where(PremiumPlan.key == key))
        plan = result.scalar_one_or_none()
        if not plan:
            return False
        await session.delete(plan)
        await session.commit()
        return True
