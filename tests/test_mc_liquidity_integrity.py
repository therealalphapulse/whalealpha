"""Regression coverage for MC/Liquidity integrity and the non-blocking
holder-retrieval policy.

Context: production alerts were observed with market cap well BELOW
liquidity (e.g. MC $41.2K / Liquidity $67.7K) even though
passes_mc_liquidity_gate() requires MC >= 2x liquidity. Investigation
found that the gate computed "mc" via a float-safe fallback
(_to_float(market_cap) or _to_float(fdv)) while the alert card and the
auto-buy filter matcher each did their own raw-value fallback
(d.get('market_cap') or d.get('fdv')) -- which disagree whenever
market_cap is a falsy-as-float-but-truthy-as-string value like "0" or
"N/A", and gave DexScreener's two different valuation fields
(circulating market_cap vs fully-diluted fdv) three independent,
sometimes-disagreeing call sites instead of one.

These tests protect the fix, not just the historical incident:
effective_market_cap() must be the ONLY place that decides which
number counts, and it must be provably the same value gate/card/filter
all use.
"""

import unittest

from domain.signals.scoring import (
    effective_market_cap,
    passes_mc_liquidity_gate,
    hard_reject_reasons,
    MC_LP_MIN_RATIO,
)


class EffectiveMarketCapTests(unittest.TestCase):
    def test_prefers_smaller_of_two_nonzero_valuations(self):
        self.assertEqual(
            effective_market_cap({"market_cap": 41_200, "fdv": 164_800}), 41_200
        )
        self.assertEqual(
            effective_market_cap({"market_cap": 164_800, "fdv": 41_200}), 41_200
        )

    def test_falls_back_to_fdv_when_market_cap_missing(self):
        self.assertEqual(effective_market_cap({"fdv": 150_000}), 150_000)

    def test_falls_back_to_fdv_when_market_cap_is_na_string(self):
        self.assertEqual(effective_market_cap({"market_cap": "N/A", "fdv": 150_000}), 150_000)

    def test_falls_back_to_fdv_when_market_cap_is_zero_string(self):
        self.assertEqual(effective_market_cap({"market_cap": "0", "fdv": 150_000}), 150_000)

    def test_zero_when_both_unavailable(self):
        self.assertEqual(effective_market_cap({}), 0.0)


class McLiquidityGateTests(unittest.TestCase):
    def _base_data(self, market_cap, liquidity, fdv=None):
        data = {"contract": "TestMint1111111111111111", "liquidity": liquidity, "market_cap": market_cap}
        if fdv is not None:
            data["fdv"] = fdv
        return data

    def test_rejects_the_observed_production_incident_shape(self):
        data = self._base_data(41_200, 67_700)
        ok, reason = passes_mc_liquidity_gate(data)
        self.assertFalse(ok)
        self.assertIn("does not exceed liquidity", reason)

    def test_rejects_second_observed_incident_shape(self):
        data = self._base_data(41_500, 68_500)
        ok, reason = passes_mc_liquidity_gate(data)
        self.assertFalse(ok)

    def test_rejects_when_ratio_below_minimum_even_if_mc_exceeds_liq(self):
        liq = 50_000
        mc = liq * (MC_LP_MIN_RATIO - 0.1)
        data = self._base_data(mc, liq)
        ok, reason = passes_mc_liquidity_gate(data)
        self.assertFalse(ok)
        self.assertIn("ratio", reason)

    def test_passes_a_healthy_ratio(self):
        liq = 50_000
        mc = liq * (MC_LP_MIN_RATIO + 0.5)
        data = self._base_data(mc, liq)
        ok, reason = passes_mc_liquidity_gate(data)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_gate_and_display_now_agree_on_na_string_market_cap(self):
        liq = 50_000
        fdv = liq * (MC_LP_MIN_RATIO + 1)
        data = self._base_data("N/A", liq, fdv=fdv)
        ok, _ = passes_mc_liquidity_gate(data)
        self.assertTrue(ok)
        self.assertEqual(effective_market_cap(data), fdv)


class HolderNonBlockingPolicyTests(unittest.TestCase):
    def _data(self):
        return {"liquidity": 25_000, "market_cap": 100_000}

    def test_early_token_status_does_not_create_fake_holder_risk(self):
        holder_analysis = {
            "holder_analysis_status": "unavailable_early_token",
            "top_holder_pct": None,
            "top10_pct": None,
            "bundle_pct": None,
            "dev_holding_pct": None,
        }
        reasons = hard_reject_reasons(self._data(), {}, holder_analysis, "TokenMint")
        self.assertFalse(any("holder" in r.lower() for r in reasons))
        self.assertFalse(any("bundle" in r.lower() for r in reasons))

    def test_provider_degraded_status_does_not_create_fake_holder_risk(self):
        holder_analysis = {
            "holder_analysis_status": "unavailable_provider_degraded",
            "total_holders": None,
            "top_holder_pct": None,
            "dev_holding_pct": None,
            "bundle_wallet_count": 0,
            "bundle_pct": None,
            "top_holder_addresses": [],
        }
        reasons = hard_reject_reasons(self._data(), {}, holder_analysis, "TokenMint")
        self.assertFalse(any("holder" in r.lower() for r in reasons))
        self.assertFalse(any("bundle" in r.lower() for r in reasons))

    def test_genuine_holder_concentration_is_still_hard_rejected_when_data_exists(self):
        holder_analysis = {
            "holder_analysis_status": "ok",
            "top_holder_pct": 45.0,
            "top10_pct": 80.0,
            "bundle_pct": 0.0,
            "dev_holding_pct": 2.0,
        }
        reasons = hard_reject_reasons(self._data(), {}, holder_analysis, "TokenMint")
        self.assertTrue(any("wallet holds" in r.lower() for r in reasons))




class AdaptiveGateIntegrityTests(unittest.TestCase):
    """These specifically exercise the LIVE production gate, i.e. what
    passes_mc_liquidity_gate resolves to once sitecustomize.py has patched
    it -- not the unpatched scoring.py function. This is what actually ran
    in production for the observed incident."""

    def test_adaptive_gate_still_rejects_the_observed_production_incident_shape(self):
        from domain.signals.adaptive_filter_policy import adaptive_mc_liquidity_gate
        data = {"market_cap": 41_200, "liquidity": 67_700, "volume_1h": 23_900,
                "txns_1h_buys": 108, "txns_1h_sells": 29, "price_change_1h": 48}
        ok, reason = adaptive_mc_liquidity_gate(data)
        self.assertFalse(ok)
        self.assertIn("does not exceed liquidity", reason)

    def test_adaptive_gate_still_rejects_second_observed_incident_shape(self):
        from domain.signals.adaptive_filter_policy import adaptive_mc_liquidity_gate
        data = {"market_cap": 41_500, "liquidity": 68_500, "volume_1h": 19_600,
                "txns_1h_buys": 134, "txns_1h_sells": 34, "price_change_1h": 46}
        ok, reason = adaptive_mc_liquidity_gate(data)
        self.assertFalse(ok)

    def test_intentional_thin_liquidity_design_is_preserved(self):
        # From tests/test_adaptive_filter_policy.py: MC $500K / Liq $20K
        # (4% depth) was an intentional relaxation vs the old 50% floor.
        # mc/liq here is 25x -- well above MC_LP_MIN_RATIO -- so the
        # restored upper-bound invariant must NOT touch this case.
        from domain.signals.adaptive_filter_policy import adaptive_mc_liquidity_gate
        data = {"market_cap": 500_000, "liquidity": 20_000, "volume_1h": 25_000,
                "txns_1h_buys": 70, "txns_1h_sells": 30, "price_change_1h": 8}
        ok, reason = adaptive_mc_liquidity_gate(data)
        self.assertTrue(ok, reason)

    def test_monkeypatch_scenario_end_to_end(self):
        # Reproduce exactly what sitecustomize.py does at import time:
        # domain.signals.adaptive_filter_policy.install(scoring_module)
        # replaces scoring.passes_mc_liquidity_gate with the adaptive gate.
        # This is what pump_radar.analyze_candidate() actually calls in
        # production -- verify the PATCHED function, not the original,
        # rejects the observed incident shape.
        import importlib
        import domain.signals.scoring as scoring_module
        from domain.signals.adaptive_filter_policy import install

        install(scoring_module)
        self.assertTrue(getattr(scoring_module, "_adaptive_filter_policy_installed", False))

        data = {"market_cap": 41_200, "liquidity": 67_700, "volume_1h": 23_900,
                "txns_1h_buys": 108, "txns_1h_sells": 29, "price_change_1h": 48}
        ok, reason = scoring_module.passes_mc_liquidity_gate(data)
        self.assertFalse(ok, "the LIVE (patched) gate must reject MC < liquidity")


if __name__ == "__main__":
    unittest.main()
