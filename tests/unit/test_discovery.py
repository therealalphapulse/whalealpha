"""Unit tests for engines/discovery.py's pure decision functions — no DB,
no network. See tests/unit/test_discovery_metrics.py for metrics computation
and tests/unit/test_scoring.py for the underlying scoring algorithm itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from whale_alpha.engines.discovery import (
    DiscoveryConfig,
    evaluate_promotion,
    evaluate_retention,
    select_wallets_to_retire_for_ceiling,
)
from whale_alpha.engines.scoring import MIN_APPROVED_SCORE, WalletMetrics

NOW = datetime(2026, 8, 1, tzinfo=UTC)

CONFIG = DiscoveryConfig(
    min_tracked_wallets=500,
    max_tracked_wallets=1500,
    min_score_to_approve=55,
    min_roi_30d=0.15,
    min_win_rate=0.5,
    min_trade_count_30d=10,
    min_wallet_age_days=14,
    inactivity_timeout_days=21,
    low_score_cycles_before_retire=3,
)

QUALIFIED_METRICS = WalletMetrics(
    roi_30d=0.8,
    win_rate=0.75,
    pnl_usd_30d=50000,
    avg_hold_minutes=240,
    avg_position_usd=4000,
    trade_frequency_7d=15,
    wallet_age_days=300,
    max_drawdown=0.15,
    trade_success_rate=0.7,
)


# --------------------------------------------------------------------------
# evaluate_promotion
# --------------------------------------------------------------------------


def test_promotes_a_wallet_that_clears_every_gate():
    decision = evaluate_promotion(
        score=80, trade_count_30d=20, metrics=QUALIFIED_METRICS, flags=[], config=CONFIG
    )
    assert decision.approved
    assert decision.reason is None


def test_rejects_a_wallet_flagged_for_suspected_wash_trading_regardless_of_score():
    decision = evaluate_promotion(
        score=95,
        trade_count_30d=20,
        metrics=QUALIFIED_METRICS,
        flags=["SUSPECTED_WASH_TRADING_FREQUENCY"],
        config=CONFIG,
    )
    assert not decision.approved
    assert decision.reason == "SUSPECTED_WASH_TRADING"


def test_rejects_a_wallet_younger_than_the_minimum_age():
    young = WalletMetrics(**{**QUALIFIED_METRICS.__dict__, "wallet_age_days": 2})
    decision = evaluate_promotion(score=80, trade_count_30d=20, metrics=young, flags=[], config=CONFIG)
    assert not decision.approved
    assert decision.reason == "WALLET_TOO_NEW"


def test_rejects_a_wallet_with_too_few_trades():
    decision = evaluate_promotion(
        score=80, trade_count_30d=3, metrics=QUALIFIED_METRICS, flags=[], config=CONFIG
    )
    assert not decision.approved
    assert decision.reason == "INSUFFICIENT_TRADE_HISTORY"


def test_rejects_a_wallet_below_minimum_win_rate():
    low_win_rate = WalletMetrics(**{**QUALIFIED_METRICS.__dict__, "win_rate": 0.2})
    decision = evaluate_promotion(
        score=80, trade_count_30d=20, metrics=low_win_rate, flags=[], config=CONFIG
    )
    assert not decision.approved
    assert decision.reason == "WIN_RATE_BELOW_MINIMUM"


def test_rejects_a_wallet_below_minimum_roi():
    low_roi = WalletMetrics(**{**QUALIFIED_METRICS.__dict__, "roi_30d": 0.01})
    decision = evaluate_promotion(score=80, trade_count_30d=20, metrics=low_roi, flags=[], config=CONFIG)
    assert not decision.approved
    assert decision.reason == "ROI_BELOW_MINIMUM"


def test_rejects_a_wallet_below_minimum_score_even_if_metrics_pass():
    decision = evaluate_promotion(
        score=10, trade_count_30d=20, metrics=QUALIFIED_METRICS, flags=[], config=CONFIG
    )
    assert not decision.approved
    assert decision.reason == "SCORE_BELOW_MINIMUM"


# --------------------------------------------------------------------------
# evaluate_retention
# --------------------------------------------------------------------------


def test_retires_a_wallet_inactive_past_the_timeout():
    decision = evaluate_retention(
        score=90,
        consecutive_low_score_cycles=0,
        last_active_at=NOW - timedelta(days=30),
        now=NOW,
        config=CONFIG,
    )
    assert decision.retire
    assert decision.reason == "INACTIVITY"


def test_does_not_retire_an_active_high_scoring_wallet():
    decision = evaluate_retention(
        score=90,
        consecutive_low_score_cycles=0,
        last_active_at=NOW - timedelta(days=1),
        now=NOW,
        config=CONFIG,
    )
    assert not decision.retire


def test_retires_after_enough_consecutive_low_score_cycles():
    # Cycle 1 and 2: below MIN_APPROVED_SCORE but not yet 3 in a row.
    d1 = evaluate_retention(
        score=MIN_APPROVED_SCORE - 1,
        consecutive_low_score_cycles=0,
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
    )
    assert not d1.retire
    assert d1.new_consecutive_low_score_cycles == 1

    d2 = evaluate_retention(
        score=MIN_APPROVED_SCORE - 1,
        consecutive_low_score_cycles=d1.new_consecutive_low_score_cycles,
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
    )
    assert not d2.retire
    assert d2.new_consecutive_low_score_cycles == 2

    d3 = evaluate_retention(
        score=MIN_APPROVED_SCORE - 1,
        consecutive_low_score_cycles=d2.new_consecutive_low_score_cycles,
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
    )
    assert d3.retire
    assert d3.reason == "SUSTAINED_LOW_SCORE"


def test_resets_low_score_streak_once_score_recovers():
    decision = evaluate_retention(
        score=90,
        consecutive_low_score_cycles=2,
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
    )
    assert not decision.retire
    assert decision.new_consecutive_low_score_cycles == 0


def test_low_score_retirement_suppressed_when_population_is_at_the_floor():
    decision = evaluate_retention(
        score=MIN_APPROVED_SCORE - 1,
        consecutive_low_score_cycles=5,  # would otherwise clearly retire
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
        allow_low_score_retirement=False,
    )
    assert not decision.retire
    assert decision.new_consecutive_low_score_cycles == 6


def test_inactivity_retirement_not_suppressed_even_at_the_floor():
    # A dormant wallet should still go even when we're short on wallets —
    # keeping dead weight never helps the shortage.
    decision = evaluate_retention(
        score=90,
        consecutive_low_score_cycles=0,
        last_active_at=NOW - timedelta(days=60),
        now=NOW,
        config=CONFIG,
        allow_low_score_retirement=False,
    )
    assert decision.retire
    assert decision.reason == "INACTIVITY"


def test_missing_score_leaves_streak_untouched_and_does_not_retire():
    decision = evaluate_retention(
        score=None,
        consecutive_low_score_cycles=2,
        last_active_at=NOW,
        now=NOW,
        config=CONFIG,
    )
    assert not decision.retire
    assert decision.new_consecutive_low_score_cycles == 2


# --------------------------------------------------------------------------
# select_wallets_to_retire_for_ceiling
# --------------------------------------------------------------------------


def test_selects_nothing_when_under_the_ceiling():
    approved = [("a", 90), ("b", 80)]
    assert select_wallets_to_retire_for_ceiling(approved, max_tracked=1500) == []


def test_selects_lowest_scoring_wallets_to_bring_population_to_the_ceiling():
    approved = [("a", 90), ("b", 10), ("c", 50), ("d", 5)]
    to_retire = select_wallets_to_retire_for_ceiling(approved, max_tracked=2)
    assert set(to_retire) == {"b", "d"}


def test_selects_exactly_the_surplus_count():
    approved = [(str(i), float(i)) for i in range(10)]
    to_retire = select_wallets_to_retire_for_ceiling(approved, max_tracked=7)
    assert len(to_retire) == 3
    assert set(to_retire) == {"0", "1", "2"}  # three lowest scores
