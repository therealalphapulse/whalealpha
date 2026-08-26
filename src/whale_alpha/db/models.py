"""SQLAlchemy 2.0 async models — port of src/database/prisma/schema.prisma.

Preserves the schema exactly: same models, enums, indexes, relations. Table
names are snake_case (SQLAlchemy convention) but every column preserves the
original semantics; Prisma's `@default(cuid())` is replaced with a Python-side
default that generates a URL-safe, sortable, unique id string (`cuid2`-style),
since cuid has no canonical Python implementation — see PORTING_NOTES.md.

New in the Python port (not present in the TS schema — see requirement #3 of
the porting brief and PORTING_NOTES.md): `TradeStatus` already included
PENDING/SUBMITTED in the original, which this port relies on for restart-safe
reconciliation (see engines/reconciliation.py). No new enum values were added;
we simply *use* the existing states as designed.
"""

from __future__ import annotations

import enum
import secrets
import string
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import ARRAY

_ID_ALPHABET = string.ascii_lowercase + string.digits


def generate_id() -> str:
    """cuid-like id: lowercase alnum, time-independent uniqueness via secrets.

    Not a byte-for-byte cuid implementation (no canonical Python cuid2 lib in
    the stdlib), but satisfies the same practical requirements Prisma's
    `cuid()` gave us: unique, URL-safe, sortable-enough, non-guessable primary
    keys. See PORTING_NOTES.md for why this is flagged as a judgment call.
    """
    return "c" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(24))


class Base(DeclarativeBase):
    pass


class Role(str, enum.Enum):
    USER = "USER"
    ADMIN = "ADMIN"
    SUPERADMIN = "SUPERADMIN"


class WalletStatus(str, enum.Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


class CandidateStatus(str, enum.Enum):
    """Lifecycle of a discovery-engine candidate, kept separate from
    WalletStatus so the (potentially large, noisy) discovery pipeline never
    touches the admin-curated WhaleWallet table until a candidate actually
    clears the promotion bar. See engines/discovery.py.
    """

    NEW = "NEW"  # queued, not yet evaluated
    EVALUATED = "EVALUATED"  # metrics computed, did not clear the bar (yet)
    PROMOTED = "PROMOTED"  # promoted into whale_wallets
    REJECTED = "REJECTED"  # evaluated and disqualified (e.g. too young, wash-trading flags)


class RiskProfile(str, enum.Enum):
    CONSERVATIVE = "CONSERVATIVE"
    BALANCED = "BALANCED"
    AGGRESSIVE = "AGGRESSIVE"


class TradeSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeSource(str, enum.Enum):
    MANUAL = "MANUAL"
    AUTO_SIGNAL = "AUTO_SIGNAL"


class TradeStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    telegram_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.USER, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Encrypted at rest — see utils/security/encryption.py. Never store plaintext.
    encrypted_wallet_key: Mapped[str | None] = mapped_column(String, nullable=True)
    wallet_public_key: Mapped[str | None] = mapped_column(String, nullable=True)

    # NEW (feature: signal -> notification wiring): whether this user wants a
    # Telegram DM when a new Signal is generated. Defaults on so existing
    # behavior (everyone gets notified) matches what the bot's /start message
    # already promises; toggle with /mute and /unmute.
    notify_signals: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    auto_trading_config: Mapped["AutoTradingConfig | None"] = relationship(
        back_populates="user", uselist=False
    )
    trades: Mapped[list["Trade"]] = relationship(back_populates="user")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="actor")
    price_alerts: Mapped[list["PriceAlert"]] = relationship(back_populates="user")


class WhaleWallet(Base):
    """Admin-curated wallet database.

    Users have read-only access via the bot/API; only ADMIN/SUPERADMIN roles
    may mutate this table (enforced in services/admin).
    """

    __tablename__ = "whale_wallets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    address: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[WalletStatus] = mapped_column(
        Enum(WalletStatus), default=WalletStatus.PENDING_REVIEW, nullable=False
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)

    # Scoring inputs / outputs (see engines/scoring.py)
    score: Mapped[float] = mapped_column(Float, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    roi_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_usd_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_hold_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_position_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    trade_frequency_7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    wallet_age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    trade_success_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_preference: Mapped[str | None] = mapped_column(String, nullable=True)

    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    added_by_admin_id: Mapped[str | None] = mapped_column(String, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # --- NEW (feature: Whale Wallet Discovery & Intelligence Engine) ---
    # Distinguishes wallets the discovery engine found and auto-promoted from
    # ones a human admin added via /addwhale, without needing a second table
    # join for the common "who/what put this here" question. Defaults false
    # for wallets that predate this feature (admin-added, or seeded).
    auto_discovered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    discovery_source: Mapped[str | None] = mapped_column(String, nullable=True)
    # Consecutive re-scoring cycles this wallet has scored below the approval
    # bar. Used for hysteresis (see engines/discovery.py) so one noisy/bad
    # cycle doesn't immediately retire an otherwise-good wallet.
    consecutive_low_score_cycles: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["WalletEvent"]] = relationship(back_populates="wallet")

    __table_args__ = (
        Index("ix_whale_wallets_status", "status"),
        Index("ix_whale_wallets_score", "score"),
    )


class WalletCandidate(Base):
    """Discovery-engine staging table — new feature, no TS equivalent existed.

    Candidate Solana addresses surfaced by
    integrations/wallet_discovery_source.py land here first, get their
    on-chain history fetched + scored (engines/discovery.py), and only cross
    into the admin-curated `whale_wallets` table if they clear the promotion
    bar. Keeping this separate means:
      * the (much larger, noisier) discovery funnel never touches the table
        the signal engine and admin RBAC care about, and
      * we don't re-fetch + re-score the same candidate address every cycle
        (rate-limit friendly) — `last_evaluated_at` gates re-evaluation.
    """

    __tablename__ = "wallet_candidates"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    address: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "helius_token_holders", "co_buyer"
    status: Mapped[CandidateStatus] = mapped_column(
        Enum(CandidateStatus), default=CandidateStatus.NEW, nullable=False
    )
    discovered_from_token_mint: Mapped[str | None] = mapped_column(String, nullable=True)

    last_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    # --- NEW (feature: Hybrid Wallet Discovery Engine, Phase 1 refactor) ---
    # On-chain behaviour scores (engines/behavior_scoring.py — Early Buyer,
    # Diamond Hand, Quick Flip, Sniper Probability, Conviction, Consistency,
    # Risk) and the smart-money labels derived from them
    # (engines/wallet_labels.py). Stored on the candidate so a promoted
    # wallet's labels/behaviour are available immediately (copied onto
    # WhaleWallet.tags at promotion) without recomputation.
    behavior_scores: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    labels: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)

    evaluation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_wallet_id: Mapped[str | None] = mapped_column(ForeignKey("whale_wallets.id"), nullable=True)

    # --- NEW (production fix: wallet-history retry queue, rate-limit
    # resilience — see engines/discovery.evaluate_candidates and
    # utils/http_retry.py) ---
    # Counts transient (429/5xx/network) history-fetch failures only — a
    # definitive "no provider configured"/"address not found" never
    # increments this, since retrying those can never succeed.
    # `next_retry_at` is a short, exponentially-growing backoff window
    # (independent of the multi-hour `last_evaluated_at` re-evaluation
    # cutoff above) — evaluate_candidates re-picks up a candidate once this
    # elapses instead of waiting for the next full re-evaluation window.
    # Once `history_retry_count` exceeds
    # DISCOVERY_HISTORY_MAX_RETRIES_BEFORE_REJECT the candidate is
    # permanently marked EVALUATED/NO_HISTORY instead of retried forever.
    history_retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_wallet_candidates_status", "status"),
        Index("ix_wallet_candidates_last_evaluated_at", "last_evaluated_at"),
        Index("ix_wallet_candidates_next_retry_at", "next_retry_at"),
    )


class WalletRelationship(Base):
    """Wallet graph — new feature (Hybrid Wallet Discovery Engine, Phase 1
    refactor). Deliberately a plain Postgres table, not a graph database
    (per the architecture requirements): a relationship is just "these two
    addresses have repeatedly shown up trading the same tokens", which a
    unique-pair row with a running counter models perfectly well at the
    scale this engine targets (thousands of wallets, not millions of edges).

    One row per (wallet_address, related_address) pair. `co_occurrence_count`
    increments every discovery cycle the pair is seen sharing a *new* token
    mint (see engines/wallet_graph.py) — repeated co-trading across distinct
    tokens is much stronger evidence than one shared hot token, which is why
    candidates sourced from this table require
    `DISCOVERY_GRAPH_MIN_COOCCURRENCE` before being queued (see
    engines/discovery.py). `strength` is a normalized 0..1 confidence value
    derived from that count, consumed as an enrichment signal only — it
    never gates promotion on its own (see evaluate_promotion).
    """

    __tablename__ = "wallet_relationships"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    wallet_address: Mapped[str] = mapped_column(String, nullable=False)
    related_address: Mapped[str] = mapped_column(String, nullable=False)
    relationship_type: Mapped[str] = mapped_column(String, nullable=False)  # e.g. CO_BUY, CO_SELL, CO_TIMING
    co_occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    shared_token_mints: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    strength: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("wallet_address", "related_address", name="uq_wallet_relationships_pair"),
        Index("ix_wallet_relationships_wallet_address", "wallet_address"),
        Index("ix_wallet_relationships_related_address", "related_address"),
    )


class WalletEvent(Base):
    """Raw accumulation events observed by the monitor engine (a wallet buying a token)."""

    __tablename__ = "wallet_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    wallet_id: Mapped[str] = mapped_column(ForeignKey("whale_wallets.id"), nullable=False)
    wallet: Mapped[WhaleWallet] = relationship(back_populates="events")
    token_mint: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide), nullable=False)
    amount_tokens: Mapped[float] = mapped_column(Float, nullable=False)
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    tx_signature: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_wallet_events_token_mint_observed_at", "token_mint", "observed_at"),
        Index("ix_wallet_events_wallet_id_observed_at", "wallet_id", "observed_at"),
    )


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    token_mint: Mapped[str] = mapped_column(String, nullable=False)
    token_symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    wallet_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_capital_usd: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    risk_level: Mapped[str] = mapped_column(String, nullable=False)
    entry_zone_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_zone_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_recommendation: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    trades: Mapped[list["Trade"]] = relationship(back_populates="signal")

    __table_args__ = (Index("ix_signals_token_mint_created_at", "token_mint", "created_at"),)


class AutoTradingConfig(Base):
    __tablename__ = "auto_trading_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    user: Mapped[User] = relationship(back_populates="auto_trading_config")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    fixed_trade_amount_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    percent_allocation: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_slippage_bps: Mapped[int] = mapped_column(Integer, default=150)
    max_market_cap_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_liquidity_usd: Mapped[float] = mapped_column(Float, default=10000)
    stop_loss_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_open_positions: Mapped[int] = mapped_column(Integer, default=5)
    max_daily_trades: Mapped[int] = mapped_column(Integer, default=10)
    max_daily_exposure_usd: Mapped[float] = mapped_column(Float, default=500)
    token_blacklist: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=15)
    risk_profile: Mapped[RiskProfile] = mapped_column(Enum(RiskProfile), default=RiskProfile.BALANCED)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    user: Mapped[User] = relationship(back_populates="trades")
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    signal: Mapped[Signal | None] = relationship(back_populates="trades")
    source: Mapped[TradeSource] = mapped_column(Enum(TradeSource), nullable=False)
    side: Mapped[TradeSide] = mapped_column(Enum(TradeSide), nullable=False)
    token_mint: Mapped[str] = mapped_column(String, nullable=False)
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    amount_tokens: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[TradeStatus] = mapped_column(Enum(TradeStatus), default=TradeStatus.PENDING)
    tx_signature: Mapped[str | None] = mapped_column(String, nullable=True)
    slippage_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- NEW (not in the original TS schema): restart-safe reconciliation fields.
    # See engines/reconciliation.py and PORTING_NOTES.md requirement #3. These
    # let a PENDING/SUBMITTED trade be re-checked against Solana after a crash
    # or redeploy, instead of being silently orphaned.
    last_blockhash: Mapped[str | None] = mapped_column(String, nullable=True)
    last_valid_block_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciliation_attempts: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_trades_user_id_created_at", "user_id", "created_at"),)


class AlertDirection(str, enum.Enum):
    UP = "UP"
    DOWN = "DOWN"
    BOTH = "BOTH"


class PriceAlert(Base):
    """NEW (feature: % price-increase alerts) — user-configured watch on a
    token's price. Not present in the original TS schema; there was no code
    path for this feature at all before this port.

    `reference_price_usd` is the baseline the percent move is measured
    against. It's set when the alert is created (or when it last fired, if
    `reset_on_trigger` is true) rather than recomputed from scratch each tick,
    so "up 20%" always means 20% from a fixed point, not a rolling window.
    """

    __tablename__ = "price_alerts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    user: Mapped[User] = relationship(back_populates="price_alerts")
    token_mint: Mapped[str] = mapped_column(String, nullable=False)
    threshold_pct: Mapped[float] = mapped_column(Float, nullable=False)
    direction: Mapped[AlertDirection] = mapped_column(
        Enum(AlertDirection), default=AlertDirection.BOTH, nullable=False
    )
    reference_price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    reset_on_trigger: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_price_alerts_active_token_mint", "active", "token_mint"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    actor: Mapped[User] = relationship(back_populates="audit_logs")
    action: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_audit_logs_actor_id_created_at", "actor_id", "created_at"),)


class WhaleAlphaAudit(Base):
    """Immutable internal release-assurance record for every Whale Alpha candidate audit."""

    __tablename__ = "whale_alpha_audits"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    analysis_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String, nullable=False)
    strategy_version: Mapped[str] = mapped_column(String, nullable=False)
    rules_version: Mapped[str] = mapped_column(String, nullable=False)
    scoring_model_version: Mapped[str] = mapped_column(String, nullable=False)
    audit_mode: Mapped[str] = mapped_column(String, nullable=False)
    token_mint: Mapped[str] = mapped_column(String, nullable=False)
    pair_address: Mapped[str | None] = mapped_column(String, nullable=True)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    final_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    final_tier: Mapped[str] = mapped_column(String, nullable=False, default="NO SIGNAL")
    findings: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    corrections: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_whale_alpha_audits_token_created_at", "token_mint", "created_at"),
        Index("ix_whale_alpha_audits_approved_created_at", "approved", "created_at"),
    )


class TokenOpportunity(Base):
    """Persisted token-hunter observation and alert/outcome record."""

    __tablename__ = "token_opportunities"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    mint: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    detection_source: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="SCORED", nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    age_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_5m_usd: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    volume_1h_usd: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    buys_5m: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sells_5m: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    buys_1h: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sells_1h: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    score_breakdown: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String, nullable=True)
    risk_flags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    key_reasons: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    alert_status: Mapped[str] = mapped_column(String, default="NOT_ATTEMPTED", nullable=False)
    alert_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alert_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alert_error: Mapped[str | None] = mapped_column(String, nullable=True)
    alert_reference_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    alert_message_ids: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    quote_milestones: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    mc_after_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    mc_after_15m: Mapped[float | None] = mapped_column(Float, nullable=True)
    mc_after_30m: Mapped[float | None] = mapped_column(Float, nullable=True)
    mc_after_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_mc_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_mc_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    snapshots: Mapped[list["TokenSnapshot"]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_token_opportunities_status_detected_at", "status", "detected_at"),
        Index("ix_token_opportunities_score", "score"),
    )


class TokenSnapshot(Base):
    """Compact market snapshot used for outcome analysis."""

    __tablename__ = "token_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("token_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    opportunity: Mapped[TokenOpportunity] = relationship(back_populates="snapshots")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    market_cap_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_5m_usd: Mapped[float] = mapped_column(Float, default=0, nullable=False)

    __table_args__ = (Index("ix_token_snapshots_opportunity_observed_at", "opportunity_id", "observed_at"),)


class DiscoveryScanProgress(Base):
    """Blockchain-first discovery engine — Phase 1. Persists the last Solana
    slot the block scanner has fully processed, so it can page through
    recent blocks in bounded batches (never the whole chain at once — see
    integrations/chain_scanner.py) and resume exactly where it left off
    after a restart instead of re-scanning or silently skipping slots.

    One row per `cluster` (mainnet-beta / devnet / testnet), so switching
    `SOLANA_CLUSTER` doesn't corrupt another cluster's progress. In
    practice this repo only ever runs one cluster at a time, but keeping
    `cluster` as the natural key costs nothing and avoids a footgun.
    """

    __tablename__ = "discovery_scan_progress"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=generate_id)
    cluster: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    last_processed_slot: Mapped[int] = mapped_column(Integer, nullable=False)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
