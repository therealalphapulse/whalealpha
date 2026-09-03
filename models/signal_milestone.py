from sqlalchemy import Column, BigInteger, String, Float, DateTime, func, UniqueConstraint
from infra.db.session import Base


class SignalMilestone(Base):
    __tablename__ = "signal_milestones"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    signal_id = Column(BigInteger, nullable=False, index=True)
    milestone_type = Column(String, nullable=False)
    milestone_value = Column(Float, nullable=True)

    sent_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("signal_id", "milestone_type", name="uq_signal_milestone"),
    )

    def __repr__(self):
        return f"<SignalMilestone {self.signal_id}:{self.milestone_type}>"
