"""domain/trading/auto_trade/signal_adapter.py

§3 of the spec: normalizes an already-qualified AlphaPulse signal
(models/signal_token.py::SignalToken) into a stable internal
AutoTradeSignal representation for the rest of this engine to consume.

READ-ONLY with respect to SignalToken / the signal engine. This module
never writes to signal_tokens, never scores or discovers tokens, and
never bypasses the existing qualification/delivery gate
(status == "active" and alert_delivered == True) that
domain/trading/real/real_automation_engine.py also requires -- this is
the same "qualifying signal" definition the existing AutoBuy uses, not
a second one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from infra.db.session import async_session
from models.signal_token import SignalToken

from .constants import SIGNAL_LOOKBACK_MINUTES


@dataclass(frozen=True)
class AutoTradeSignal:
    """Internal, stable representation of a qualifying signal (§3)."""

    signal_id: int
    contract: str
    symbol: str
    name: str
    chain: str
    score: float | None
    tier: str
    price: float | None
    market_cap: float | None
    liquidity: float | None
    detected_at: datetime | None
    was_redelivered: bool
    metadata: dict


def score_to_tier(score: float | None) -> str:
    """AlphaPulse's SignalToken has no discrete `tier` column (see
    models/signal_token.py) -- score is the underlying conviction
    signal. This bucketing is this engine's own, purely for the
    allowed_signal_tiers policy filter (§4/§6); it does not change or
    read any tier concept from the signal engine itself, and the
    signal engine does not need to know it exists.
    """
    if score is None:
        return "UNKNOWN"
    if score >= 85:
        return "HIGH"
    if score >= 65:
        return "MEDIUM"
    return "LOW"


def normalize_signal(signal: SignalToken) -> AutoTradeSignal:
    """AlphaPulse Signal -> AutoTradeSignal (§3)."""
    score = signal.entry_score
    price = signal.current_price or signal.entry_price
    market_cap = signal.current_market_cap or signal.entry_market_cap
    liquidity = signal.current_liquidity or signal.entry_liquidity
    return AutoTradeSignal(
        signal_id=signal.id,
        contract=signal.contract,
        symbol=signal.symbol or "???",
        name=signal.name or "",
        chain="robinhood",
        score=score,
        tier=score_to_tier(score),
        price=price,
        market_cap=market_cap,
        liquidity=liquidity,
        detected_at=signal.signaled_at,
        was_redelivered=bool(signal.was_redelivered),
        metadata={
            "bundle_pct": signal.bundle_pct,
            "dev_holding_pct": signal.dev_holding_pct,
            "total_holders": signal.total_holders,
        },
    )


async def get_recent_qualifying_signals(limit: int = 50) -> list[AutoTradeSignal]:
    """Same read-only query shape as
    real_automation_engine._get_recent_active_signals: only signals
    that are active AND whose Signal Alert actually reached
    subscribers (alert_delivered == True), within the lookback window.
    This is the sole discovery mechanism for this engine -- it never
    queries anything else, and never triggers on another wallet's
    activity (no copy-trading, §46).
    """
    # SignalToken.signaled_at is TIMESTAMP WITHOUT TIME ZONE (naive) in
    # Postgres; asyncpg raises DataError on a tz-aware bind parameter
    # against a naive column ("can't subtract offset-naive and
    # offset-aware datetimes"), which was silently killing every single
    # scan_and_authorize() tick before it could read a single signal.
    # .replace(tzinfo=None) matches the convention already used for
    # this exact reason elsewhere in this engine (orchestrator.py,
    # exit_engine.py, reconciliation.py, risk_gate.py).
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=SIGNAL_LOOKBACK_MINUTES)
    async with async_session() as session:
        result = await session.execute(
            select(SignalToken)
            .where(
                SignalToken.status == "active",
                SignalToken.signaled_at >= cutoff,
                SignalToken.alert_delivered == True,  # noqa: E712
            )
            .order_by(SignalToken.signaled_at.desc())
            .limit(limit)
        )
        rows = result.scalars().all()
    return [normalize_signal(s) for s in rows]
