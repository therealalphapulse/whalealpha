from sqlalchemy import Column, BigInteger, String, DateTime, func, UniqueConstraint, ForeignKey
from infra.db.session import Base


class Watchlist(Base):
    __tablename__ = "watchlists"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    contract = Column(String, nullable=False)
    added_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "contract", name="uq_user_contract"),
    )

    def __repr__(self):
        return f"<Watchlist {self.user_id}:{self.contract}>"
