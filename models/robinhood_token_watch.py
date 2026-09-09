"""
Per-contract price/liquidity history for Discovery Engine B (Robinhood
Chain Token Discovery). Updated every cycle for every candidate
observed -- not just ones that get alerted -- so the REVIVAL lane's
"dumped, now recovering" detection has real multi-cycle history behind
it instead of guessing from a single DexScreener snapshot. Independent
of RobinhoodDiscoverySignal (the alert/dedupe ledger): this table is
pure observation history and is never itself alerted on directly.
"""

from sqlalchemy import Column, BigInteger, String, Float, Integer, DateTime, func

from infra.db.session import Base


class RobinhoodTokenWatch(Base):
    __tablename__ = "robinhood_token_watch"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    token_contract = Column(String, nullable=False, unique=True, index=True)

    first_seen_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, nullable=False)
    samples_count = Column(Integer, nullable=False, default=0)

    # Local high since we started watching this contract, and the
    # lowest price observed AFTER that high (the "dump bottom"). Reset
    # whenever a new local high is set, so the revival window always
    # measures the most recent dump/recovery cycle, not a stale one.
    local_high_price = Column(Float, nullable=True)
    local_high_at = Column(DateTime, nullable=True)
    local_low_price_since_high = Column(Float, nullable=True)
    local_low_at = Column(DateTime, nullable=True)

    last_price = Column(Float, nullable=True)
    last_liquidity = Column(Float, nullable=True)
    last_volume_1h = Column(Float, nullable=True)

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return (
            f"<RobinhoodTokenWatch {self.token_contract} "
            f"high={self.local_high_price} low={self.local_low_price_since_high} "
            f"samples={self.samples_count}>"
        )
