from sqlalchemy import Column, BigInteger, String, Float, Integer, DateTime, Text, func
from infra.db.session import Base


class RobinhoodDiscoverySignal(Base):
    """
    Discovery Engine B (Robinhood Chain Token Discovery via DexScreener)
    signal history and dedupe/cooldown ledger. Independent of every
    Solana wallet-consensus table above — Robinhood Chain signals never
    require and never reference wallet consensus (WhaleAlpha spec:
    "Do not accidentally require wallet consensus for Robinhood
    tokens").
    """

    __tablename__ = "robinhood_discovery_signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    token_contract = Column(String, nullable=False, unique=True, index=True)
    pair_address = Column(String, nullable=True)
    token_symbol = Column(String, nullable=True)
    token_name = Column(String, nullable=True)
    chain = Column(String, nullable=False, default="robinhood")

    discovery_source = Column(String, nullable=False)  # "dexscreener_new" | "dexscreener_renewed"
    discovery_score = Column(Float, nullable=False)
    score_breakdown_json = Column(Text, nullable=True)
    reasons_json = Column(Text, nullable=True)  # human-readable "why selected" evidence

    snapshot_json = Column(Text, nullable=True)

    status = Column(String, nullable=False, default="active")
    times_alerted = Column(Integer, nullable=False, default=1)
    first_alerted_at = Column(DateTime, nullable=True)
    last_alerted_at = Column(DateTime, nullable=True)
    cooldown_expires_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<RobinhoodDiscoverySignal {self.token_contract} score={self.discovery_score}>"
