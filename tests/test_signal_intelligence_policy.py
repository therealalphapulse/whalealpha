"""Regression coverage for signal-intelligence scoring policy.

These tests intentionally protect the architecture rather than tuning weights
without historical outcome data: holder retrieval is safety/uncertainty data,
while narrative and momentum are bounded quality evidence.
"""

import unittest
from unittest.mock import patch

from domain.signals.scoring import (
    HARD_FLOOR_CUTOFF,
    MAX_MULTIPLIER,
    _narrative_social_multiplier,
    _score_momentum_quality,
    hard_reject_reasons,
)


class SignalIntelligencePolicyTests(unittest.TestCase):
    def test_holder_unavailable_does_not_create_fake_holder_risk(self):
        data = {
            "liquidity": 25_000,
            "market_cap": 100_000,
        }
        holder_analysis = {
            "holder_analysis_status": "unavailable_early_token",
            "top_holder_pct": None,
            "top10_pct": None,
            "bundle_pct": None,
            "dev_holding_pct": None,
        }

        reasons = hard_reject_reasons(data, {}, holder_analysis, "TokenMint")

        self.assertFalse(any("holder" in r.lower() for r in reasons))
        self.assertFalse(any("bundle" in r.lower() for r in reasons))

    def test_narrative_cannot_rescue_below_hard_floor(self):
        bonus, _ = _narrative_social_multiplier(
            {"name": "AI Agent Pepe", "symbol": "AIP"},
            HARD_FLOOR_CUTOFF - 0.1,
        )
        self.assertEqual(bonus, 0.0)

    def test_narrative_bonus_is_bounded(self):
        bonus, notes = _narrative_social_multiplier(
            {"name": "AI Agent Pepe", "symbol": "AIP"},
            HARD_FLOOR_CUTOFF,
        )
        self.assertGreaterEqual(bonus, 0.0)
        self.assertLessEqual(bonus, MAX_MULTIPLIER)
        self.assertTrue(notes)

    def test_momentum_manufactured_volume_is_penalized(self):
        data = {
            "liquidity": 40_000,
            "market_cap": 100_000,
            "volume_1h": 150_000,
            "txns_1h_buys": 80,
            "txns_1h_sells": 20,
            "price_change_1h": 25,
            "price_change_6h": 30,
            "price_change_24h": 40,
        }
        holder_analysis = {}

        with patch(
            "domain.signals.scoring.estimate_fake_volume_ratio",
            return_value=0.0,
        ), patch(
            "domain.signals.scoring.estimate_wash_trading_risk",
            return_value=0.0,
        ):
            clean_points, _ = _score_momentum_quality(data, holder_analysis)

        with patch(
            "domain.signals.scoring.estimate_fake_volume_ratio",
            return_value=0.8,
        ), patch(
            "domain.signals.scoring.estimate_wash_trading_risk",
            return_value=0.8,
        ):
            penalized_points, notes = _score_momentum_quality(data, holder_analysis)

        self.assertLess(penalized_points, clean_points)
        self.assertTrue(any("risk" in n.lower() for n in notes))


if __name__ == "__main__":
    unittest.main()
