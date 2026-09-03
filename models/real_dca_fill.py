from sqlalchemy import Column, BigInteger, String, Float, Integer, DateTime, ForeignKey, func
from infra.db.session import Base


class RealDCAFill(Base):
    """
    One executed (or failed) order belonging to a RealDCASchedule.
    Kept separate from RealTrade so a schedule's own order-by-order
    history can be shown (services/real_dca_engine.get_schedule_fills)
    even though each successful fill also creates/updates a normal
    RealTrade row for position tracking.
    """

    __tablename__ = "real_dca_fills"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    schedule_id = Column(BigInteger, ForeignKey("real_dca_schedules.id"), nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False, index=True)

    order_index = Column(Integer, nullable=False)  # 1-based position within the schedule

    status = Column(String, nullable=False, default="filled")  # filled | skipped | failed
    reason = Column(String, nullable=True)  # skip/failure reason, or null on a clean fill

    sol_amount = Column(Float, nullable=True)
    token_quantity = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    tx_signature = Column(String, nullable=True)

    real_trade_id = Column(BigInteger, nullable=True)

    created_at = Column(DateTime, server_default=func.now())

    def __repr__(self):
        return f"<RealDCAFill schedule={self.schedule_id} order={self.order_index} {self.status}>"
