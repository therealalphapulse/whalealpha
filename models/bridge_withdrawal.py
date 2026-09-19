from sqlalchemy import Column, BigInteger, String, Boolean, Float, DateTime, func, ForeignKey
from infra.db.session import Base


class BridgeWithdrawal(Base):
    """Tracks an ETH withdrawal from Robinhood Chain back to Ethereum via
    the canonical Arbitrum bridge. A withdrawal is a three-step process
    (see domain/trading/real/robinhood_bridge.py): initiate on L2, wait out
    the ~7-day fraud-proof challenge period, then claim on L1.
    """

    __tablename__ = "bridge_withdrawals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)

    amount_eth = Column(Float, nullable=False)
    destination_address = Column(String, nullable=False)

    l2_tx_hash = Column(String, nullable=False)
    l2_to_l1_position = Column(String, nullable=False)
    l2_block_number = Column(BigInteger, nullable=False)
    # Exact L2 block timestamp (unix seconds) -- NOT initiated_at (DB
    # insert time). The outbox proof's hash check requires this exact
    # on-chain value or the claim transaction reverts.
    l2_block_timestamp = Column(BigInteger, nullable=False)

    initiated_at = Column(DateTime, server_default=func.now())
    claimable_after = Column(DateTime, nullable=False)

    claimed = Column(Boolean, default=False)
    l1_claim_tx_hash = Column(String, nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    claim_failure_reason = Column(String, nullable=True)

    def __repr__(self):
        return f"<BridgeWithdrawal user={self.user_id} amount={self.amount_eth} claimed={self.claimed}>"
