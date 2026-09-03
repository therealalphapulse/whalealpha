from sqlalchemy import Column, BigInteger, Float, Boolean, Integer, DateTime, func, ForeignKey
from infra.db.session import Base


class PaperSettings(Base):
    __tablename__ = "paper_settings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, unique=True, index=True)

    auto_buy = Column(Boolean, default=True)
    buy_amount_usd = Column(Float, default=25.0)

    take_profit_pct = Column(Float, default=100.0)
    stop_loss_pct = Column(Float, default=15.0)

    max_open_positions = Column(Integer, default=10)
    daily_trade_limit = Column(Integer, default=10)
    notifications_enabled = Column(Boolean, default=True)
    pnl_cards_enabled = Column(Boolean, default=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<PaperSettings user={self.user_id}>"
