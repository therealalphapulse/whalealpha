"""Unit tests for engines/behavior_scoring.py — pure functions, no I/O."""

from __future__ import annotations

from whale_alpha.engines.behavior_scoring import (
    MAX_BEHAVIOR_CONFIDENCE_BONUS,
    behavior_confidence_bonus,
    behavior_scores_to_json,
    compute_behavior_scores,
)
from whale_alpha.engines.scoring import WalletMetrics

_DISCIPLINED_METRICS = WalletMetrics(
    roi_30d=0.8,
    win_rate=0.75,
    pnl_usd_30d=50000,
    avg_hold_minutes=1000,
    avg_position_usd=6000,
    trade_frequency_7d=8,
    wallet_age_days=300,
    max_drawdown=0.1,
    trade_success_rate=0.75,
)

_SNIPER_LIKE_METRICS = WalletMetrics(
    roi_30d=0.05,
    win_rate=0.3,
    pnl_usd_30d=500,
    avg_hold_minutes=2,
    avg_position_usd=200,
    trade_frequency_7d=140,
    wallet_age_days=5,
    max_drawdown=0.8,
    trade_success_rate=0.2,
)


def test_disciplined_wallet_scores_high_on_conviction_and_consistency():
    scores = compute_behavior_scores(_DISCIPLINED_METRICS, swaps=[])
    assert scores.conviction_score > 50
    assert scores.consistency_score > 50
    assert scores.diamond_hand_score > 50


def test_sniper_like_wallet_scores_high_on_sniper_probability_and_risk():
    scores = compute_behavior_scores(_SNIPER_LIKE_METRICS, swaps=[])
    assert scores.sniper_probability > 50
    assert scores.risk_score > 50
    # A bot-like spray of tiny fast trades should not read as "diamond hands".
    assert scores.diamond_hand_score < scores.sniper_probability


def test_all_scores_are_bounded_0_to_100():
    for metrics in (_DISCIPLINED_METRICS, _SNIPER_LIKE_METRICS):
        scores = compute_behavior_scores(metrics, swaps=[])
        for value in behavior_scores_to_json(scores).values():
            assert 0 <= value <= 100


def test_behavior_confidence_bonus_is_bounded_and_directionally_sane():
    disciplined_bonus = behavior_confidence_bonus(compute_behavior_scores(_DISCIPLINED_METRICS, swaps=[]))
    risky_bonus = behavior_confidence_bonus(compute_behavior_scores(_SNIPER_LIKE_METRICS, swaps=[]))

    assert -MAX_BEHAVIOR_CONFIDENCE_BONUS <= disciplined_bonus <= MAX_BEHAVIOR_CONFIDENCE_BONUS
    assert -MAX_BEHAVIOR_CONFIDENCE_BONUS <= risky_bonus <= MAX_BEHAVIOR_CONFIDENCE_BONUS
    # A disciplined, low-risk wallet should never score worse than a
    # sniper-like, high-drawdown one on this enrichment signal.
    assert disciplined_bonus > risky_bonus
