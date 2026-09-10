"""
models/auto_trade_claim.py

Atomic per-(user, signal) idempotency claim for the standalone
Auto-Trade Engine (§9 of the spec: "Idempotency").

Structurally similar to models/auto_buy_claim.py (which belongs to the
existing, untouched domain/trading/real automation) but keyed on
signal_id rather than contract, per the spec's explicit "idempotency
key such as user_id + signal_id" guidance. Kept separate so the two
engines can never contend on each other's claims.

Status lifecycle: pending -> committed (buy confirmed on-chain and an
AutoTradePosition exists; permanent) or pending -> failed (buy
definitively did not happen -- safe to retry a later signal for the
same token, though not this exact signal_id again).
"""

from sqlalchemy import Column, BigInteger, String, DateTime, func, UniqueConstraint
from infra.db.session import Base


class AutoTradeClaim(Base):
    __tablename__ = "auto_trade_claims"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    signal_id = Column(BigInteger, nullable=False, index=True)
    contract = Column(String, nullable=False, index=True)

    # "pending" | "committed" | "failed"
    status = Column(String, nullable=False, default="pending")

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "signal_id", name="uq_auto_trade_claims_user_signal"),
    )

    def __repr__(self):
        return f"<AutoTradeClaim user={self.user_id} signal={self.signal_id} status={self.status}>"
