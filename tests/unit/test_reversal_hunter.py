from __future__ import annotations

from whale_alpha.engines.reversal_hunter import Candle, detect_dip_consolidation_breakout


def _fixture(two_candle_breakout: bool = True) -> list[Candle]:
    now = 1_787_770_000
    candles: list[Candle] = []
    for i in range(864):
        ts = now - (863 - i) * 300
        if i < 500:
            price = 1.8
        elif i <= 600:
            price = 2.0 - (i - 500) * 0.008
        elif i < 700:
            price = 1.2 + (i - 600) * 0.0002
        elif i < 762:
            price = 1.22 + (0.002 if i % 2 else -0.002)
        else:
            price = 1.22
        candles.append(Candle(ts, price, price * 1.002, price * 0.998, price, 1000))
    candles[-2] = Candle(now - 300, 1.22, 1.31, 1.21, 1.30, 2200)
    candles[-1] = Candle(now, 1.30, 1.36, 1.29, 1.35 if two_candle_breakout else 1.22, 2500)
    return candles


def test_confirms_dip_consolidation_and_breakout():
    result = detect_dip_consolidation_breakout(_fixture(), 1_787_770_000)
    assert result is not None
    assert 15 <= result.dip_pct <= 50
    assert result.consolidation_minutes >= 45
    assert result.consolidation_range_pct <= 12
    assert result.breakout_confirmed
    assert result.breakout_volume_5m_mult >= 1.8
    assert result.breakout_volume_15m_mult >= 1.8


def test_rejects_single_candle_spike_without_follow_through():
    candles = _fixture(two_candle_breakout=False)
    result = detect_dip_consolidation_breakout(candles, 1_787_770_000)
    assert result is None
