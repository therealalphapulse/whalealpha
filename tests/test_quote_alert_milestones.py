"""
tests/test_quote_alert_milestones.py

Regression coverage for the Quote Alert milestone-ladder fix in
domain/signals/signal_tracker.py.

Root cause under test: the lifecycle loop used to alert on whatever the
*current* live gain happened to be at poll time, gated only by
"gain > last_alerted". That meant a price that jumped several ladder rungs
between two polls only ever produced ONE alert (for the current gain),
silently skipping every intermediate rung, and the alert label was derived
from the raw instantaneous gain rather than a clean ladder rung ("2.37X"
instead of "2X"), which also meant it fell through to the generic MULTI_X
bucket instead of the correct named milestone.

_milestones_crossed(last_alerted, gain) is the pure function that replaced
that logic: given the last multiple actually alerted and the current gain,
it returns every ladder rung in (last_alerted, gain] in ascending order, so
the caller can fire exactly one alert per rung — never zero (missed) and
never more than once per rung (duplicate/spam) — regardless of how far the
price moved in a single poll. This file exercises it directly, plus
_milestone_enum()'s mapping of ladder labels to the DB enum bucket.

Ladder under test: +25% -> +50% -> 2X -> 3X -> ... -> NX, for every integer
X indefinitely.
"""

import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/test")

from domain.signals.signal_tracker import (  # noqa: E402
    Milestone,
    _milestone_enum,
    _milestones_crossed,
)


class NoMilestoneCrossedTests(unittest.TestCase):
    """Below the first rung, or no new rung reached, must never alert."""

    def test_below_first_rung_is_not_crossed(self):
        self.assertEqual(_milestones_crossed(1.0, 1.10), [])

    def test_exactly_at_last_alerted_is_not_re_crossed(self):
        # A poll that reports the same multiple already alerted (e.g. price
        # is flat) must not re-fire that milestone.
        self.assertEqual(_milestones_crossed(2.0, 2.0), [])

    def test_between_two_already_alerted_integer_rungs_is_not_crossed(self):
        # Regression for the "2.4X" bug: once 2X has been alerted, further
        # polls landing between 2X and 3X (2.1, 2.5, 2.99...) must produce
        # no alert at all, not a fuzzy re-alert.
        self.assertEqual(_milestones_crossed(2.0, 2.99), [])

    def test_gain_below_or_equal_last_alerted_never_crosses(self):
        self.assertEqual(_milestones_crossed(5.0, 4.0), [])
        self.assertEqual(_milestones_crossed(5.0, 5.0), [])


class SingleRungBoundaryTests(unittest.TestCase):
    """Each named rung fires exactly once, right at its boundary."""

    def test_pct_25_boundary(self):
        self.assertEqual(_milestones_crossed(1.0, 1.25), [(1.25, "+25%")])

    def test_just_below_pct_25_does_not_fire(self):
        self.assertEqual(_milestones_crossed(1.0, 1.249999), [])

    def test_pct_50_boundary_from_pct_25(self):
        self.assertEqual(_milestones_crossed(1.25, 1.50), [(1.50, "+50%")])

    def test_2x_boundary_from_pct_50(self):
        self.assertEqual(_milestones_crossed(1.50, 2.0), [(2.0, "2X")])

    def test_3x_boundary_from_2x(self):
        self.assertEqual(_milestones_crossed(2.0, 3.0), [(3.0, "3X")])

    def test_7x_boundary(self):
        self.assertEqual(_milestones_crossed(6.0, 7.0), [(7.0, "7X")])

    def test_8x_boundary(self):
        self.assertEqual(_milestones_crossed(7.0, 8.0), [(8.0, "8X")])

    def test_9x_boundary(self):
        self.assertEqual(_milestones_crossed(8.0, 9.0), [(9.0, "9X")])

    def test_10x_boundary(self):
        self.assertEqual(_milestones_crossed(9.0, 10.0), [(10.0, "10X")])

    def test_11x_boundary(self):
        self.assertEqual(_milestones_crossed(10.0, 11.0), [(11.0, "11X")])

    def test_ladder_continues_indefinitely_past_11x(self):
        self.assertEqual(_milestones_crossed(24.0, 25.0), [(25.0, "25X")])
        self.assertEqual(_milestones_crossed(99.0, 100.0), [(100.0, "100X")])


class MultiMilestoneJumpTests(unittest.TestCase):
    """A price that jumps several rungs in one poll must yield ALL of them,
    in order, without skipping or duplicating any."""

    def test_jump_from_start_straight_to_2x_hits_25_50_2x(self):
        self.assertEqual(
            _milestones_crossed(1.0, 2.5),
            [(1.25, "+25%"), (1.50, "+50%"), (2.0, "2X")],
        )

    def test_jump_from_start_to_9x_hits_every_rung_in_order(self):
        self.assertEqual(
            _milestones_crossed(1.0, 9.9),
            [
                (1.25, "+25%"),
                (1.50, "+50%"),
                (2.0, "2X"),
                (3.0, "3X"),
                (4.0, "4X"),
                (5.0, "5X"),
                (6.0, "6X"),
                (7.0, "7X"),
                (8.0, "8X"),
                (9.0, "9X"),
            ],
        )

    def test_jump_across_double_digit_rungs(self):
        self.assertEqual(
            _milestones_crossed(9.0, 11.2),
            [(10.0, "10X"), (11.0, "11X")],
        )

    def test_jump_already_past_pct_milestones_only_hits_integer_rungs(self):
        # last_alerted already beyond +25%/+50% (e.g. 2X was previously
        # alerted) — a jump to 5X must not re-fire the percentage rungs.
        self.assertEqual(
            _milestones_crossed(2.0, 5.4),
            [(3.0, "3X"), (4.0, "4X"), (5.0, "5X")],
        )

    def test_no_duplicate_alert_for_a_rung_already_at_last_alerted(self):
        # last_alerted sits exactly on an integer rung already claimed by a
        # previous alert; the next poll's jump must start strictly after it.
        crossed = _milestones_crossed(6.0, 9.0)
        self.assertEqual(crossed, [(7.0, "7X"), (8.0, "8X"), (9.0, "9X")])
        self.assertNotIn((6.0, "6X"), crossed)


class MilestoneEnumMappingTests(unittest.TestCase):
    """The DB enum bucket must be stable and never raise for any label the
    ladder can produce, including the indefinite tail beyond the explicitly
    named enum values."""

    def test_named_rungs_map_to_their_own_enum_value(self):
        self.assertEqual(_milestone_enum("+25%"), Milestone.PCT_25)
        self.assertEqual(_milestone_enum("+50%"), Milestone.PCT_50)
        self.assertEqual(_milestone_enum("2X"), Milestone.TWO_X)
        self.assertEqual(_milestone_enum("3X"), Milestone.THREE_X)
        self.assertEqual(_milestone_enum("4X"), Milestone.FOUR_X)
        self.assertEqual(_milestone_enum("5X"), Milestone.FIVE_X)
        self.assertEqual(_milestone_enum("6X"), Milestone.SIX_X)
        self.assertEqual(_milestone_enum("10X"), Milestone.TEN_X)

    def test_unnamed_rungs_fall_back_to_multi_x_without_raising(self):
        for label in ("7X", "8X", "9X", "11X", "25X", "100X"):
            self.assertEqual(_milestone_enum(label), Milestone.MULTI_X)


if __name__ == "__main__":
    unittest.main()
