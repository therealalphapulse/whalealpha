from domain.signals.adaptive_filter_policy import adaptive_mc_liquidity_gate


def _data(**overrides):
    data = {
        "liquidity": 20_000,
        "market_cap": 500_000,
        "volume_1h": 25_000,
        "txns_1h_buys": 70,
        "txns_1h_sells": 30,
        "price_change_1h": 8,
    }
    data.update(overrides)
    return data


def test_healthy_meme_pool_passes_without_2x_mc_liquidity_ratio():
    # Old gate required liquidity >= 50% of MC. The adaptive policy uses a
    # depth floor tied to MC instead, so a healthy 4% pool is eligible.
    ok, reason = adaptive_mc_liquidity_gate(_data(liquidity=20_000))
    assert ok, reason


def test_absolute_liquidity_floor_remains_hard():
    ok, reason = adaptive_mc_liquidity_gate(_data(liquidity=4_999))
    assert not ok
    assert "absolute floor" in reason


def test_normal_mc_ceiling_still_rejects_unconfirmed_large_token():
    ok, reason = adaptive_mc_liquidity_gate(
        _data(market_cap=2_000_000, liquidity=100_000, volume_1h=20_000,
              txns_1h_buys=55, txns_1h_sells=45, price_change_1h=2)
    )
    assert not ok
    assert "normal ceiling" in reason


def test_strong_flow_can_use_extended_mc_window():
    ok, reason = adaptive_mc_liquidity_gate(
        _data(market_cap=2_000_000, liquidity=100_000, volume_1h=220_000,
              txns_1h_buys=75, txns_1h_sells=25, price_change_1h=12)
    )
    assert ok, reason


def test_low_volume_requires_real_flow_confirmation():
    ok, reason = adaptive_mc_liquidity_gate(
        _data(volume_1h=900, txns_1h_buys=3, txns_1h_sells=3)
    )
    assert not ok
    assert "low 1h volume" in reason


def test_low_volume_can_pass_when_relative_flow_is_strong():
    ok, reason = adaptive_mc_liquidity_gate(
        _data(market_cap=30_000, liquidity=5_000, volume_1h=900,
              txns_1h_buys=8, txns_1h_sells=2)
    )
    assert ok, reason
