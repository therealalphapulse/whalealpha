from enum import Enum

from sqlalchemy import Column, BigInteger, String, Float, DateTime, Enum as SAEnum, ForeignKey, func
from sqlalchemy.orm import relationship

from infra.db.session import Base


class Milestone(str, Enum):
    ENTRY = "entry"
    PCT_25 = "25pct"
    PCT_50 = "50pct"
    TWO_X = "2x"
    THREE_X = "3x"
    FOUR_X = "4x"
    FIVE_X = "5x"
    SIX_X = "6x"
    TEN_X = "10x"
    MULTI_X = "multi_x"
    DUMP = "dump"
    ARCHIVE = "archive"


class SignalEvent(Base):
    __tablename__ = "signal_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # IMPORTANT: add ForeignKey here
    signal_id = Column(BigInteger, ForeignKey("signal_tokens.id"), nullable=False, index=True)

    milestone_type = Column(SAEnum(Milestone), nullable=False)
    milestone_value = Column(Float, nullable=True)

    timestamp = Column(DateTime, server_default=func.now(), index=True)

    status = Column(String, nullable=True, default="pending")
    raw_data = Column(String, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    resolved_at = Column(DateTime, nullable=True)

    note = Column(String, nullable=True)

    # Relationship back to SignalToken
    signal = relationship("SignalToken", back_populates="events")

    def __repr__(self):
        return f"<SignalEvent {self.milestone_type.value}>"
