from sqlalchemy import Column, BigInteger, String, Float, Boolean, DateTime, Text, func
from infra.db.session import Base


class PremiumWallet(Base):
    """
    The Smart Money wallet database that powers the Premium Intelligence
    Engine. Every row is a Solana wallet the discovery engine found,
    validated, and is continuously scoring — see
    services/premium_wallet_discovery.py, premium_wallet_scorer.py and
    premium_wallet_maintenance.py.

    status lifecycle:
      candidate  -> just discovered, not yet observed long enough to trust
      active     -> passed validation, eligible to contribute to signal
                    consensus and to be counted in the Smart Wallet DB
      watch      -> active but underperforming recently; on probation
      removed    -> pruned by the maintenance engine (soft delete — kept
                    for historical / audit purposes, excluded from all
                    live engine logic)

    tier is a coarse label derived from reputation_score, used for
    monitoring cadence (elite wallets get checked more often) and for
    consensus weighting (an elite wallet's buy counts for more than a
    watch-tier wallet's buy).
    """

    __tablename__ = "premium_wallets"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    wallet_address = Column(String, nullable=False, unique=True, index=True)
    label = Column(String, nullable=True)

    # --- discovery provenance ---
    source = Column(String, nullable=False, default="unknown")
    # e.g. "kol_provider", "winning_signal_holder", "tracked_wallet_popularity",
    # "manual", "peer_discovery"
    source_detail = Column(String, nullable=True)
    discovered_at = Column(DateTime, server_default=func.now())

    # --- lifecycle ---
    status = Column(String, nullable=False, default="candidate", index=True)
    tier = Column(String, nullable=False, default="candidate")  # elite | core | watch | candidate

    # --- internal-only behavioural archetype tags, comma-separated ---
    # e.g. "smart_money,swing_trader,high_conviction". Set by
    # services/premium_wallet_scorer.py from observed trade behaviour.
    # NEVER surfaced to Free or Premium users — internal classification
    # only, used for future consensus-weighting/analytics.
    classification = Column(String, nullable=True)

    # --- reputation score (0-100) and its sub-components, recomputed on
    #     every scoring pass by services/premium_wallet_scorer.py ---
    reputation_score = Column(Float, nullable=False, default=0.0, index=True)
    profitability_score = Column(Float, nullable=True)
    consistency_score = Column(Float, nullable=True)
    risk_score = Column(Float, nullable=True)
    activity_score = Column(Float, nullable=True)
    holding_behavior_score = Column(Float, nullable=True)
    position_size_score = Column(Float, nullable=True)
    liquidity_preference_score = Column(Float, nullable=True)
    scam_exposure_score = Column(Float, nullable=True)

    # --- rolling trade statistics, maintained by the scorer from
    #     PremiumWalletTrade rows ---
    trades_observed = Column(BigInteger, nullable=False, default=0)
    wins = Column(BigInteger, nullable=False, default=0)
    losses = Column(BigInteger, nullable=False, default=0)
    win_rate = Column(Float, nullable=True)
    avg_roi_pct = Column(Float, nullable=True)
    best_roi_pct = Column(Float, nullable=True)
    worst_roi_pct = Column(Float, nullable=True)
    max_drawdown_pct = Column(Float, nullable=True)
    avg_hold_minutes = Column(Float, nullable=True)
    realized_pnl_usd = Column(Float, nullable=True, default=0.0)
    avg_position_usd = Column(Float, nullable=True)
    avg_entry_liquidity_usd = Column(Float, nullable=True)
    scam_exposure_pct = Column(Float, nullable=True)

    # --- portfolio snapshot, refreshed periodically ---
    wallet_value_usd = Column(Float, nullable=True)
    last_wallet_snapshot_at = Column(DateTime, nullable=True)

    # --- activity tracking for the monitoring loop ---
    last_signature_seen = Column(String, nullable=True)
    last_activity_at = Column(DateTime, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)
    consecutive_empty_checks = Column(BigInteger, nullable=False, default=0)

    # --- consensus contribution ---
    signals_contributed_to = Column(BigInteger, nullable=False, default=0)

    # --- probation / removal bookkeeping ---
    probation_started_at = Column(DateTime, nullable=True)
    removed_at = Column(DateTime, nullable=True)
    removed_reason = Column(String, nullable=True)

    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<PremiumWallet {self.wallet_address} tier={self.tier} score={self.reputation_score}>"
