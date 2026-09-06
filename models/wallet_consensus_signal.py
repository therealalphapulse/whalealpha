from sqlalchemy import Column, BigInteger, String, Float, Integer, DateTime, Text, func
from infra.db.session import Base


class WalletConsensusSignal(Base):
    """
    Discovery Engine A (Solana Profitable Wallet Consensus) signal
    history and dedupe/cooldown ledger — mirrors the proven shape of
    models/pump_alerted_token.py (the free Signal Engine's own dedupe
    table) rather than inventing a new convention.

    One row per token that has ever crossed the wallet-consensus
    threshold and been alerted; contributing-wallet evidence is captured
    at the moment of the FIRST qualifying threshold crossing for each
    distinct consensus event (see wallet_consensus_engine.py) and is
    never mutated afterward, so historical evidence always reflects
    exactly what triggered that alert.
    """

    __tablename__ = "wallet_consensus_signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    token_contract = Column(String, nullable=False, unique=True, index=True)
    token_symbol = Column(String, nullable=True)
    token_name = Column(String, nullable=True)
    chain = Column(String, nullable=False, default="solana")

    # --- consensus evidence (persisted exactly as required by the
    #     WhaleAlpha spec: "persist the exact wallet addresses and
    #     transaction evidence that contributed to the 3-wallet
    #     threshold") ---
    wallet_count = Column(Integer, nullable=False)
    wallet_addresses_json = Column(Text, nullable=True)  # ["addr1", "addr2", ...]
    wallet_classifications_json = Column(Text, nullable=True)  # {"addr1": "profitable_trader,smart_money", ...}
    transaction_evidence_json = Column(Text, nullable=True)  # [{"wallet":..,"signature":..,"detected_at":..,"amount":..}, ...]
    coordinated_cluster_json = Column(Text, nullable=True)  # funding-graph evidence, if any wallets share a funder
    avg_wallet_reputation = Column(Float, nullable=True)
    observation_window_minutes = Column(Float, nullable=True)

    confidence_score = Column(Float, nullable=True)
    snapshot_json = Column(Text, nullable=True)  # rich-card market snapshot at signal time

    # --- dedupe / cooldown / re-arm (same convention as PumpAlertedToken) ---
    status = Column(String, nullable=False, default="active")
    times_alerted = Column(Integer, nullable=False, default=1)
    first_alerted_at = Column(DateTime, nullable=True)
    last_alerted_at = Column(DateTime, nullable=True)
    cooldown_expires_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<WalletConsensusSignal {self.token_contract} wallets={self.wallet_count}>"
