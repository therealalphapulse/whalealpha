from sqlalchemy import Column, BigInteger, String, Float, DateTime, Text, func
from infra.db.session import Base


class PremiumSignal(Base):
    """
    A Premium signal — only created when a token passes BOTH gates
    (services/premium_signal_engine.py):
      1. AlphaPulse AI analysis (services/conviction_scorer.score_candidate)
      2. Smart Wallet consensus (>= PREMIUM_CONSENSUS_MIN_WALLETS distinct
         active Premium wallets buying the same token within
         PREMIUM_CONSENSUS_WINDOW_MINUTES of each other)

    Kept as its own table (separate from models/signal_token.py) so the
    free-tier Signal Engine is completely untouched — Premium signals are
    an additive, higher-bar layer on top, not a modification of it.
    """

    __tablename__ = "premium_signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    token_mint = Column(String, nullable=False, index=True)
    token_name = Column(String, nullable=True)
    token_symbol = Column(String, nullable=True)

    # --- AI analysis snapshot ---
    ai_score = Column(Float, nullable=True)
    ai_tier = Column(String, nullable=True)
    ai_breakdown_json = Column(Text, nullable=True)

    # --- consensus snapshot ---
    consensus_wallet_count = Column(BigInteger, nullable=False, default=0)
    consensus_wallet_addresses_json = Column(Text, nullable=True)
    consensus_avg_reputation = Column(Float, nullable=True)
    consensus_window_minutes = Column(Float, nullable=True)

    # --- combined confidence (weighted blend of ai_score and consensus
    #     strength) used to rank premium signals against each other ---
    confidence_score = Column(Float, nullable=False, default=0.0, index=True)

    entry_price = Column(Float, nullable=True)
    entry_market_cap = Column(Float, nullable=True)
    entry_liquidity = Column(Float, nullable=True)

    current_price = Column(Float, nullable=True)
    current_market_cap = Column(Float, nullable=True)
    current_multiple = Column(Float, nullable=True, default=1.0)
    ath_multiple = Column(Float, nullable=True, default=1.0)

    pair_url = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active")  # active | archived

    message_ids_json = Column(Text, nullable=True, default="{}")

    signaled_at = Column(DateTime, server_default=func.now(), index=True)
    last_checked_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<PremiumSignal {self.token_symbol} conf={self.confidence_score}>"
