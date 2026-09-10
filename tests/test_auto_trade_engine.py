"""tests/test_auto_trade_engine.py

Unit tests for the pure-logic parts of the new, isolated Auto-Trade
Engine (domain/trading/auto_trade/) that don't require a database or
network: tier bucketing and the TP/SL/trailing-stop trigger evaluation
(§17-21 of the spec).

These deliberately do not touch models/real_trade.py or anything under
domain/trading/real/ -- the new engine is tested in isolation, same as
it runs in isolation.
"""

from domain.trading.auto_trade.signal_adapter import score_to_tier
from domain.trading.auto_trade.exit_engine import _check_exit_trigger


def test_score_to_tier_buckets():
    assert score_to_tier(None) == "UNKNOWN"
    assert score_to_tier(10) == "LOW"
    assert score_to_tier(64.9) == "LOW"
    assert score_to_tier(65) == "MEDIUM"
    assert score_to_tier(84.9) == "MEDIUM"
    assert score_to_tier(85) == "HIGH"
    assert score_to_tier(99) == "HIGH"


def _position(entry_price, highest=None, tp=50.0, sl=30.0, trailing_enabled=False, trailing_pct=None,
              activation=None, retracement=None):
    import json
    from types import SimpleNamespace

    snapshot = {
        "auto_trade_enabled": True, "buy_amount_sol": 0.1,
        "take_profit_pct": tp, "stop_loss_pct": sl,
        "trailing_stop_enabled": trailing_enabled, "trailing_stop_pct": trailing_pct,
        "slippage_bps": 150, "priority_fee_tier": "auto",
        "max_position_size_usdt": None, "created_at": "2026-01-01T00:00:00+00:00",
        "trailing_activation_pct": activation, "trailing_retracement_pct": retracement,
    }
    return SimpleNamespace(
        entry_price=entry_price,
        highest_observed_price=highest if highest is not None else entry_price,
        policy_snapshot_json=json.dumps(snapshot),
    )


def test_stop_loss_triggers_before_take_profit_and_trailing():
    # §20 exit priority: SL checked first.
    position = _position(entry_price=1.0, sl=30.0, tp=10.0, trailing_enabled=True, trailing_pct=5.0)
    assert _check_exit_trigger(position, current_price=0.65) == "sl"


def test_take_profit_triggers_at_threshold():
    position = _position(entry_price=1.0, sl=30.0, tp=50.0)
    assert _check_exit_trigger(position, current_price=1.49) is None
    assert _check_exit_trigger(position, current_price=1.50) == "tp"


def test_trailing_stop_does_not_arm_below_entry():
    position = _position(entry_price=1.0, sl=30.0, tp=None, trailing_enabled=True, trailing_pct=10.0, highest=1.0)
    assert _check_exit_trigger(position, current_price=0.95) is None


def test_trailing_stop_fires_after_a_gain_then_pullback():
    position = _position(entry_price=1.0, sl=90.0, tp=None, trailing_enabled=True, trailing_pct=10.0, highest=2.0)
    assert _check_exit_trigger(position, current_price=1.85) is None
    assert _check_exit_trigger(position, current_price=1.79) == "trailing"


def test_no_trigger_when_no_rules_configured():
    position = _position(entry_price=1.0, sl=None, tp=None, trailing_enabled=False)
    assert _check_exit_trigger(position, current_price=100.0) is None
    assert _check_exit_trigger(position, current_price=0.01) is None

def test_trailing_does_not_arm_until_activation_threshold_reached():
    # Trailing Activation (%): trailing must not arm (or trigger) until
    # price has gained at least the configured activation percentage
    # from entry, even though the position is already above entry and a
    # Retracement (%) is configured.
    position = _position(
        entry_price=1.0, sl=90.0, tp=None, trailing_enabled=True,
        highest=1.15, activation=20.0, retracement=10.0,
    )
    # Peak is only +15% -- below the 20% activation gate -- so trailing
    # must stay unarmed even on a pullback.
    assert _check_exit_trigger(position, current_price=1.03) is None


def test_trailing_retracement_fires_once_activated():
    # Once Activation (%) is cleared, Retracement (%) governs the sell
    # trigger as a pullback from the highest observed price.
    position = _position(
        entry_price=1.0, sl=90.0, tp=None, trailing_enabled=True,
        highest=1.25, activation=20.0, retracement=10.0,
    )
    # Peak +25% clears the 20% activation gate. Trigger price = 1.25 * 0.90 = 1.125.
    assert _check_exit_trigger(position, current_price=1.13) is None
    assert _check_exit_trigger(position, current_price=1.12) == "trailing"


def test_trailing_retracement_falls_back_to_legacy_trailing_stop_pct():
    # A policy/snapshot with no trailing_retracement_pct configured (the
    # field didn't exist yet, or the user never set it) must keep
    # triggering at exactly the same distance as the pre-existing
    # trailing_stop_pct -- no behavior change for already-configured
    # trades.
    position = _position(entry_price=1.0, sl=90.0, tp=None, trailing_enabled=True, trailing_pct=10.0, highest=2.0)
    assert _check_exit_trigger(position, current_price=1.85) is None
    assert _check_exit_trigger(position, current_price=1.79) == "trailing"


def test_trailing_still_works_from_snapshot_missing_new_fields():
    # Simulates a position opened before this migration: its stored
    # policy_snapshot_json has no trailing_activation_pct or
    # trailing_retracement_pct keys at all. TradePolicySnapshot.from_json
    # must still parse it (via the new fields' dataclass defaults) and
    # exit_engine must still trigger exactly as it did before.
    import json
    from types import SimpleNamespace

    snapshot = {
        "auto_trade_enabled": True, "buy_amount_sol": 0.1,
        "take_profit_pct": None, "stop_loss_pct": 90.0,
        "trailing_stop_enabled": True, "trailing_stop_pct": 10.0,
        "slippage_bps": 150, "priority_fee_tier": "auto",
        "max_position_size_usdt": None, "created_at": "2026-01-01T00:00:00+00:00",
    }
    position = SimpleNamespace(entry_price=1.0, highest_observed_price=2.0, policy_snapshot_json=json.dumps(snapshot))
    assert _check_exit_trigger(position, current_price=1.79) == "trailing"


def test_sl_still_takes_priority_over_trailing_with_new_fields_set():
    # §20 exit priority must be unchanged: SL still wins over trailing,
    # even when Activation/Retracement are both configured.
    position = _position(
        entry_price=1.0, sl=30.0, tp=None, trailing_enabled=True,
        highest=1.05, activation=5.0, retracement=5.0,
    )
    assert _check_exit_trigger(position, current_price=0.65) == "sl"
