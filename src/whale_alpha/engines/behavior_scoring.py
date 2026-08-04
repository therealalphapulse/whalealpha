"""On-chain behaviour analysis — Hybrid Discovery Engine Priority 6.

New module (Phase 1 refactor). Scores a candidate wallet on *behaviour*
patterns rather than raw profitability — engines/scoring.score_wallet already
covers profitability (ROI, win rate, drawdown, etc.) and is explicitly
NOT modified here. These scores are a separate, additive enrichment layer:
they feed engines/wallet_labels.assign_labels and a small, bounded confidence
adjustment (see engines/discovery.py), never the core score or the hard
promotion gates in evaluate_promotion.

Every score below is a deterministic function of real, already-computed
signals (engines/discovery_metrics.ComputedMetrics + the raw swap list) — no
invented data, no fabricated history. Because none of our free-tier sources
give us "this wallet bought within N seconds of pool creation" ground truth,
several of these are necessarily heuristic proxies built from the metrics we
*do* have (hold time, trade frequency, drawdown, position sizing). That's
flagged per-score below; treat them as ranking signals, not certified facts.

Pure functions only — no I/O — so this is fully unit-testable without a
database or network, same shape as scoring.py / discovery_metrics.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from whale_alpha.engines.scoring import WalletMetrics
from whale_alpha.integrations.wallet_discovery_source import WalletSwap


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _sigmoid(x: float, midpoint: float, steepness: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-steepness * (x - midpoint)))
    except OverflowError:
        return 0.0 if steepness * (x - midpoint) < 0 else 1.0


@dataclass(frozen=True)
class BehaviorScores:
    """All fields 0..100. Higher is not always "better" — e.g. a high
    sniper_probability or risk_score is a caution flag, not praise."""

    early_buyer_score: float
    diamond_hand_score: float
    quick_flip_score: float
    sniper_probability: float
    conviction_score: float
    consistency_score: float
    risk_score: float


def compute_behavior_scores(
    metrics: WalletMetrics,
    swaps: list[WalletSwap],
) -> BehaviorScores:
    """Derives BehaviorScores from a candidate's computed WalletMetrics and
    its raw swap list. `swaps` is accepted (rather than just `metrics`) so
    future refinements can look at per-trade timing distributions directly;
    the current heuristics only need the aggregate metrics, but keeping the
    raw list in the signature avoids a breaking change later.
    """
    del swaps  # not currently needed beyond what's already aggregated into `metrics`

    # Early Buyer Score (heuristic): entering a position and riding it to a
    # meaningfully positive ROI over a non-trivial hold time looks like
    # accumulating ahead of a move, rather than chasing one already in
    # progress. We have no direct "seconds after pool creation" signal from
    # any free-tier source, so this proxies on ROI + hold time instead.
    early_buyer_score = _clamp01(
        0.6 * _sigmoid(metrics.roi_30d, 0.4, 3.0) + 0.4 * _sigmoid(metrics.avg_hold_minutes, 180, 0.01)
    ) * 100

    # Diamond Hand Score: long average holds + comparatively low trade
    # frequency reads as patience (riding a position out) rather than
    # panic-selling into every dip.
    diamond_hand_score = _clamp01(
        0.7 * _sigmoid(metrics.avg_hold_minutes, 720, 0.004)
        + 0.3 * (1 - _clamp01(metrics.trade_frequency_7d / 100))
    ) * 100

    # Quick Flip Score: short holds paired with a solid win rate — fast,
    # disciplined scalps rather than a bot-like spray of trades (that's
    # sniper_probability below).
    quick_flip_score = _clamp01(
        0.6 * (1 - _sigmoid(metrics.avg_hold_minutes, 120, 0.02)) + 0.4 * metrics.win_rate
    ) * 100

    # Sniper Probability: very high trade frequency + very short average
    # holds together look automated (a bot sniping new pools) rather than a
    # discretionary trader — a caution flag for evaluate_promotion's wash-
    # trading-adjacent gates, not a compliment.
    sniper_probability = _clamp01(
        _sigmoid(metrics.trade_frequency_7d, 50, 0.05) * (1 - _sigmoid(metrics.avg_hold_minutes, 30, 0.05))
    ) * 100

    # Conviction Score: meaningful position sizing + low realized drawdown +
    # a solid win rate together suggest the wallet backs its own calls
    # rather than spraying tiny exploratory positions.
    conviction_score = _clamp01(
        0.4 * _sigmoid(metrics.avg_position_usd, 3000, 0.0003)
        + 0.3 * (1 - metrics.max_drawdown)
        + 0.3 * metrics.win_rate
    ) * 100

    # Consistency Score: low drawdown + a stable, healthy trade-success rate.
    consistency_score = _clamp01(0.6 * (1 - metrics.max_drawdown) + 0.4 * metrics.trade_success_rate) * 100

    # Risk Score (higher = riskier): rewards exactly the failure modes the
    # other scores don't — deep drawdown, poor success rate, and
    # sniper-adjacent frequency. Intentionally the inverse-flavored score of
    # the set; feeds a caution flag, not a promotion boost.
    risk_score = _clamp01(
        0.5 * metrics.max_drawdown
        + 0.3 * (1 - metrics.trade_success_rate)
        + 0.2 * _sigmoid(metrics.trade_frequency_7d, 100, 0.03)
    ) * 100

    return BehaviorScores(
        early_buyer_score=round(early_buyer_score, 1),
        diamond_hand_score=round(diamond_hand_score, 1),
        quick_flip_score=round(quick_flip_score, 1),
        sniper_probability=round(sniper_probability, 1),
        conviction_score=round(conviction_score, 1),
        consistency_score=round(consistency_score, 1),
        risk_score=round(risk_score, 1),
    )


def behavior_scores_to_json(scores: BehaviorScores) -> dict[str, float]:
    return {
        "early_buyer_score": scores.early_buyer_score,
        "diamond_hand_score": scores.diamond_hand_score,
        "quick_flip_score": scores.quick_flip_score,
        "sniper_probability": scores.sniper_probability,
        "conviction_score": scores.conviction_score,
        "consistency_score": scores.consistency_score,
        "risk_score": scores.risk_score,
    }


# A small, deliberately bounded confidence adjustment derived from behaviour
# — enrichment only (Priority 6/8: "these should become inputs into the
# discovery confidence" / "only enrich confidence, never depend on this
# source"). Capped well below the weight of the core score_wallet output so
# behaviour analysis can nudge ranking but never substitute for profitability.
MAX_BEHAVIOR_CONFIDENCE_BONUS = 8.0


def behavior_confidence_bonus(scores: BehaviorScores) -> float:
    """Returns a value in [-MAX_BEHAVIOR_CONFIDENCE_BONUS,
    +MAX_BEHAVIOR_CONFIDENCE_BONUS] to add to a candidate's confidence.
    Rewards conviction/consistency/diamond-handing; penalizes high sniper
    probability / risk score.
    """
    positive = (scores.conviction_score + scores.consistency_score + scores.diamond_hand_score) / 3
    negative = (scores.sniper_probability + scores.risk_score) / 2
    net = (positive - negative) / 100  # -1..1
    return round(_clamp01((net + 1) / 2) * (2 * MAX_BEHAVIOR_CONFIDENCE_BONUS) - MAX_BEHAVIOR_CONFIDENCE_BONUS, 1)
