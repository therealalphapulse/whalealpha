from sqlalchemy import Column, BigInteger, String, Boolean, Integer, Float, DateTime, func, ForeignKey
from infra.db.session import Base


class RealWallet(Base):
    """A user's encrypted mainnet Solana wallet and automation safety state."""

    __tablename__ = "real_wallets"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, unique=True, index=True)
    public_key = Column(String, nullable=False, index=True)
    encrypted_secret = Column(String, nullable=False)
    encryption_nonce = Column(String, nullable=False)
    source = Column(String, nullable=False, default="created")

    auto_trading_enabled = Column(Boolean, default=False)
    auto_max_daily_spend_sol = Column(Float, default=1.0)
    auto_daily_spent_sol = Column(Float, default=0.0)
    auto_daily_spent_date = Column(String, nullable=True)
    # Separate from the SOL spend cap: limits the number of signal-driven
    # auto-buys per UTC day for this user (1-20).
    auto_daily_buy_count = Column(BigInteger, default=0)
    auto_daily_buy_count_date = Column(String, nullable=True)
    auto_kill_switch = Column(Boolean, default=False)

    slippage_bps = Column(Integer, default=150)
    priority_fee_tier = Column(String, default="auto")
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<RealWallet user={self.user_id} pubkey={self.public_key}>"