from sqlalchemy import Column, BigInteger, String, Boolean, Integer, DateTime, func
from infra.db.session import Base


class PaymentMethod(Base):
    """
    Admin-configured payment method the Owner sets up from the Admin
    Panel — no code change needed to add a new receiving wallet or a
    new manual (bank transfer / local payment) option.

    method_type: "crypto" | "manual"

    For "crypto":
      asset          - "SOL" | "USDC" | "USDT"
      receive_address - the wallet address payments must land in.
                        For SOL this is the address itself. For
                        USDC/USDT (SPL tokens) this is also the OWNER
                        WALLET address — services.premium_payments
                        derives that wallet's associated token account
                        for the relevant mint when checking a transfer,
                        same derivation used in services/wallet_withdraw.py.

    For "manual":
      instructions   - free text shown to the user (bank details, etc.)
    """

    __tablename__ = "payment_methods"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    key = Column(String, nullable=False, unique=True)
    label = Column(String, nullable=False)
    method_type = Column(String, nullable=False)  # "crypto" | "manual"

    # crypto fields (nullable — only used when method_type == "crypto")
    asset = Column(String, nullable=True)              # "SOL" | "USDC" | "USDT"
    receive_address = Column(String, nullable=True)

    # manual fields (nullable — only used when method_type == "manual")
    instructions = Column(String, nullable=True)

    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<PaymentMethod {self.key} ({self.method_type})>"
