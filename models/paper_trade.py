from sqlalchemy import Column, BigInteger, String, Float, Boolean, Integer, DateTime, func, ForeignKey
from infra.db.session import Base


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)

    contract = Column(String, nullable=False)
    name = Column(String, nullable=True)
    symbol = Column(String, nullable=True)

    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    exit_price = Column(Float, nullable=True)
    highest_price = Column(Float, nullable=True)
    lowest_price = Column(Float, nullable=True)

    usd_invested = Column(Float, nullable=False)
    token_quantity = Column(Float, nullable=False)
    remaining_quantity = Column(Float, nullable=True)

    take_profit_pct = Column(Float, default=100.0)
    stop_loss_pct = Column(Float, default=15.0)

    pnl_usd = Column(Float, default=0.0)
    pnl_pct = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)

    status = Column(String, default="open")  # open, closed_tp, closed_sl, closed_manual
    exit_reason = Column(String, nullable=True)

    # DCA (Dollar-Cost Averaging) tracking. entry_price above becomes the
    # live weighted-average cost basis once DCA fills happen (used as-is
    # by existing TP/SL %% and display logic); initial_entry_price keeps
    # the original fill price so DCA drawdown levels have a stable anchor
    # to trigger from regardless of how the average has since moved.
    initial_entry_price = Column(Float, nullable=True)
    dca_fills = Column(Integer, default=0)  # count of DCA add-ins, NOT including the initial buy
    last_dca_price = Column(Float, nullable=True)

    opened_at = Column(DateTime, server_default=func.now())
    closed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<PaperTrade {self.symbol} {self.status}>"
