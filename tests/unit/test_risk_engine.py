"""Port of tests/unit/riskEngine.test.ts — same cases, same assertions."""

from __future__ import annotations

from datetime import datetime, timedelta

from whale_alpha.engines.risk import AutoTradingRules, UserTradingState, evaluate_auto_trade
from whale_alpha.engines.signal import SignalCandidate

signal = SignalCandidate(
    token_mint="TOKEN_XYZ",
    wallet_count=4,
    total_capital_usd=8000,
    confidence_score=78,
    risk_level="LOW",
    entry_zone=None,
    ai_recommendation="test",
    contributing_wallets=["a", "b", "c", "d"],
)


def base_rules(**overrides) -> AutoTradingRules:
    defaults = dict(
        enabled=True,
        fixed_trade_amount_usd=100,
        percent_allocation=None,
        max_slippage_bps=150,
        max_market_cap_usd=5_000_000,
        min_liquidity_usd=10000,
        max_open_positions=5,
        max_daily_trades=10,
        max_daily_exposure_usd=500,
        token_blacklist=[],
        cooldown_minutes=15,
    )
    defaults.update(overrides)
    return AutoTradingRules(**defaults)


def base_state(**overrides) -> UserTradingState:
    defaults = dict(
        open_positions=1,
        trades_today=2,
        exposure_usd_today=100,
        last_trade_at=None,
        portfolio_value_usd=2000,
    )
    defaults.update(overrides)
    return UserTradingState(**defaults)


def test_approves_a_trade_when_all_rules_pass():
    result = evaluate_auto_trade(signal, 2_000_000, 50000, base_rules(), base_state())
    assert result.approved is True
    assert result.proposed_trade_usd == 100


def test_rejects_when_auto_trading_is_disabled():
    result = evaluate_auto_trade(signal, 2_000_000, 50000, base_rules(enabled=False), base_state())
    assert result.approved is False
    assert "AUTO_TRADING_DISABLED" in result.reasons


def test_rejects_a_blacklisted_token_even_with_a_strong_signal():
    result = evaluate_auto_trade(
        signal, 2_000_000, 50000, base_rules(token_blacklist=["TOKEN_XYZ"]), base_state()
    )
    assert result.approved is False
    assert "TOKEN_BLACKLISTED" in result.reasons


def test_rejects_when_liquidity_is_below_the_configured_minimum():
    result = evaluate_auto_trade(signal, 2_000_000, 500, base_rules(), base_state())
    assert result.approved is False
    assert "LIQUIDITY_BELOW_MINIMUM" in result.reasons


def test_rejects_when_daily_exposure_would_be_exceeded():
    state = base_state(exposure_usd_today=450)
    result = evaluate_auto_trade(signal, 2_000_000, 50000, base_rules(), state)
    assert result.approved is False
    assert "MAX_DAILY_EXPOSURE_EXCEEDED" in result.reasons


def test_rejects_during_an_active_cooldown_window():
    state = base_state(last_trade_at=datetime.now() - timedelta(minutes=5))
    result = evaluate_auto_trade(signal, 2_000_000, 50000, base_rules(), state)
    assert result.approved is False
    assert "COOLDOWN_ACTIVE" in result.reasons


def test_rejects_when_max_open_positions_is_reached():
    state = base_state(open_positions=5)
    result = evaluate_auto_trade(signal, 2_000_000, 50000, base_rules(), state)
    assert result.approved is False
    assert "MAX_OPEN_POSITIONS_REACHED" in result.reasons
