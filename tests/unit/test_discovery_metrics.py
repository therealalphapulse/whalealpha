"""Unit tests for engines/discovery_metrics.py — pure functions, no I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from whale_alpha.engines.discovery_metrics import compute_wallet_metrics
from whale_alpha.integrations.wallet_discovery_source import WalletSwap

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _swap(side: str, mint: str, amount_usd: float, days_ago: float) -> WalletSwap:
    return WalletSwap(side=side, token_mint=mint, amount_usd=amount_usd, timestamp=NOW - timedelta(days=days_ago))


def test_returns_none_with_fewer_than_two_swaps():
    assert compute_wallet_metrics([_swap("BUY", "TOKEN", 100, 1)], wallet_age_days=100, now=NOW) is None


def test_returns_none_with_no_closed_round_trips():
    # Two BUYs, no SELL — nothing realized yet.
    swaps = [_swap("BUY", "TOKEN", 100, 5), _swap("BUY", "TOKEN", 100, 3)]
    assert compute_wallet_metrics(swaps, wallet_age_days=100, now=NOW) is None


def test_computes_positive_roi_and_full_win_rate_for_a_profitable_round_trip():
    swaps = [
        _swap("BUY", "TOKEN", 100, 5),
        _swap("SELL", "TOKEN", 200, 3),  # 2x return
    ]
    computed = compute_wallet_metrics(swaps, wallet_age_days=100, now=NOW)
    assert computed is not None
    assert computed.metrics.roi_30d == 1.0  # (200-100)/100
    assert computed.metrics.win_rate == 1.0
    assert computed.metrics.pnl_usd_30d == 100
    assert computed.trade_count_30d == 2


def test_computes_zero_win_rate_for_a_losing_round_trip():
    swaps = [
        _swap("BUY", "TOKEN", 100, 5),
        _swap("SELL", "TOKEN", 40, 3),  # a loss
    ]
    computed = compute_wallet_metrics(swaps, wallet_age_days=100, now=NOW)
    assert computed is not None
    assert computed.metrics.win_rate == 0.0
    assert computed.metrics.pnl_usd_30d == -60


def test_fifo_closes_the_oldest_open_lot_first_leaving_others_open():
    # A single SELL closes exactly one open lot (the oldest) in full — see
    # _match_round_trips' docstring for why this is one-to-one, not
    # proportional. The second BUY lot remains open (unrealized) and isn't
    # counted towards realized PnL/win-rate.
    swaps = [
        _swap("BUY", "TOKEN", 100, 10),
        _swap("BUY", "TOKEN", 100, 8),
        _swap("SELL", "TOKEN", 300, 5),  # closes only the oldest ($100) lot
    ]
    computed = compute_wallet_metrics(swaps, wallet_age_days=100, now=NOW)
    assert computed is not None
    assert computed.metrics.pnl_usd_30d == 200  # 300 proceeds - 100 cost basis
    assert computed.metrics.win_rate == 1.0  # the one realized round trip was profitable
    assert computed.trade_count_30d == 3  # all three swaps fall within the 30d window


def test_a_sell_with_no_open_lot_is_skipped_not_counted_as_a_loss():
    swaps = [
        _swap("SELL", "TOKEN", 200, 5),  # no prior BUY observed — unmatched, skipped
        _swap("BUY", "TOKEN", 100, 3),
        _swap("SELL", "TOKEN", 150, 1),
    ]
    computed = compute_wallet_metrics(swaps, wallet_age_days=100, now=NOW)
    assert computed is not None
    assert computed.metrics.pnl_usd_30d == 50
    assert computed.metrics.win_rate == 1.0


def test_excludes_round_trips_outside_the_30d_window_from_roi_and_pnl():
    swaps = [
        _swap("BUY", "TOKEN", 100, 40),
        _swap("SELL", "TOKEN", 500, 35),  # huge win, but > 30 days ago
        _swap("BUY", "TOKEN", 100, 5),
        _swap("SELL", "TOKEN", 90, 2),  # small loss, within 30 days
    ]
    computed = compute_wallet_metrics(swaps, wallet_age_days=100, now=NOW)
    assert computed is not None
    # Only the recent (losing) round trip should count towards the 30d window.
    assert computed.metrics.pnl_usd_30d == -10
    assert computed.metrics.roi_30d < 0
    # But win_rate is computed over ALL realized round trips, not just 30d.
    assert computed.metrics.win_rate == 0.5


def test_max_drawdown_is_zero_when_equity_curve_never_dips_below_start():
    swaps = [
        _swap("BUY", "TOKEN", 100, 10),
        _swap("SELL", "TOKEN", 150, 8),
        _swap("BUY", "TOKEN", 100, 6),
        _swap("SELL", "TOKEN", 200, 4),
    ]
    computed = compute_wallet_metrics(swaps, wallet_age_days=100, now=NOW)
    assert computed is not None
    assert computed.metrics.max_drawdown == 0.0


def test_max_drawdown_reflects_a_dip_after_a_peak():
    swaps = [
        _swap("BUY", "TOKEN", 100, 10),
        _swap("SELL", "TOKEN", 200, 8),  # peak equity = 100
        _swap("BUY", "TOKEN", 100, 6),
        _swap("SELL", "TOKEN", 50, 4),  # equity drops to 50 -> 50% drawdown from peak
    ]
    computed = compute_wallet_metrics(swaps, wallet_age_days=100, now=NOW)
    assert computed is not None
    assert computed.metrics.max_drawdown == 0.5


def test_passes_through_wallet_age_days_and_defaults_to_zero_when_unknown():
    swaps = [_swap("BUY", "TOKEN", 100, 5), _swap("SELL", "TOKEN", 150, 3)]
    computed = compute_wallet_metrics(swaps, wallet_age_days=None, now=NOW)
    assert computed is not None
    assert computed.metrics.wallet_age_days == 0

    computed_known_age = compute_wallet_metrics(swaps, wallet_age_days=42, now=NOW)
    assert computed_known_age is not None
    assert computed_known_age.metrics.wallet_age_days == 42


def test_ignores_zero_amount_swaps():
    swaps = [
        _swap("BUY", "TOKEN", 0, 5),  # unsized swap, e.g. couldn't resolve native amount
        _swap("BUY", "TOKEN", 100, 4),
        _swap("SELL", "TOKEN", 150, 2),
    ]
    computed = compute_wallet_metrics(swaps, wallet_age_days=100, now=NOW)
    assert computed is not None
    assert computed.metrics.pnl_usd_30d == 50
