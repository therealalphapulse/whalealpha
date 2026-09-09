"""
Regression tests for the King Token pattern bonus
(domain.signals.scoring._king_pattern_bonus) and its wiring into
score_candidate().

Mirrors the pure-function-under-test convention already used by
tests/test_quote_alert_milestones.py: import the private function
directly rather than standing up a full candidate/DB fixture, since
the bonus itself takes plain numeric inputs and an optional profile
dict -- no DB/IO.
"""

import unittest

from domain.signals.scoring import (
    HARD_FLOOR_CUTOFF,
    KING_PATTERN_MAX_BONUS,
    KING_PATTERN_MIN_SAMPLE_SIZE,
    _king_pattern_bonus,
)

# A representative King profile: 5 proven multi-milestone winners,
# averaging these four sub-scores at entry time.
_PROFILE = {
    "sample_size": 5,
    "centroid": {
        "liquidity_lp_integrity": 20.0,
        "holder_distribution": 20.0,
        "momentum_quality": 25.0,
        "wallet_deployer_behavior": 15.0,
    },
}


class KingPatternBonusTests(unittest.TestCase):
    def test_no_profile_gives_zero_bonus(self):
        bonus, notes = _king_pattern_bonus(90.0, 20.0, 20.0, 25.0, 15.0, None)
        self.assertEqual(bonus, 0.0)
        self.assertEqual(notes, [])

    def test_empty_profile_dict_gives_zero_bonus(self):
        bonus, notes = _king_pattern_bonus(90.0, 20.0, 20.0, 25.0, 15.0, {})
        self.assertEqual(bonus, 0.0)

    def test_below_hard_floor_gives_zero_bonus_even_with_perfect_match(self):
        # The whole point: this bonus can never rescue a token that
        # failed on-chain quality -- it can only add on top.
        bonus, notes = _king_pattern_bonus(
            HARD_FLOOR_CUTOFF - 1, 20.0, 20.0, 25.0, 15.0, _PROFILE
        )
        self.assertEqual(bonus, 0.0)
        self.assertEqual(notes, [])

    def test_at_hard_floor_with_perfect_match_gives_full_bonus(self):
        bonus, _ = _king_pattern_bonus(
            HARD_FLOOR_CUTOFF, 20.0, 20.0, 25.0, 15.0, _PROFILE
        )
        self.assertEqual(bonus, KING_PATTERN_MAX_BONUS)

    def test_sample_size_below_minimum_gives_zero_bonus(self):
        thin_profile = {
            "sample_size": KING_PATTERN_MIN_SAMPLE_SIZE - 1,
            "centroid": _PROFILE["centroid"],
        }
        bonus, notes = _king_pattern_bonus(90.0, 20.0, 20.0, 25.0, 15.0, thin_profile)
        self.assertEqual(bonus, 0.0)
        self.assertEqual(notes, [])

    def test_sample_size_at_minimum_can_earn_bonus(self):
        thin_profile = {
            "sample_size": KING_PATTERN_MIN_SAMPLE_SIZE,
            "centroid": _PROFILE["centroid"],
        }
        bonus, _ = _king_pattern_bonus(90.0, 20.0, 20.0, 25.0, 15.0, thin_profile)
        self.assertEqual(bonus, KING_PATTERN_MAX_BONUS)

    def test_perfect_match_earns_max_bonus_and_a_note(self):
        bonus, notes = _king_pattern_bonus(90.0, 20.0, 20.0, 25.0, 15.0, _PROFILE)
        self.assertEqual(bonus, KING_PATTERN_MAX_BONUS)
        self.assertEqual(len(notes), 1)
        self.assertIn("proven multi-milestone winner", notes[0])

    def test_far_mismatch_earns_zero_bonus(self):
        bonus, notes = _king_pattern_bonus(90.0, 2.0, 2.0, 3.0, 1.0, _PROFILE)
        self.assertEqual(bonus, 0.0)
        self.assertEqual(notes, [])

    def test_partial_match_earns_a_bonus_between_zero_and_max(self):
        # Close on liquidity/holders, off on momentum/wallet.
        bonus, _ = _king_pattern_bonus(90.0, 19.0, 19.0, 10.0, 5.0, _PROFILE)
        self.assertGreaterEqual(bonus, 0.0)
        self.assertLessEqual(bonus, KING_PATTERN_MAX_BONUS)

    def test_centroid_with_only_one_overlapping_component_gives_zero(self):
        thin_centroid = {
            "sample_size": 5,
            "centroid": {"liquidity_lp_integrity": 20.0},
        }
        bonus, notes = _king_pattern_bonus(90.0, 20.0, 20.0, 25.0, 15.0, thin_centroid)
        self.assertEqual(bonus, 0.0)
        self.assertEqual(notes, [])

    def test_bonus_never_exceeds_max(self):
        # Even an implausibly perfect match can't exceed the cap.
        bonus, _ = _king_pattern_bonus(100.0, 20.0, 20.0, 25.0, 15.0, _PROFILE)
        self.assertLessEqual(bonus, KING_PATTERN_MAX_BONUS)


class KingPatternBonusBackwardCompatibilityTests(unittest.TestCase):
    """score_candidate() itself must behave identically to before this
    feature for every existing caller that doesn't pass king_profile."""

    def test_score_candidate_accepts_call_without_king_profile(self):
        from domain.signals.scoring import score_candidate

        data = {
            "priceUsd": "0.001",
            "marketCap": 150000,
            "liquidity": {"usd": 40000},
            "volume": {"h24": 200000, "h1": 30000, "m5": 5000},
            "priceChange": {"h24": 40, "h1": 8, "m5": 1},
            "txns": {"h24": {"buys": 400, "sells": 250}, "h1": {"buys": 60, "sells": 40}},
            "pairCreatedAt": 0,
        }
        # Must not raise for callers written before king_profile existed.
        result = score_candidate(data, None, None, None, "SomeContract111")
        self.assertIn("breakdown", result)
        self.assertEqual(result["breakdown"].get("king_pattern_bonus"), 0.0)
        self.assertEqual(result["breakdown"].get("king_profile_sample_size"), 0)


if __name__ == "__main__":
    unittest.main()
