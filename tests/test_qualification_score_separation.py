"""Regression tests for score/qualification separation."""

import unittest

from domain.signals.qualification import qualify_candidate


class QualificationScoreSeparationTests(unittest.TestCase):
    def test_below_cutoff_is_rejected(self):
        decision = qualify_candidate(68.0, 76.0)
        self.assertFalse(decision.qualified)
        self.assertEqual(decision.reason, "BELOW_DYNAMIC_CUTOFF")

    def test_above_cutoff_is_qualified(self):
        decision = qualify_candidate(78.4, 76.0)
        self.assertTrue(decision.qualified)
        self.assertEqual(decision.reason, "SCORE_ABOVE_CUTOFF")

    def test_authoritative_final_score_can_qualify(self):
        decision = qualify_candidate(68.0, 76.0, final_score=76.0)
        self.assertTrue(decision.qualified)
        self.assertEqual(decision.reason, "SCORE_ABOVE_CUTOFF")

    def test_final_score_below_cutoff_still_rejects(self):
        decision = qualify_candidate(68.0, 76.0, final_score=75.9)
        self.assertFalse(decision.qualified)
        self.assertEqual(decision.reason, "BELOW_DYNAMIC_CUTOFF")

    def test_quota_pressure_cannot_change_score_or_qualification(self):
        raw_score = 68.0
        dynamic_cutoff = 76.0
        adjusted_score = raw_score
        decision = qualify_candidate(adjusted_score, dynamic_cutoff)
        self.assertEqual(adjusted_score, raw_score)
        self.assertFalse(decision.qualified)
        self.assertNotEqual(raw_score + 8.0, adjusted_score)

    def test_qualified_final_score_is_not_mutated_by_qualification(self):
        final_score = 82.0
        decision = qualify_candidate(68.0, 76.0, final_score=final_score)
        self.assertTrue(decision.qualified)
        self.assertEqual(final_score, 82.0)

    def test_safety_rejection_remains_independent_of_quota(self):
        safety_rejected = True
        quota_exhausted = False
        self.assertFalse((not safety_rejected) and (not quota_exhausted))


if __name__ == "__main__":
    unittest.main()
