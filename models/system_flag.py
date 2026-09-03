from sqlalchemy import Column, String
from infra.db.session import Base


class SystemFlag(Base):
    """
    Small persistent key-value store for one-off migration markers and
    simple counters (e.g. 'has the one-time signal history reset run yet',
    'how many signals have been sent in total'). Deliberately generic so we
    don't need a new table for every small piece of durable state.
    """
    __tablename__ = "system_flags"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=True, default="")

    def __repr__(self):
        return f"<SystemFlag {self.key}={self.value}>"
