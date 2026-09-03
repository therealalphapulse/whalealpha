from sqlalchemy import Column, BigInteger, String, Float, DateTime, func, ForeignKey
from infra.db.session import Base


class PaperPnlEvent(Base):
    """
    One row per realized PnL event (a full close or a partial/moonbag sell).
    Used to power the Paper Trading PnL Calendar with accurate day-by-day
    totals, since a single PaperTrade can realize PnL across several days
    (moonbag partial sells) before it is fully closed.
    """
    __tablename__ = "paper_pnl_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)
    trade_id = Column(BigInteger, nullable=False, index=True)

    symbol = Column(String, nullable=True)
    pnl_usd = Column(Float, nullable=False, default=0.0)

    occurred_at = Column(DateTime, server_default=func.now(), index=True)

    def __repr__(self):
        return f"<PaperPnlEvent user={self.user_id} trade={self.trade_id} pnl={self.pnl_usd}>"
