from sqlalchemy import Column, BigInteger, String, Float, Integer, Boolean, DateTime, func
from infra.db.session import Base


class PremiumPlan(Base):
    """
    Admin-configurable subscription plan (Monthly/Quarterly/Yearly/Lifetime
    by default, but nothing is hardcoded — the Owner can add/edit/retire
    plans from the Admin Panel without a code change or redeploy).

    duration_days = NULL means Lifetime (no expiry set on activation).

    price_usd is the reference price shown to users. Crypto amounts are
    NOT auto-converted from it (no live FX oracle in this build, which
    would be its own failure mode) — price_sol / price_usdc are the
    actual amounts a crypto payment must match, set by the admin
    alongside price_usd. If a coin's price field is left null, that
    plan simply isn't payable in that coin.
    """

    __tablename__ = "premium_plans"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    key = Column(String, nullable=False, unique=True)   # e.g. "monthly", "quarterly", "yearly", "lifetime"
    name = Column(String, nullable=False)                # display name

    duration_days = Column(Integer, nullable=True)        # NULL = lifetime
    price_usd = Column(Float, nullable=False, default=0.0)

    price_sol = Column(Float, nullable=True)
    price_usdc = Column(Float, nullable=True)
    price_usdt = Column(Float, nullable=True)

    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<PremiumPlan {self.key} ${self.price_usd}>"
