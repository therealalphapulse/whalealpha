from sqlalchemy import Column, BigInteger, Float, DateTime, func, ForeignKey
from infra.db.session import Base


class PaperPortfolio(Base):
    __tablename__ = "paper_portfolios"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, unique=True, index=True)

    initial_balance = Column(Float, nullable=False, default=100.0)
    balance = Column(Float, nullable=False, default=100.0)

    total_trades = Column(BigInteger, default=0)
    winning_trades = Column(BigInteger, default=0)
    losing_trades = Column(BigInteger, default=0)

    total_profit = Column(Float, default=0.0)
    total_loss = Column(Float, default=0.0)
    net_pnl = Column(Float, default=0.0)

    best_trade_pnl = Column(Float, default=0.0)
    worst_trade_pnl = Column(Float, default=0.0)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<PaperPortfolio user={self.user_id}>"
