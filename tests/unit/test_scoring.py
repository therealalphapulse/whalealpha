"""Port of tests/unit/scoring.test.ts — same cases, same assertions."""

from __future__ import annotations

from whale_alpha.engines.scoring import WalletMetrics, score_wallet

strong_wallet = WalletMetrics(
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

weak_wallet = WalletMetrics(
    roi_30d=-0.3,
    win_rate=0.25,
    pnl_usd_30d=-5000,
    avg_hold_minutes=5,
    avg_position_usd=50,
    trade_frequency_7d=400,  # wash-trading-like frequency
    wallet_age_days=3,
    max_drawdown=0.8,
    trade_success_rate=0.2,
)


def test_scores_a_strong_wallet_significantly_higher_than_a_weak_one():
    strong = score_wallet(strong_wallet)
    weak = score_wallet(weak_wallet)
    assert strong.score > weak.score
    assert strong.score > 60
    assert weak.score < 40


def test_flags_suspiciously_high_trade_frequency_as_suspected_wash_trading():
    result = score_wallet(weak_wallet)
    assert "SUSPECTED_WASH_TRADING_FREQUENCY" in result.flags


def test_flags_new_wallets_with_low_track_record():
    result = score_wallet(weak_wallet)
    assert "NEW_WALLET_LOW_TRACK_RECORD" in result.flags


def test_keeps_score_within_0_100_bounds():
    for metrics in (strong_wallet, weak_wallet):
        result = score_wallet(metrics)
        assert 0 <= result.score <= 100
        assert 0 <= result.confidence <= 100
