from sqlalchemy import Column, BigInteger, String, DateTime, func, UniqueConstraint, ForeignKey
from infra.db.session import Base


class TrackedWallet(Base):
    __tablename__ = "tracked_wallets"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    wallet_address = Column(String, nullable=False)
    label = Column(String, nullable=True)
    added_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "wallet_address", name="uq_user_wallet"),
    )

    def __repr__(self):
        return f"<TrackedWallet {self.user_id}:{self.wallet_address}>"
