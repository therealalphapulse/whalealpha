"""Deterministic wallet scoring algorithm.

Exact port of src/engines/scoring/walletScoring.ts. Produces a 0-100 score used
to rank the admin-curated whale database and to weight a wallet's contribution
to signal confidence. Pure function — no I/O — so it's fully unit-testable
(see tests/unit/test_scoring.py, ported case-for-case from
tests/unit/scoring.test.ts) and swappable for an ML model later without
touching callers.

HARD REQUIREMENT: every weight, threshold, and formula below is copied
verbatim from the TS source. Do not "simplify" or "improve" — a subtle change
here changes real trading outcomes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from whale_alpha.engines._js_compat import js_round


@dataclass(frozen=True)
class WalletMetrics:
    roi_30d: float  # e.g. 0.42 = +42%
    win_rate: float  # 0..1
    pnl_usd_30d: float
    avg_hold_minutes: float
    avg_position_usd: float
    trade_frequency_7d: float  # trades per week
    wallet_age_days: int
    max_drawdown: float  # 0..1, e.g. 0.35 = 35% max drawdown
    trade_success_rate: float  # 0..1


@dataclass(frozen=True)
class WalletScoreResult:
    score: float  # 0..100
    confidence: float  # 0..100 — how much data backs the score
    breakdown: dict[str, float]
    flags: list[str] = field(default_factory=list)


_WEIGHTS = {
    "roi": 0.22,
    "win_rate": 0.18,
    "consistency": 0.15,  # inverse of drawdown
    "success_rate": 0.15,
    "activity": 0.1,  # sane trade frequency, not wash-trading levels
    "age": 0.1,
    "position_discipline": 0.1,
}

# Wallets below this composite score should not remain APPROVED — used by the
# periodic re-scoring job.
MIN_APPROVED_SCORE = 40


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _sigmoid(x: float, midpoint: float, steepness: float) -> float:
    return 1.0 / (1.0 + math.exp(-steepness * (x - midpoint)))


def score_wallet(m: WalletMetrics) -> WalletScoreResult:
    flags: list[str] = []

    # ROI: saturate around +150% so outliers don't dominate.
    roi_component = _clamp01(_sigmoid(m.roi_30d, 0.5, 3))

    win_rate_component = _clamp01(m.win_rate)

    # Consistency rewards low max drawdown.
    consistency_component = _clamp01(1 - m.max_drawdown)

    success_rate_component = _clamp01(m.trade_success_rate)

    # Activity: too few trades = not enough signal; absurdly many = likely wash trading.
    if m.trade_frequency_7d < 1:
        activity_component = 0.2
    elif m.trade_frequency_7d > 150:
        activity_component = 0.1
        flags.append("SUSPECTED_WASH_TRADING_FREQUENCY")
    else:
        activity_component = _clamp01(_sigmoid(m.trade_frequency_7d, 15, 0.15))

    # Age: older wallets with a track record are inherently lower-risk to trust.
    age_component = _clamp01(_sigmoid(m.wallet_age_days, 90, 0.02))
    if m.wallet_age_days < 14:
        flags.append("NEW_WALLET_LOW_TRACK_RECORD")

    # Position discipline: extremely large single positions relative to typical size flag risk.
    if m.avg_position_usd > 0:
        position_discipline_component = _clamp01(
            _sigmoid(m.avg_position_usd, 5000, -0.0004) + 0.5
        )
    else:
        position_discipline_component = 0.3

    breakdown = {
        "roi": roi_component * _WEIGHTS["roi"],
        "win_rate": win_rate_component * _WEIGHTS["win_rate"],
        "consistency": consistency_component * _WEIGHTS["consistency"],
        "success_rate": success_rate_component * _WEIGHTS["success_rate"],
        "activity": activity_component * _WEIGHTS["activity"],
        "age": age_component * _WEIGHTS["age"],
        "position_discipline": position_discipline_component * _WEIGHTS["position_discipline"],
    }

    raw = sum(breakdown.values())  # 0..1
    score = js_round(raw * 100)

    # Confidence in the score itself scales with how much history backs it.
    data_volume_confidence = _clamp01(
        _sigmoid(m.trade_frequency_7d * (m.wallet_age_days / 30), 20, 0.08)
    )
    confidence = js_round(data_volume_confidence * 100)

    if m.max_drawdown > 0.6:
        flags.append("HIGH_DRAWDOWN_RISK")
    if m.trade_success_rate < 0.35:
        flags.append("LOW_SUCCESS_RATE")

    return WalletScoreResult(score=score, confidence=confidence, breakdown=breakdown, flags=flags)
