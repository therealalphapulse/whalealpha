from sqlalchemy import Column, BigInteger, String, Boolean, DateTime, func, ForeignKey
from infra.db.session import Base


class PremiumMembership(Base):
    """
    One row per user who has ever had Premium activated. Absence of a row
    means "never premium" (treated the same as an expired/inactive row by
    services/premium_service.is_premium()).

    status: "active" | "expired" | "revoked"
      - active:  currently entitled to Premium benefits.
      - expired: was active, ran past expires_at (set automatically by
        the expiry sweep in services/premium_service.py, or lazily the
        next time is_premium() is checked).
      - revoked: manually ended by an admin before its natural expiry.

    expires_at = NULL means a lifetime/unlimited membership (no auto-expiry).

    This table is the single source of truth every future Premium-only
    feature should gate against via services.premium_service.is_premium() —
    see that module's docstring for the intended extension pattern.
    """

    __tablename__ = "premium_memberships"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, unique=True, index=True)

    status = Column(String, nullable=False, default="expired")  # active | expired | revoked
    tier = Column(String, nullable=False, default="premium")    # reserved for future multi-tier support

    activated_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)  # NULL = lifetime

    auto_renew = Column(Boolean, default=False)  # placeholder — no payment integration yet

    granted_by = Column(String, nullable=True)   # admin telegram_id (as string) or "system"
    revoked_by = Column(String, nullable=True)
    notes = Column(String, nullable=True)

    # Whether this user receives Premium Signal broadcasts (Smart Wallet
    # consensus + AI high-confidence alerts). Defaults on for every
    # Premium member; toggleable via /premium_signals_toggle. See
    # services/premium_signal_engine.py for the broadcast itself.
    signal_alerts_enabled = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<PremiumMembership user={self.user_id} status={self.status}>"
