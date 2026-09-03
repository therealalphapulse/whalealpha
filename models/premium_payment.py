from sqlalchemy import Column, BigInteger, String, Float, DateTime, func, ForeignKey
from infra.db.session import Base


class PremiumPayment(Base):
    """
    A single Premium payment request, either crypto (auto-verified
    on-chain, see services/solana_payment_verify.py) or manual (proof
    submitted, then approved/rejected by an admin). See
    services/premium_payments.py for the full lifecycle.

    status: pending -> verifying -> approved            (crypto path)
            pending -> awaiting_review -> approved/rejected  (manual path)
            pending -> expired  (abandoned, never paid/submitted)
    """

    __tablename__ = "premium_payments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)

    plan_key = Column(String, nullable=False)
    method_key = Column(String, nullable=False)
    method_type = Column(String, nullable=False)  # "crypto" | "manual"

    expected_amount = Column(Float, nullable=False)
    currency = Column(String, nullable=False)  # "SOL" | "USDC" | "USDT" | "USD"

    status = Column(String, nullable=False, default="pending", index=True)

    # Crypto path
    tx_signature = Column(String, nullable=True, index=True)

    # Manual path
    proof_text = Column(String, nullable=True)
    proof_file_id = Column(String, nullable=True)

    # Resolution (either path)
    reject_reason = Column(String, nullable=True)
    reviewed_by = Column(String, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, server_default=func.now())

    def __repr__(self):
        return f"<PremiumPayment {self.id} user={self.user_id} {self.status}>"
