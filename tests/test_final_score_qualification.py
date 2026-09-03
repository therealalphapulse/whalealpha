import unittest

from domain.signals.qualification import qualify_candidate


class FinalScoreQualificationTests(unittest.TestCase):
    def test_final_score_can_qualify_when_base_score_is_below_cutoff(self):
        decision = qualify_candidate(68.0, 76.0, final_score=76.0)
        self.assertTrue(decision.qualified)
        self.assertEqual(decision.reason, "SCORE_ABOVE_CUTOFF")

    def test_final_score_above_cutoff_qualifies(self):
        decision = qualify_candidate(73.0, 76.0, final_score=77.0)
        self.assertTrue(decision.qualified)
        self.assertEqual(decision.reason, "SCORE_ABOVE_CUTOFF")

    def test_final_score_below_cutoff_remains_rejected(self):
        decision = qualify_candidate(68.0, 76.0, final_score=75.9)
        self.assertFalse(decision.qualified)
        self.assertEqual(decision.reason, "BELOW_DYNAMIC_CUTOFF")

    def test_legacy_two_argument_behavior_is_preserved(self):
        decision = qualify_candidate(68.0, 76.0)
        self.assertFalse(decision.qualified)
        self.assertEqual(decision.reason, "BELOW_DYNAMIC_CUTOFF")


if __name__ == "__main__":
    unittest.main()
