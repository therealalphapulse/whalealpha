from sqlalchemy import Column, BigInteger, String, Float, Integer, DateTime, func, ForeignKey
from infra.db.session import Base


class PaperDcaFill(Base):
    """
    One row per executed DCA add-in (never the initial buy — that's still
    recorded as the PaperTrade itself). This is the DCA-specific trade
    history ledger referenced by the feature request, kept separate from
    PaperPnlEvent (which only logs realized PnL on sells) since a DCA fill
    is a buy-side event with no PnL of its own yet.
    """

    __tablename__ = "paper_dca_fills"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)
    trade_id = Column(BigInteger, nullable=False, index=True)

    contract = Column(String, nullable=False)
    symbol = Column(String, nullable=True)

    fill_number = Column(Integer, nullable=False)  # 2 = first DCA add-in (1 is the initial buy)
    trigger_reason = Column(String, nullable=True)  # "price_drawdown" or "duplicate_signal_merge"

    price = Column(Float, nullable=False)
    usd_amount = Column(Float, nullable=False)
    token_quantity = Column(Float, nullable=False)

    new_avg_entry_price = Column(Float, nullable=True)
    new_total_invested = Column(Float, nullable=True)

    occurred_at = Column(DateTime, server_default=func.now())

    def __repr__(self):
        return f"<PaperDcaFill trade={self.trade_id} fill={self.fill_number}>"
