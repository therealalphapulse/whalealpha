from sqlalchemy import Column, BigInteger, String, Float, DateTime, func, ForeignKey
from infra.db.session import Base


class WalletWithdrawal(Base):
    """
    Audit log of every Real Wallet withdrawal (SOL or SPL token) sent to
    an external address. Separate from RealTrade since a withdrawal isn't
    a swap/position — it's funds leaving AlphaPulse entirely.

    Written after broadcast (status starts "broadcast", then updated to
    "confirmed"/"failed"/"timeout" once services.wallet_withdraw polls
    the result) so a row always exists for anything that made it past
    signing, even if confirmation is inconclusive.
    """

    __tablename__ = "wallet_withdrawals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)

    # "SOL" for native transfers, otherwise the SPL mint address.
    mint = Column(String, nullable=False)
    symbol = Column(String, nullable=True)

    amount = Column(Float, nullable=False)
    destination_address = Column(String, nullable=False)

    tx_signature = Column(String, nullable=True)
    status = Column(String, nullable=False, default="broadcast")  # broadcast|confirmed|failed|timeout
    fail_reason = Column(String, nullable=True)

    created_at = Column(DateTime, server_default=func.now())

    def __repr__(self):
        return f"<WalletWithdrawal user={self.user_id} {self.amount} {self.symbol} -> {self.destination_address}>"
