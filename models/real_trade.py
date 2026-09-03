from sqlalchemy import Column, BigInteger, String, Float, Integer, DateTime, func, ForeignKey
from infra.db.session import Base


class RealTrade(Base):
    """
    A real, on-chain buy/sell executed from a user's Real Wallet via
    Jupiter. Mirrors PaperTrade's shape so the existing dashboard/PnL
    UX patterns can be reused, but every row here corresponds to an
    actual signed Solana transaction (tx_signature_* columns), not a
    simulation.
    """

    __tablename__ = "real_trades"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)

    contract = Column(String, nullable=False)
    name = Column(String, nullable=True)
    symbol = Column(String, nullable=True)

    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)

    sol_spent = Column(Float, nullable=False)
    token_quantity = Column(Float, nullable=False)
    remaining_quantity = Column(Float, nullable=True)

    sol_received = Column(Float, nullable=True)

    pnl_usd = Column(Float, default=0.0)
    pnl_pct = Column(Float, default=0.0)
    realized_pnl_sol = Column(Float, default=0.0)

    status = Column(String, default="open")  # open, closed_manual
    exit_reason = Column(String, nullable=True)

    # "manual" (default, unchanged behavior) | "automation" | "dca" — set
    # by services/real_trade_engine.execute_real_buy's optional `source`
    # argument. Purely descriptive (history/position display); trading
    # logic doesn't branch on it.
    source = Column(String, nullable=False, default="manual")

    tx_signature_buy = Column(String, nullable=True)
    tx_signature_sell = Column(String, nullable=True)

    slippage_bps = Column(Integer, default=150)
    token_decimals = Column(Integer, nullable=False, default=6)

    opened_at = Column(DateTime, server_default=func.now())
    closed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<RealTrade {self.symbol} {self.status}>"
