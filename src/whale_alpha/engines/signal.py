"""Signal engine: turns clustered whale accumulation into a confidence-scored Signal.

Exact port of src/engines/signal/signalEngine.ts. Pure logic over inputs the
monitor engine gathers — it does not query the chain itself, keeping it
deterministic and unit-testable (see tests/unit/test_signal_engine.py, ported
case-for-case from tests/unit/signalEngine.test.ts).

HARD REQUIREMENT: every weight, threshold, and formula below matches the TS
source exactly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from whale_alpha.engines._js_compat import js_round

RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]


@dataclass
class WhaleAccumulationEvent:
    wallet_id: str
    wallet_score: float  # 0..100, from scoring
    token_mint: str
    amount_usd: float
    observed_at: datetime


@dataclass
class TokenSafetyContext:
    market_cap_usd: float
    liquidity_usd: float
    holder_concentration_top10_pct: float  # 0..1
    lp_locked: bool
    mint_authority_revoked: bool
    freeze_authority_revoked: bool


@dataclass
class EntryZone:
    low: float
    high: float


@dataclass
class SignalCandidate:
    token_mint: str
    wallet_count: int
    total_capital_usd: float
    confidence_score: float  # 0..100
    risk_level: RiskLevel
    entry_zone: EntryZone | None
    ai_recommendation: str
    contributing_wallets: list[str] = field(default_factory=list)


@dataclass
class SignalEngineConfig:
    min_wallets: int
    window_minutes: float
    min_confidence: float


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def evaluate_token_cluster(
    token_mint: str,
    events: list[WhaleAccumulationEvent],
    safety: TokenSafetyContext | None,
    config: SignalEngineConfig,
) -> SignalCandidate | None:
    """Groups events into a time window per token and computes a confidence score.

    Call this periodically (e.g. every 30s from the scheduler) over the recent
    event buffer.
    """
    now_ms = time.time() * 1000
    window_ms = config.window_minutes * 60 * 1000

    recent_events = [
        e
        for e in events
        if e.token_mint == token_mint and now_ms - e.observed_at.timestamp() * 1000 <= window_ms
    ]

    unique_wallets = {e.wallet_id for e in recent_events}
    if len(unique_wallets) < config.min_wallets:
        return None

    total_capital_usd = sum(e.amount_usd for e in recent_events)

    # Weight each wallet's contribution to confidence by its own quality score.
    avg_wallet_score = sum(e.wallet_score for e in recent_events) / len(recent_events)

    # Timing correlation: tighter clustering in time = stronger signal.
    timestamps = sorted(e.observed_at.timestamp() * 1000 for e in recent_events)
    spread_ms = timestamps[-1] - timestamps[0]
    timing_correlation = 1 - _clamp(spread_ms / window_ms, 0, 1)  # 1 = simultaneous, 0 = spread across whole window

    wallet_count_boost = _clamp((len(unique_wallets) - config.min_wallets) / 10, 0, 1)

    safety_component = 0.5  # neutral if unknown
    risk_flags: list[str] = []
    if safety is not None:
        s = 0.0
        n = 0
        s += 1 if safety.lp_locked else 0
        n += 1
        s += 1 if safety.mint_authority_revoked else 0
        n += 1
        s += 1 if safety.freeze_authority_revoked else 0
        n += 1
        s += _clamp(1 - safety.holder_concentration_top10_pct / 0.5, 0, 1)
        n += 1
        s += _clamp(safety.liquidity_usd / 50000, 0, 1)
        n += 1
        safety_component = s / n

        if not safety.lp_locked:
            risk_flags.append("LP_NOT_LOCKED")
        if not safety.mint_authority_revoked:
            risk_flags.append("MINT_AUTHORITY_ACTIVE")
        if safety.holder_concentration_top10_pct > 0.5:
            risk_flags.append("HIGH_HOLDER_CONCENTRATION")
        if safety.liquidity_usd < 10000:
            risk_flags.append("LOW_LIQUIDITY")

    confidence_score = js_round(
        _clamp(
            (avg_wallet_score / 100) * 0.4
            + timing_correlation * 0.2
            + wallet_count_boost * 0.15
            + safety_component * 0.25,
            0,
            1,
        )
        * 100
    )

    if confidence_score < config.min_confidence:
        return None

    if len(risk_flags) == 0:
        risk_level: RiskLevel = "LOW"
    elif len(risk_flags) <= 1:
        risk_level = "MEDIUM"
    else:
        risk_level = "HIGH"

    if risk_level == "LOW" and confidence_score >= 80:
        ai_recommendation = (
            "Strong consensus, favorable safety profile. Consider standard "
            "position sizing per your risk rules."
        )
    elif risk_level == "HIGH":
        ai_recommendation = (
            "High-confidence whale consensus but material safety flags present — "
            "reduce size or skip."
        )
    else:
        ai_recommendation = "Moderate consensus. Position within your normal risk limits and monitor closely."

    return SignalCandidate(
        token_mint=token_mint,
        wallet_count=len(unique_wallets),
        total_capital_usd=total_capital_usd,
        confidence_score=confidence_score,
        risk_level=risk_level,
        entry_zone=None,  # filled in by caller once current price is fetched from price feed
        ai_recommendation=ai_recommendation,
        contributing_wallets=list(unique_wallets),
    )
