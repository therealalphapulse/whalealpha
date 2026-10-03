"""
tests/test_repump_redelivery.py

Coverage for domain/signals/repump_redelivery.py's pure decision logic:
when an already-alerted token that pumped, dumped and later begins a
significant new pump qualifies for a REDELIVERED alert + tracking reset.

No DB / Telegram / network: update_trough() and evaluate_repump() are pure.
"""

import json
import unittest
from datetime import datetime, timedelta

from domain.signals.repump_redelivery import (
    RepumpConfig,
    append_cycle_archive,
    evaluate_repump,
    update_trough,
)

CFG = RepumpConfig()
NOW = datetime(2026, 10, 3, 12, 0, 0)
LONG_AGO = NOW - timedelta(hours=3)


def _eval(**over):
    base = dict(
        entry_mc=100_000.0,
        peak_mc=300_000.0,          # 3.0x prior pump
        trough_mc=100_000.0,        # -66% from peak
        cur_mc=250_000.0,           # 2.5x off the low
        volume_24h=50_000.0,
        liquidity=20_000.0,
        now=NOW,
        reference_time=LONG_AGO,
        redelivery_count=0,
        cfg=CFG,
    )
    base.update(over)
    return evaluate_repump(**base)


class TroughTrackingTests(unittest.TestCase):
    def test_single_poll_dip_never_creates_a_trough(self):
        # Provider glitch: one bad tick deep in the dump zone, previous poll normal.
        self.assertIsNone(update_trough(280_000, 5_000, 300_000, None, CFG))

    def test_two_consecutive_polls_in_dump_zone_confirm_trough(self):
        # trough is the HIGHER (more conservative) of the two readings
        self.assertEqual(update_trough(120_000, 90_000, 300_000, None, CFG), 120_000)

    def test_trough_only_ratchets_down(self):
        t = update_trough(120_000, 90_000, 300_000, None, CFG)
        t = update_trough(80_000, 70_000, 300_000, t, CFG)
        self.assertEqual(t, 80_000)
        # a later, higher in-zone pair must not raise it
        self.assertEqual(update_trough(140_000, 130_000, 300_000, t, CFG), 80_000)

    def test_above_dump_line_leaves_trough_unchanged(self):
        self.assertEqual(update_trough(200_000, 210_000, 300_000, 80_000, CFG), 80_000)

    def test_new_high_clears_trough(self):
        self.assertIsNone(update_trough(250_000, 320_000, 300_000, 80_000, CFG))


class EvaluateRepumpTests(unittest.TestCase):
    def test_pump_dump_repump_triggers(self):
        d = _eval()
        self.assertTrue(d.trigger, d.reason)
        self.assertAlmostEqual(d.prior_peak_multiple, 3.0)
        self.assertAlmostEqual(d.rebound_multiple, 2.5)
        self.assertAlmostEqual(d.dump_pct, (1 - 100_000 / 300_000) * 100)

    def test_disabled(self):
        self.assertEqual(_eval(cfg=RepumpConfig(enabled=False)).reason, "disabled")

    def test_no_confirmed_dump(self):
        self.assertEqual(_eval(trough_mc=None).reason, "no_confirmed_dump")

    def test_token_that_never_pumped_does_not_qualify(self):
        # peak only 1.2x entry: dumped then bounced, but never pumped first
        d = _eval(peak_mc=120_000, trough_mc=50_000, cur_mc=110_000)
        self.assertEqual(d.reason, "no_prior_pump")

    def test_shallow_pullback_is_not_a_dump(self):
        d = _eval(trough_mc=200_000, cur_mc=450_000)
        self.assertEqual(d.reason, "dump_too_shallow")

    def test_small_bounce_is_not_a_significant_repump(self):
        self.assertEqual(_eval(cur_mc=150_000).reason, "rebound_too_small")  # 1.5x < 2x

    def test_exact_rebound_threshold_triggers(self):
        self.assertTrue(_eval(cur_mc=200_000).trigger)  # exactly 2.0x

    def test_dead_volume_blocks(self):
        self.assertEqual(_eval(volume_24h=1_000).reason, "low_volume")

    def test_drained_liquidity_blocks(self):
        self.assertEqual(_eval(liquidity=500).reason, "low_liquidity")

    def test_min_gap_since_cycle_start(self):
        self.assertEqual(_eval(reference_time=NOW - timedelta(minutes=5)).reason, "min_gap")

    def test_max_redeliveries_cap(self):
        self.assertEqual(_eval(redelivery_count=3).reason, "max_redeliveries")

    def test_bad_market_data(self):
        self.assertEqual(_eval(cur_mc=0).reason, "bad_market_cap")


class EndToEndPollSimulationTests(unittest.TestCase):
    """Walks a realistic poll sequence through update_trough + evaluate_repump
    exactly as the lifecycle hook does (stale peak/prev from the prior poll)."""

    def _run(self, series, entry=100_000.0):
        peak, prev, trough, fired_at = entry, entry, None, None
        for i, mc in enumerate(series):
            d = evaluate_repump(
                entry_mc=entry, peak_mc=peak, trough_mc=trough, cur_mc=mc,
                volume_24h=50_000, liquidity=20_000, now=NOW,
                reference_time=LONG_AGO, redelivery_count=0, cfg=CFG,
            )
            if d.trigger:
                fired_at = i
                break
            trough = update_trough(prev, mc, peak, trough, CFG)
            peak = max(peak, mc)
            prev = mc
        return fired_at

    def test_pump_dump_repump_fires_on_the_rebound_poll(self):
        #        pump                 dump (2 polls in zone)      re-pump
        series = [150_000, 300_000, 140_000, 90_000, 80_000, 95_000, 170_000, 200_000, 260_000]
        # trough confirmed at max(90k,80k)=90k; 2x off it = 180k -> first >=180k is index 7 (200k)
        self.assertEqual(self._run(series), 7)

    def test_ordinary_chop_never_fires(self):
        series = [120_000, 140_000, 110_000, 150_000, 130_000, 160_000, 125_000]
        self.assertIsNone(self._run(series))

    def test_dump_without_recovery_never_fires(self):
        series = [300_000, 120_000, 90_000, 70_000, 60_000, 65_000, 80_000]
        self.assertIsNone(self._run(series))

    def test_glitch_tick_cannot_fake_a_dump_and_repump(self):
        # one-poll 1_000 mc glitch then straight back: no confirmed trough
        series = [300_000, 290_000, 1_000, 280_000, 295_000, 310_000]
        self.assertIsNone(self._run(series))


class ArchiveTests(unittest.TestCase):
    def test_append_and_cap(self):
        s = None
        for i in range(25):
            s = append_cycle_archive(s, {"cycle": i}, keep=20)
        cycles = json.loads(s)
        self.assertEqual(len(cycles), 20)
        self.assertEqual(cycles[-1]["cycle"], 24)

    def test_tolerates_corrupt_prior_json(self):
        s = append_cycle_archive("{not json", {"cycle": 1})
        self.assertEqual(json.loads(s), [{"cycle": 1}])


if __name__ == "__main__":
    unittest.main()
