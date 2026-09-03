from sqlalchemy import Column, BigInteger, String, Float, Boolean, DateTime, func, ForeignKey
from infra.db.session import Base


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    contract = Column(String, nullable=False)
    alert_type = Column(String, nullable=False)  # "price_up", "price_down", "volume_spike", "security"
    threshold = Column(Float, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    def __repr__(self):
        return f"<Alert {self.user_id}:{self.alert_type}>"
