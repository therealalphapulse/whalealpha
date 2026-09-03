"""Tests for the Signal Quality & Alert Qualification Upgrade:
qualification.evaluate_signal_readiness() (combined score + confidence
decision, replacing the old two-independent-serial-hard-gates stack),
qualification.candidate_worth_full_enrichment() (the widened pre-
enrichment resource filter), and the scoring.py momentum velocity/
acceleration addition plus compute_confidence()'s new
"required_confirmations" field.

Existing qualify_candidate() behavior (tests/test_final_score_qualification.py,
tests/test_qualification_score_separation.py) is untouched by this change
and is not re-tested here.
"""

import unittest

from domain.signals.qualification import (
    evaluate_signal_readiness,
    candidate_worth_full_enrichment,
    qualify_candidate,
    READY_ELITE,
    READY_STRONG,
    READY_EMERGING_WATCH,
    READY_REJECT,
    SCORE_NEAR_MISS_BAND,
    CONFIDENCE_NEAR_MISS_BAND,
    COMPENSATION_STRENGTH_MARGIN,
)
from domain.signals.scoring import compute_confidence, _score_momentum_quality

MIN_CONFIDENCE_SCORE = 55.0


def _confidence(score, confirmed, checked, required):
    return {
        "confidence_score": score,
        "confirmed_count": confirmed,
        "checked_count": checked,
        "required_confirmations": required,
        "meets_bar": None,  # not consulted by evaluate_signal_readiness
    }


class CandidateWorthFullEnrichmentTests(unittest.TestCase):
    def test_at_or_above_cutoff_is_worth_enrichment(self):
        self.assertTrue(candidate_worth_full_enrichment(80.0, 74.0))
        self.assertTrue(candidate_worth_full_enrichment(74.0, 74.0))

    def test_within_near_miss_band_is_worth_enrichment(self):
        self.assertTrue(candidate_worth_full_enrichment(74.0 - SCORE_NEAR_MISS_BAND, 74.0))

    def test_beyond_near_miss_band_is_not_worth_enrichment(self):
        self.assertFalse(candidate_worth_full_enrichment(74.0 - SCORE_NEAR_MISS_BAND - 0.1, 74.0))

    def test_far_below_cutoff_is_not_worth_enrichment(self):
        self.assertFalse(candidate_worth_full_enrichment(42.3, 74.0))


class EvaluateSignalReadinessCleanPassTests(unittest.TestCase):
    def test_clears_both_dimensions_is_strong(self):
        d = evaluate_signal_readiness(85.0, 74.0, _confidence(70, 4, 6, 3), MIN_CONFIDENCE_SCORE)
        self.assertTrue(d.ready)
        self.assertEqual(d.tier, READY_STRONG)

    def test_far_above_both_dimensions_is_elite(self):
        d = evaluate_signal_readiness(95.0, 74.0, _confidence(90, 6, 7, 3), MIN_CONFIDENCE_SCORE)
        self.assertTrue(d.ready)
        self.assertEqual(d.tier, READY_ELITE)

    def test_exactly_at_both_bars_clears(self):
        d = evaluate_signal_readiness(74.0, 74.0, _confidence(55.0, 3, 5, 3), MIN_CONFIDENCE_SCORE)
        self.assertTrue(d.ready)


class EvaluateSignalReadinessCompensationTests(unittest.TestCase):
    """The core fix: score and confidence are no longer two independent
    single points of failure -- a candidate strong on one can be forgiven
    a small, bounded shortfall on the other."""

    def test_score_near_miss_compensated_by_strong_confidence(self):
        # score 1 point below cutoff (within SCORE_NEAR_MISS_BAND=4),
        # confidence far above its own bar (comfortably clears
        # COMPENSATION_STRENGTH_MARGIN=8 above MIN_CONFIDENCE_SCORE=55).
        d = evaluate_signal_readiness(73.0, 74.0, _confidence(80, 6, 7, 3), MIN_CONFIDENCE_SCORE)
        self.assertTrue(d.ready)
        self.assertEqual(d.tier, READY_EMERGING_WATCH)
        self.assertIn("SCORE_NEAR_MISS", d.reason)

    def test_confidence_near_miss_compensated_by_strong_score(self):
        # confidence 5 points below bar (within CONFIDENCE_NEAR_MISS_BAND=12),
        # score far above cutoff (comfortably clears COMPENSATION_STRENGTH_MARGIN=8).
        d = evaluate_signal_readiness(90.0, 74.0, _confidence(50, 3, 6, 3), MIN_CONFIDENCE_SCORE)
        self.assertTrue(d.ready)
        self.assertEqual(d.tier, READY_EMERGING_WATCH)
        self.assertIn("CONFIDENCE_NEAR_MISS", d.reason)

    def test_score_near_miss_not_compensated_by_merely_passing_confidence(self):
        # confidence clears its bar but only barely (not comfortably above
        # it by COMPENSATION_STRENGTH_MARGIN) -- must not compensate.
        d = evaluate_signal_readiness(73.0, 74.0, _confidence(56.0, 3, 5, 3), MIN_CONFIDENCE_SCORE)
        self.assertFalse(d.ready)
        self.assertEqual(d.tier, READY_REJECT)

    def test_confidence_near_miss_not_compensated_by_merely_passing_score(self):
        d = evaluate_signal_readiness(74.5, 74.0, _confidence(50, 3, 6, 3), MIN_CONFIDENCE_SCORE)
        self.assertFalse(d.ready)
        self.assertEqual(d.tier, READY_REJECT)


class EvaluateSignalReadinessRejectionTests(unittest.TestCase):
    def test_score_beyond_near_miss_band_rejects_regardless_of_confidence(self):
        d = evaluate_signal_readiness(65.0, 74.0, _confidence(95, 7, 7, 3), MIN_CONFIDENCE_SCORE)
        self.assertFalse(d.ready)
        self.assertEqual(d.tier, READY_REJECT)
        self.assertIn("BELOW_DYNAMIC_CUTOFF", d.reason)

    def test_confidence_beyond_near_miss_band_rejects_regardless_of_score(self):
        d = evaluate_signal_readiness(95.0, 74.0, _confidence(30, 3, 6, 3), MIN_CONFIDENCE_SCORE)
        self.assertFalse(d.ready)
        self.assertEqual(d.tier, READY_REJECT)
        self.assertIn("BELOW_CONFIDENCE_BAR", d.reason)

    def test_both_near_miss_neither_strong_enough_rejects(self):
        d = evaluate_signal_readiness(72.0, 74.0, _confidence(50, 3, 6, 3), MIN_CONFIDENCE_SCORE)
        self.assertFalse(d.ready)
        self.assertIn("BELOW_BOTH", d.reason)

    def test_weak_on_both_rejects(self):
        d = evaluate_signal_readiness(50.0, 74.0, _confidence(20, 1, 6, 3), MIN_CONFIDENCE_SCORE)
        self.assertFalse(d.ready)


class EvaluateSignalReadinessConfirmationsFloorTests(unittest.TestCase):
    """The 'multiple independent things must actually agree' floor is
    enforced unconditionally -- neither score nor confidence_score can
    compensate for too few confirmed checks."""

    def test_insufficient_confirmations_rejects_even_with_excellent_score_and_confidence(self):
        d = evaluate_signal_readiness(95.0, 74.0, _confidence(90, 1, 7, 3), MIN_CONFIDENCE_SCORE)
        self.assertFalse(d.ready)
        self.assertIn("INSUFFICIENT_CONFIRMATIONS", d.reason)

    def test_zero_checked_rejects(self):
        d = evaluate_signal_readiness(95.0, 74.0, _confidence(0, 0, 0, 0), MIN_CONFIDENCE_SCORE)
        self.assertFalse(d.ready)


class QualifyCandidateBackwardCompatibilityTests(unittest.TestCase):
    """qualify_candidate() itself is untouched by this upgrade."""

    def test_unchanged_behavior(self):
        self.assertTrue(qualify_candidate(68.0, 76.0, final_score=76.0).qualified)
        self.assertFalse(qualify_candidate(68.0, 76.0).qualified)


class ComputeConfidenceRequiredConfirmationsFieldTests(unittest.TestCase):
    """scoring.compute_confidence() now also returns
    required_confirmations, additively -- existing fields/values unchanged."""

    def test_required_confirmations_field_present_and_correct(self):
        result = compute_confidence(80.0, {"a": True, "b": True, "c": True, "d": True, "e": True, "f": False, "g": None})
        self.assertIn("required_confirmations", result)
        self.assertEqual(result["confidence_score"], 84.0)
        self.assertEqual(result["confirmed_count"], 5)
        self.assertEqual(result["checked_count"], 6)
        self.assertTrue(result["meets_bar"])

    def test_thin_data_required_confirmations_capped_at_checked_count(self):
        result = compute_confidence(80.0, {"a": True})
        self.assertEqual(result["required_confirmations"], 1)


class MomentumAccelerationScoringTests(unittest.TestCase):
    """Velocity/acceleration addition to _score_momentum_quality(): a
    move whose 1h pace outruns its own 6h/24h pace scores higher than an
    otherwise-identical move at a steady (non-accelerating) pace, but the
    existing 0-30 category cap is never exceeded."""

    def _base(self, **overrides):
        data = {
            "liquidity": 30000, "market_cap": 100000, "volume_1h": 25000,
            "txns_1h_buys": 10, "txns_1h_sells": 8,
            "price_change_1h": 5, "price_change_6h": 30, "price_change_24h": 100,
        }
        data.update(overrides)
        return data

    def test_accelerating_move_scores_higher_than_steady_move(self):
        steady_pts, _ = _score_momentum_quality(self._base(), {})
        accel_pts, accel_notes = _score_momentum_quality(
            self._base(price_change_1h=20, price_change_6h=12, price_change_24h=24), {}
        )
        self.assertGreater(accel_pts, steady_pts)
        self.assertTrue(any("Accelerating" in n for n in accel_notes))

    def test_equal_pace_earns_no_acceleration_bonus(self):
        # 1h rate == 6h rate exactly -- not accelerating, no bonus/no note.
        pts, notes = _score_momentum_quality(
            self._base(price_change_1h=5, price_change_6h=30, price_change_24h=120), {}
        )
        self.assertFalse(any("Accelerating" in n or "accelerating" in n for n in notes))

    def test_category_cap_still_enforced(self):
        maxed = self._base(
            price_change_1h=200, price_change_6h=20, price_change_24h=10,
            txns_1h_buys=100, txns_1h_sells=5, market_cap=60000,
        )
        pts, _ = _score_momentum_quality(maxed, {})
        self.assertLessEqual(pts, 30.0)

    def test_no_acceleration_bonus_never_penalizes(self):
        # A token with negative/flat momentum must not score lower purely
        # for lacking the acceleration bonus -- additive-only convention.
        flat = self._base(price_change_1h=0, price_change_6h=0, price_change_24h=0)
        pts, _ = _score_momentum_quality(flat, {})
        self.assertGreaterEqual(pts, 0.0)


if __name__ == "__main__":
    unittest.main()
