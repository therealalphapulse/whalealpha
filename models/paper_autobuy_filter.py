from sqlalchemy import Column, BigInteger, Float, Boolean, DateTime, func, ForeignKey
from infra.db.session import Base


class PaperAutoBuyFilter(Base):
    """
    Per-user, user-configurable auto-buy filter criteria for the Paper
    Trade auto-buy engine (DEX-screener / ave.ai / GMGN-style filtering).

    All threshold fields are nullable — a null value means "no constraint
    on this field". If every field is null (or `enabled` is False), the
    user has no active filters and the auto-buy engine falls back to its
    randomized high-potential-signal selection instead of filter matching.
    """

    __tablename__ = "paper_autobuy_filters"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, unique=True, index=True)

    enabled = Column(Boolean, default=True)

    min_market_cap = Column(Float, nullable=True)
    max_market_cap = Column(Float, nullable=True)

    min_holders = Column(Float, nullable=True)
    min_liquidity_usd = Column(Float, nullable=True)

    max_bundle_pct = Column(Float, nullable=True)
    max_dev_holding_pct = Column(Float, nullable=True)

    min_age_hours = Column(Float, nullable=True)
    max_age_hours = Column(Float, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def has_active_filters(self) -> bool:
        return any([
            self.min_market_cap is not None,
            self.max_market_cap is not None,
            self.min_holders is not None,
            self.min_liquidity_usd is not None,
            self.max_bundle_pct is not None,
            self.max_dev_holding_pct is not None,
            self.min_age_hours is not None,
            self.max_age_hours is not None,
        ])

    def __repr__(self):
        return f"<PaperAutoBuyFilter user={self.user_id} enabled={self.enabled}>"
