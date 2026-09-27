"""
hoodalpha/models.py

HoodAlpha's own tables, all in the `hoodalpha` Postgres schema (created
by hoodalpha/bootstrap.py) inside the same Postgres instance WhaleAlpha
uses. Registered against HoodBase (hoodalpha/db.py), a separate
DeclarativeBase from WhaleAlpha's infra.db.session.Base -- these models
never appear in WhaleAlpha's `models/__init__.py` or its Alembic
autogenerate diffs.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Integer, String, Float, DateTime, ForeignKey, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hoodalpha.db import HoodBase

SCHEMA = "hoodalpha"


class HoodWatch(HoodBase):
    """One row per (contract, chain) HoodAlpha has started tracking,
    seeded from a WhaleAlpha SignalToken whose Signal Alert was
    confirmed delivered (see hoodalpha/ingest.py).

    status lifecycle:
      watching       -> newly ingested, or re-armed after a prior alert;
                         waiting for a dip of at least HOOD_MIN_DIP_PCT
                         below entry_alert_price.
      dip_confirmed  -> a meaningful dip has been recorded; now watching
                         for a HOOD_RISE_THRESHOLD_PCT rise off
                         lowest_dip_price.
      alerted        -> a HoodAlert has fired for the current
                         lowest_dip_price. Immediately re-armed back to
                         'watching' with lowest_dip_price reset to the
                         current price, so a later, genuinely new dip
                         cycle can still fire its own alert (guarded by
                         HoodAlert's own unique constraint below).
      stale          -> no usable price for HOOD_STALE_AFTER_HOURS;
                         excluded from further polling.
    """

    __tablename__ = "watch"
    __table_args__ = (
        UniqueConstraint("contract", "chain", name="uq_hood_watch_contract_chain"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    contract: Mapped[str] = mapped_column(String, nullable=False, index=True)
    chain: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # Traceability only -- plain data, not a real cross-schema FK, so a
    # WhaleAlpha-side delete/archive of the source row can never cascade
    # into HoodAlpha.
    source_signal_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    source_name: Mapped[str | None] = mapped_column(String, nullable=True)

    entry_alert_price: Mapped[float] = mapped_column(Float, nullable=False)

    lowest_dip_price: Mapped[float] = mapped_column(Float, nullable=False)
    lowest_dip_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String, nullable=False, default="watching")

    created_at = mapped_column(DateTime, server_default=func.now())
    updated_at = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    alerts = relationship("HoodAlert", back_populates="watch", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<HoodWatch {self.contract}/{self.chain} status={self.status}>"


class HoodAlert(HoodBase):
    """The dedup guard: one HoodAlpha signal per (watch, dip_price)
    pair. If price dips to a NEW, lower low after this fires, that is a
    legitimately new dip cycle with a different dip_price and is
    allowed to alert again once it recovers 20% off that new low."""

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("watch_id", "dip_price", name="uq_hood_alert_watch_dip"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    watch_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(f"{SCHEMA}.watch.id"), nullable=False, index=True
    )

    dip_price: Mapped[float] = mapped_column(Float, nullable=False)
    alert_price: Mapped[float] = mapped_column(Float, nullable=False)
    rise_pct: Mapped[float] = mapped_column(Float, nullable=False)

    sent_at = mapped_column(DateTime, server_default=func.now())

    watch = relationship("HoodWatch", back_populates="alerts")

    def __repr__(self) -> str:
        return f"<HoodAlert watch={self.watch_id} rise={self.rise_pct:.1f}%>"


class HoodIngestCursor(HoodBase):
    """Single-row table: the highest WhaleAlpha signal_tokens.id
    hoodalpha/ingest.py has already processed, so each ingest cycle only
    scans NEW rows instead of the entire table."""

    __tablename__ = "ingest_cursor"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_seen_signal_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
