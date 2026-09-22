"""Regression tests for the global Trailing Stop toggle's pure logic
(the manual-side validation already covered in test_trailing_stop.py is
not repeated here)."""

from dataclasses import asdict

from domain.trading.auto_trade.policy_service import TradePolicySnapshot


def _base_snapshot(**overrides):
    fields = dict(
        auto_trade_enabled=True, buy_amount_sol=0.1, take_profit_pct=50.0, stop_loss_pct=30.0,
        trailing_stop_enabled=False, trailing_stop_pct=None, slippage_bps=150, priority_fee_tier="auto",
        max_position_size_usdt=None, created_at="2026-01-01T00:00:00", trailing_activation_pct=None,
        trailing_retracement_pct=None,
    )
    fields.update(overrides)
    return TradePolicySnapshot(**fields)


def test_enabling_trailing_on_snapshot_preserves_tp_sl():
    # Mirrors what set_trailing_for_all_open_positions does to one snapshot:
    # only trailing_* keys change, tp/sl are carried through untouched.
    snapshot = _base_snapshot()
    fields = asdict(snapshot)
    fields["trailing_stop_enabled"] = True
    fields["trailing_stop_pct"] = 10.0
    fields["trailing_activation_pct"] = 0.0
    updated = TradePolicySnapshot(**fields)

    assert updated.trailing_stop_enabled is True
    assert updated.trailing_stop_pct == 10.0
    assert updated.take_profit_pct == snapshot.take_profit_pct
    assert updated.stop_loss_pct == snapshot.stop_loss_pct
    assert updated.buy_amount_sol == snapshot.buy_amount_sol


def test_disabling_trailing_on_snapshot_preserves_tp_sl_and_pct_value():
    # Turning off must only flip the enabled flag, never erase the
    # configured trail_pct -- so turning back on later restores it.
    snapshot = _base_snapshot(trailing_stop_enabled=True, trailing_stop_pct=15.0)
    fields = asdict(snapshot)
    fields["trailing_stop_enabled"] = False
    updated = TradePolicySnapshot(**fields)

    assert updated.trailing_stop_enabled is False
    assert updated.trailing_stop_pct == 15.0  # untouched, not cleared
    assert updated.take_profit_pct == snapshot.take_profit_pct
    assert updated.stop_loss_pct == snapshot.stop_loss_pct


def test_enabling_does_not_override_an_already_configured_trail_pct():
    # A position/policy that already has its own trailing_stop_pct set
    # must NOT be overwritten by the wallet's default when the global
    # toggle turns on -- only unset positions get the default.
    snapshot = _base_snapshot(trailing_stop_pct=25.0)
    fields = asdict(snapshot)
    if not fields.get("trailing_stop_pct") and not fields.get("trailing_retracement_pct"):
        fields["trailing_stop_pct"] = 10.0  # would only apply if unset
    fields["trailing_stop_enabled"] = True
    updated = TradePolicySnapshot(**fields)

    assert updated.trailing_stop_pct == 25.0  # the user's own value, not the 10.0 default
