from whale_alpha.engines.trading_engine import FIXED_AUTO_POLICY, fixed_auto_rules, validate_manual_buy


def test_fixed_auto_policy():
    rules = fixed_auto_rules(True)
    assert rules.fixed_trade_amount_usd == FIXED_AUTO_POLICY.amount_usd == 5.0
    assert rules.max_slippage_bps == 150
    assert rules.max_daily_trades == 5
    assert rules.max_daily_exposure_usd == 25.0
    assert rules.min_liquidity_usd == 10_000.0
    assert rules.percent_allocation is None


def test_manual_buy_bounds():
    assert validate_manual_buy(5.0, 150) is None
    assert validate_manual_buy(0.99, 150) is not None
    assert validate_manual_buy(500.01, 150) is not None
    assert validate_manual_buy(5.0, 49) is not None
    assert validate_manual_buy(5.0, 501) is not None
