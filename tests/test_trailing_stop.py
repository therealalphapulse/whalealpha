import asyncio

from domain.trading.real import real_exit_engine
from models.real_exit_rule import RealExitRule


def test_trail_validate_accepts_valid_params():
    real_exit_engine._validate_rule("trail", trigger_pct=10, sell_fraction=1.0, arm_pct=5)


def test_trail_validate_rejects_trigger_pct_at_or_over_100():
    try:
        real_exit_engine._validate_rule("trail", trigger_pct=100, sell_fraction=1.0, arm_pct=0)
    except real_exit_engine.ExitRuleValidationError:
        return
    raise AssertionError("trail trigger_pct >= 100 was accepted")


def test_trail_validate_rejects_partial_sell_fraction():
    try:
        real_exit_engine._validate_rule("trail", trigger_pct=10, sell_fraction=0.5, arm_pct=0)
    except real_exit_engine.ExitRuleValidationError:
        return
    raise AssertionError("trail with sell_fraction != 1.0 was accepted")


def test_trail_validate_rejects_negative_arm_pct():
    try:
        real_exit_engine._validate_rule("trail", trigger_pct=10, sell_fraction=1.0, arm_pct=-1)
    except real_exit_engine.ExitRuleValidationError:
        return
    raise AssertionError("negative arm_pct was accepted")


def _make_rule(arm_pct=0.0, high_water_price=None, trigger_pct=10.0):
    return RealExitRule(
        id=1, user_id=1, trade_id=1, kind="trail",
        trigger_pct=trigger_pct, sell_fraction=1.0,
        arm_pct=arm_pct, high_water_price=high_water_price, status="active",
    )


def test_trail_stays_unarmed_below_arm_threshold():
    rule = _make_rule(arm_pct=20.0, high_water_price=None)
    fired = asyncio.run(real_exit_engine._tick_trail_rule(rule, entry_price=1.0, current_price=1.10))
    assert fired is False
    assert rule.high_water_price is None


def test_trail_does_not_fire_within_trail_distance_of_peak():
    rule = _make_rule(arm_pct=0.0, high_water_price=2.0, trigger_pct=10.0)
    fired = asyncio.run(real_exit_engine._tick_trail_rule(rule, entry_price=1.0, current_price=1.9))
    assert fired is False
    assert rule.high_water_price == 2.0


def test_trail_fires_once_price_falls_trail_distance_below_peak():
    rule = _make_rule(arm_pct=0.0, high_water_price=2.0, trigger_pct=10.0)
    fired = asyncio.run(real_exit_engine._tick_trail_rule(rule, entry_price=1.0, current_price=1.7))
    assert fired is True
