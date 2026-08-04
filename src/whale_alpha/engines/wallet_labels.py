"""Smart Money Labels — Hybrid Discovery Engine Priority 7.

New module (Phase 1 refactor). Every promoted wallet is automatically
tagged with zero or more labels derived from its already-computed metrics
(engines/discovery_metrics.ComputedMetrics), behaviour scores
(engines/behavior_scoring.BehaviorScores), and any social/KOL enrichment
(engines/discovery.py, Priority 8) — never invented, always derived from
real computed numbers. Labels are stored on WalletCandidate.labels and
copied onto WhaleWallet.tags at promotion (see engines/discovery.py).

Pure function, no I/O — fully unit-testable.
"""

from __future__ import annotations

from whale_alpha.engines.behavior_scoring import BehaviorScores
from whale_alpha.engines.scoring import WalletMetrics

# Thresholds are deliberately named constants (not magic numbers) so they're
# easy to tune without hunting through the boolean logic below.
_WHALE_MIN_POSITION_USD = 10_000
_KOL_MIN_TRADE_FREQUENCY_7D = 5
_HIGH_CONVICTION_MIN_SCORE = 70
_MOMENTUM_MIN_TRADE_FREQUENCY_7D = 20
_SWING_MIN_HOLD_MINUTES = 720  # 12h+
_SCALPER_MAX_HOLD_MINUTES = 60
_EARLY_ADOPTER_MIN_SCORE = 65
_FRESH_WALLET_MAX_AGE_DAYS = 30
_DORMANT_ALPHA_MIN_ROI = 0.5


def assign_labels(
    *,
    metrics: WalletMetrics,
    behavior: BehaviorScores,
    trade_count_30d: int,
    has_social_signal: bool = False,
) -> list[str]:
    """Returns the deduplicated list of labels this wallet qualifies for.
    A wallet can (and often will) carry several at once — e.g. a large,
    disciplined, momentum-riding wallet is both "Whale" and "Momentum
    Trader". `has_social_signal` comes from the Priority 8 enrichment (a
    known public wallet list / KOL-linked token metadata match) — see
    engines/discovery.py; passing False (the default) simply skips the KOL
    label, it never blocks any other label.
    """
    labels: list[str] = []

    if metrics.avg_position_usd >= _WHALE_MIN_POSITION_USD:
        labels.append("Whale")

    if behavior.conviction_score >= _HIGH_CONVICTION_MIN_SCORE and metrics.win_rate >= 0.5:
        labels.append("Smart Money")

    if has_social_signal:
        labels.append("KOL")

    if behavior.sniper_probability >= 70 and trade_count_30d >= 20:
        labels.append("Cabal")

    if behavior.early_buyer_score >= _EARLY_ADOPTER_MIN_SCORE:
        labels.append("Early Adopter")

    if metrics.trade_frequency_7d >= _MOMENTUM_MIN_TRADE_FREQUENCY_7D and metrics.roi_30d > 0:
        labels.append("Momentum Trader")

    if behavior.conviction_score >= _HIGH_CONVICTION_MIN_SCORE:
        labels.append("High Conviction")

    if behavior.diamond_hand_score >= 60 and metrics.avg_hold_minutes >= _SWING_MIN_HOLD_MINUTES:
        labels.append("Swing Trader")

    if behavior.quick_flip_score >= 60 and metrics.avg_hold_minutes <= _SCALPER_MAX_HOLD_MINUTES:
        labels.append("Scalper")

    if metrics.wallet_age_days <= _FRESH_WALLET_MAX_AGE_DAYS:
        labels.append("Fresh Wallet")

    if metrics.roi_30d >= _DORMANT_ALPHA_MIN_ROI and metrics.trade_frequency_7d < 2:
        labels.append("Dormant Alpha")

    # Preserve first-seen order but drop duplicates (defensive — the rules
    # above are already mutually distinct, but this keeps the guarantee
    # explicit for future rule additions).
    seen: set[str] = set()
    deduped: list[str] = []
    for label in labels:
        if label not in seen:
            seen.add(label)
            deduped.append(label)
    return deduped
