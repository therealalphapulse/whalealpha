from sqlalchemy import Column, BigInteger, Boolean, DateTime, func, UniqueConstraint, ForeignKey
from infra.db.session import Base


class KolAlertSubscription(Base):
    __tablename__ = "kol_alert_subscriptions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    enabled = Column(Boolean, default=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_kol_alert_user"),
    )

    def __repr__(self):
        return f"<KolAlertSubscription {self.user_id}:{self.enabled}>"
