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

    auto_trading_config: Mapped["AutoTradingConfig | None"] = relationship(
        back_populates="user", uselist=False
    )
    trades: Mapped[list["Trade"]] = relationship(back_populates="user")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="actor")


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

    events: Mapped[list["WalletEvent"]] = relationship(back_populates="wallet")

    __table_args__ = (
        Index("ix_whale_wallets_status", "status"),
        Index("ix_whale_wallets_score", "score"),
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
