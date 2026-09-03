"""
tests/test_first_milestone_automation.py

Regression coverage for the First Milestone Snapshot / First Milestone
Auto-Buy source added on top of the existing Quote Alert milestone ladder
and existing Automation settings (see domain/signals/signal_tracker.py and
domain/trading/real/real_automation_engine.py). Exercises the pure/unit-
testable pieces of that behavior directly, matching the style of
tests/test_quote_alert_milestones.py:

  1. signal_source_components() / ALL_SIGNAL_SOURCE_VALUES -- the extended
     auto_buy_signal_source value set (New only / Redelivered only / First
     Milestone only / and every pairwise + triple combination), including
     the "both" legacy alias.
  2. _signal_source_matches() -- the existing New/Redelivered signal-scan
     auto-buy gate must keep its exact pre-existing behavior for "new" /
     "redelivered" / "both", and must NEVER match when only
     "first_milestone" is selected (that source has its own dedicated
     trigger and must not also fire off a token's initial signal).
  3. _build_first_milestone_snapshot_text() -- the First Milestone Snapshot
     always includes the token's existing tracked data (name, symbol, CA,
     entry/current MC, gain, multiple, milestone reached) and is never
     worded as a new/fresh signal.
"""

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/test")

from domain.trading.real.real_automation_engine import (  # noqa: E402
    ALL_SIGNAL_SOURCE_VALUES,
    signal_source_components,
    _signal_source_matches,
)
from domain.signals.signal_tracker import _build_first_milestone_snapshot_text  # noqa: E402


def _filt(source):
    return SimpleNamespace(auto_buy_signal_source=source)


def _signal(was_redelivered=False, **overrides):
    base = dict(
        contract="ABCPUMP",
        name="Test Token",
        symbol="TEST",
        entry_market_cap=100_000.0,
        entry_price=0.001,
        current_price=0.004,
        ath_multiple=4.0,
        ath_market_cap=400_000.0,
        total_holders=250,
        top_holder_pct=5.5,
        dev_holding_pct=2.0,
        bundle_pct=1.5,
        was_redelivered=was_redelivered,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class SignalSourceComponentsTests(unittest.TestCase):
    """auto_buy_signal_source -> component set mapping. Every existing
    value's meaning is unchanged; the new combinations add first_milestone
    without altering what "new"/"redelivered"/"both" already meant."""

    def test_all_seven_new_values_are_accepted(self):
        expected = {
            "new",
            "redelivered",
            "first_milestone",
            "new_redelivered",
            "new_first_milestone",
            "redelivered_first_milestone",
            "new_redelivered_first_milestone",
        }
        self.assertTrue(expected.issubset(ALL_SIGNAL_SOURCE_VALUES))

    def test_legacy_both_value_still_accepted(self):
        self.assertIn("both", ALL_SIGNAL_SOURCE_VALUES)

    def test_new_only(self):
        self.assertEqual(signal_source_components(_filt("new")), {"new"})

    def test_redelivered_only(self):
        self.assertEqual(signal_source_components(_filt("redelivered")), {"redelivered"})

    def test_first_milestone_only(self):
        self.assertEqual(signal_source_components(_filt("first_milestone")), {"first_milestone"})

    def test_both_legacy_alias_equals_new_redelivered(self):
        self.assertEqual(signal_source_components(_filt("both")), {"new", "redelivered"})
        self.assertEqual(signal_source_components(_filt("new_redelivered")), {"new", "redelivered"})

    def test_new_plus_first_milestone(self):
        self.assertEqual(signal_source_components(_filt("new_first_milestone")), {"new", "first_milestone"})

    def test_redelivered_plus_first_milestone(self):
        self.assertEqual(
            signal_source_components(_filt("redelivered_first_milestone")),
            {"redelivered", "first_milestone"},
        )

    def test_all_three_combined(self):
        self.assertEqual(
            signal_source_components(_filt("new_redelivered_first_milestone")),
            {"new", "redelivered", "first_milestone"},
        )

    def test_default_and_unknown_values_fall_back_to_new_redelivered(self):
        self.assertEqual(signal_source_components(_filt(None)), {"new", "redelivered"})
        self.assertEqual(signal_source_components(_filt("")), {"new", "redelivered"})
        self.assertEqual(signal_source_components(_filt("not_a_real_value")), {"new", "redelivered"})


class SignalSourceMatchesExistingBehaviorTests(unittest.TestCase):
    """The New/Redelivered signal-scan gate must behave exactly as before
    for every pre-existing setting value."""

    def test_new_only_matches_non_redelivered_signal(self):
        self.assertTrue(_signal_source_matches(_signal(was_redelivered=False), _filt("new")))

    def test_new_only_rejects_redelivered_signal(self):
        self.assertFalse(_signal_source_matches(_signal(was_redelivered=True), _filt("new")))

    def test_redelivered_only_matches_redelivered_signal(self):
        self.assertTrue(_signal_source_matches(_signal(was_redelivered=True), _filt("redelivered")))

    def test_redelivered_only_rejects_non_redelivered_signal(self):
        self.assertFalse(_signal_source_matches(_signal(was_redelivered=False), _filt("redelivered")))

    def test_both_matches_either(self):
        self.assertTrue(_signal_source_matches(_signal(was_redelivered=False), _filt("both")))
        self.assertTrue(_signal_source_matches(_signal(was_redelivered=True), _filt("both")))

    def test_new_redelivered_matches_either(self):
        self.assertTrue(_signal_source_matches(_signal(was_redelivered=False), _filt("new_redelivered")))
        self.assertTrue(_signal_source_matches(_signal(was_redelivered=True), _filt("new_redelivered")))


class FirstMilestoneNeverTriggersSignalScanTests(unittest.TestCase):
    """A wallet with ONLY First Milestone selected must never have its
    normal New/Redelivered signal-scan path (_try_auto_buy /
    scan_and_execute) fire -- First Milestone has its own dedicated
    trigger (run_first_milestone_auto_buy), not this one."""

    def test_first_milestone_only_never_matches_new_signal(self):
        self.assertFalse(_signal_source_matches(_signal(was_redelivered=False), _filt("first_milestone")))

    def test_first_milestone_only_never_matches_redelivered_signal(self):
        self.assertFalse(_signal_source_matches(_signal(was_redelivered=True), _filt("first_milestone")))

    def test_combined_settings_still_gate_the_scan_path_by_new_redelivered_only(self):
        # "new_first_milestone" still only matches non-redelivered signals
        # on the SCAN path; first_milestone's own trigger is independent.
        self.assertTrue(_signal_source_matches(_signal(was_redelivered=False), _filt("new_first_milestone")))
        self.assertFalse(_signal_source_matches(_signal(was_redelivered=True), _filt("new_first_milestone")))


class FirstMilestoneSnapshotTextTests(unittest.TestCase):
    """The First Milestone Snapshot must include the token's existing
    tracked data and must never be worded as a new/fresh signal."""

    def setUp(self):
        self.signal = _signal()
        self.text = _build_first_milestone_snapshot_text(self.signal, "+25%", 125_000.0, 1.25)

    def test_includes_core_identity_fields(self):
        self.assertIn("TEST", self.text)
        self.assertIn("Test Token", self.text)
        self.assertIn("ABCPUMP", self.text)

    def test_includes_entry_and_current_market_cap(self):
        self.assertIn("$100.00K", self.text)  # entry MC
        self.assertIn("$125.00K", self.text)  # current MC

    def test_includes_gain_and_multiple_and_milestone_label(self):
        self.assertIn("+25%", self.text)
        self.assertIn("1.25x", self.text)

    def test_includes_entry_and_current_price(self):
        self.assertIn("$0.00100000", self.text)  # entry price
        self.assertIn("$0.00400000", self.text)  # current price

    def test_includes_ath_when_available(self):
        self.assertIn("4.00x", self.text)

    def test_includes_existing_snapshot_data_when_available(self):
        self.assertIn("250", self.text)  # total_holders
        self.assertIn("5.5%", self.text)  # top_holder_pct
        self.assertIn("2.0%", self.text)  # dev_holding_pct
        self.assertIn("1.5%", self.text)  # bundle_pct

    def test_never_worded_as_a_new_or_fresh_signal(self):
        lowered = self.text.lower()
        self.assertIn("not a new call", lowered)
        self.assertNotIn("new signal", lowered)

    def test_omits_missing_snapshot_fields_without_raising(self):
        bare_signal = _signal(
            total_holders=None, top_holder_pct=None, dev_holding_pct=None,
            bundle_pct=None, ath_multiple=None,
        )
        text = _build_first_milestone_snapshot_text(bare_signal, "2X", 200_000.0, 2.0)
        self.assertIn("2X", text)
        self.assertIn("2.00x", text)


if __name__ == "__main__":
    unittest.main()
