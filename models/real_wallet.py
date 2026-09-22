from sqlalchemy import Column, BigInteger, String, Boolean, Integer, Float, DateTime, func, ForeignKey
from infra.db.session import Base


class RealWallet(Base):
    """A user's encrypted Robinhood Chain EVM wallet and automation safety state."""

    __tablename__ = "real_wallets"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, unique=True, index=True)
    public_key = Column(String, nullable=False, index=True)
    chain_id = Column(Integer, nullable=True, index=True)
    network = Column(String, nullable=True)
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

    # Global Trailing Stop defaults for this wallet, set from the
    # dedicated Trailing section under /wallet. Used to prefill "apply my
    # default trail" on any open position; each position's actual rule is
    # still an independent RealExitRule (kind="trail") and can be
    # customized per-position regardless of these defaults.
    trail_default_pct = Column(Float, default=10.0)
    trail_default_arm_pct = Column(Float, default=0.0)

    # Master ON/OFF switch (Trailing section under /realwallet). When True,
    # every open and future position — manual (RealExitRule kind="trail")
    # and auto-bought (AutoTradePolicy.trailing_stop_enabled, a separate
    # existing system) — gets a trailing stop; when False, trailing is
    # cancelled/disabled everywhere without touching TP/SL. See
    # app_platform/commands/real_wallet.py's rw:trail_global_toggle.
    trail_global_enabled = Column(Boolean, default=False)

    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<RealWallet user={self.user_id} pubkey={self.public_key}>"