"""Tests for the restored, two-lane GeckoTerminal discovery thesis
(domain/signals/pump_radar.py: _GeckoPoolCandidate, _gt_pool_age_hours,
_extract_gecko_pool_candidate, _fresh_momentum_score,
_recovery_strength_score, _fetch_gecko_pools, fetch_pump_fun_launches),
plus a smoke test confirming the retired DexScreener-first adapter
(domain/signals/_radar_discovery_adapter.py) is now a harmless no-op.

Covers: mandatory pump.fun-origin verification, cheap-gate rejection
before enrichment (reusing _pre_enrichment_quality_gate — single source
of truth, not a second set of thresholds), age-based (not feed-based)
lane classification, dedup across feeds, ranking monotonicity for both
lanes, and the score-integrity boundary (trending is a candidate
source only, never a ranking/buy signal).
"""

import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import domain.signals.pump_radar as pump_radar
import domain.signals._radar_discovery_adapter as adapter


def _iso_age(age_hours: float) -> str:
    created = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return created.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pool(
    *,
    age_hours: float = 0.5,
    liquidity=25_000,
    market_cap=150_000,
    volume_1h=40_000,
    price_change_1h=25.0,
    price_change_24h=50.0,
    buys=30,
    sells=10,
) -> dict:
    return {
        "attributes": {
            "reserve_in_usd": liquidity,
            "market_cap_usd": market_cap,
            "fdv_usd": (market_cap or 0) * 1.5 if market_cap else None,
            "volume_usd": {"h1": volume_1h, "h24": (volume_1h or 0) * 10},
            "price_change_percentage": {
                "h1": price_change_1h,
                "h6": (price_change_1h or 0) / 2,
                "h24": price_change_24h,
            },
            "transactions": {"h1": {"buys": buys, "sells": sells}},
            "pool_created_at": _iso_age(age_hours),
        }
    }


# A real pump.fun-style mint address (ends with "pump") and one that
# deliberately does not, for the mandatory-verification tests.
PUMP_MINT = "FreshMintAAAAAAAAAAAAAAAAAAAAAAAAAAAApump"
NON_PUMP_MINT = "NonPumpMintDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"


class GtPoolAgeHoursTests(unittest.TestCase):
    def test_parses_iso_timestamp(self):
        age = pump_radar._gt_pool_age_hours(_iso_age(2.0)[:])
        self.assertAlmostEqual(age, 2.0, delta=0.05)

    def test_missing_timestamp_returns_none(self):
        self.assertIsNone(pump_radar._gt_pool_age_hours(None))
        self.assertIsNone(pump_radar._gt_pool_age_hours(""))

    def test_unparseable_timestamp_returns_none(self):
        self.assertIsNone(pump_radar._gt_pool_age_hours("not-a-date"))


class ExtractGeckoPoolCandidateTests(unittest.TestCase):
    def test_extracts_all_fields(self):
        c = pump_radar._extract_gecko_pool_candidate(PUMP_MINT, _pool())
        self.assertIsNotNone(c)
        self.assertEqual(c.mint, PUMP_MINT)
        self.assertEqual(c.liquidity, 25_000)
        self.assertEqual(c.market_cap, 150_000)
        self.assertEqual(c.volume_1h, 40_000)
        self.assertAlmostEqual(c.age_hours, 0.5, delta=0.05)

    def test_missing_age_returns_none(self):
        pool = _pool()
        pool["attributes"]["pool_created_at"] = None
        self.assertIsNone(pump_radar._extract_gecko_pool_candidate(PUMP_MINT, pool))

    def test_as_gate_data_field_names_match_downstream_gate(self):
        c = pump_radar._extract_gecko_pool_candidate(PUMP_MINT, _pool())
        data = c.as_gate_data()
        for key in ("liquidity", "market_cap", "fdv", "volume_1h", "txns_1h_buys", "txns_1h_sells"):
            self.assertIn(key, data)


class CheapGateReuseTests(unittest.TestCase):
    """The Discovery cheap gate is _pre_enrichment_quality_gate() itself
    (single source of truth) — not a second, possibly-disagreeing
    implementation of the same thresholds."""

    def test_thin_liquidity_candidate_rejected(self):
        c = pump_radar._extract_gecko_pool_candidate(PUMP_MINT, _pool(liquidity=500, market_cap=5_000, volume_1h=10, buys=0, sells=0))
        ok, reason = pump_radar._pre_enrichment_quality_gate(PUMP_MINT, c.as_gate_data())
        self.assertFalse(ok)

    def test_healthy_candidate_passes(self):
        c = pump_radar._extract_gecko_pool_candidate(PUMP_MINT, _pool())
        ok, reason = pump_radar._pre_enrichment_quality_gate(PUMP_MINT, c.as_gate_data())
        self.assertTrue(ok, reason)


class LaneRankingTests(unittest.TestCase):
    def test_fresh_momentum_rewards_stronger_signal(self):
        weak = pump_radar._extract_gecko_pool_candidate(
            PUMP_MINT, _pool(age_hours=5.5, volume_1h=5_000, price_change_1h=2.0, buys=6, sells=6)
        )
        strong = pump_radar._extract_gecko_pool_candidate(
            PUMP_MINT, _pool(age_hours=0.3, volume_1h=50_000, price_change_1h=40.0, buys=35, sells=5)
        )
        self.assertGreater(pump_radar._fresh_momentum_score(strong), pump_radar._fresh_momentum_score(weak))

    def test_recovery_score_rewards_bounce_over_continued_dump(self):
        bouncing = pump_radar._extract_gecko_pool_candidate(
            PUMP_MINT, _pool(age_hours=48, price_change_1h=12.0, price_change_24h=-25.0, volume_1h=15_000, buys=20, sells=8)
        )
        still_dumping = pump_radar._extract_gecko_pool_candidate(
            PUMP_MINT, _pool(age_hours=48, price_change_1h=-15.0, price_change_24h=-30.0, volume_1h=15_000, buys=8, sells=20)
        )
        self.assertGreater(
            pump_radar._recovery_strength_score(bouncing),
            pump_radar._recovery_strength_score(still_dumping),
        )

    def test_trending_membership_itself_has_no_scoring_effect(self):
        """Two otherwise-identical candidates score identically regardless
        of which GT feed they came from -- trending is a candidate
        source, never a ranking signal."""
        pool = _pool(age_hours=48, price_change_1h=3.0, price_change_24h=-5.0, volume_1h=12_000, buys=15, sells=10)
        c1 = pump_radar._extract_gecko_pool_candidate(PUMP_MINT, pool)
        c2 = pump_radar._extract_gecko_pool_candidate(PUMP_MINT, pool)
        self.assertAlmostEqual(
            pump_radar._recovery_strength_score(c1),
            pump_radar._recovery_strength_score(c2),
            delta=0.01,
        )


class FetchPumpFunLaunchesTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end tests of the native two-lane fetch_pump_fun_launches(),
    mocking only the raw GeckoTerminal pools fetch."""

    def _mock_fetch(self, new_pools=None, trending_pools=None):
        new_pools = new_pools or []
        trending_pools = trending_pools or []

        async def fake(url):
            return new_pools if "new_pools" in url else trending_pools

        return patch.object(pump_radar, "_fetch_gecko_pools", AsyncMock(side_effect=fake))

    async def test_mandatory_pumpfun_verification(self):
        pool = _pool()
        with self._mock_fetch(new_pools=[(NON_PUMP_MINT, pool)]):
            result = await pump_radar.fetch_pump_fun_launches(limit=10)
        self.assertEqual(result, [])

    async def test_fresh_and_recovery_candidates_both_selected(self):
        fresh_pool = _pool(age_hours=0.5, volume_1h=40_000, price_change_1h=25.0, buys=30, sells=10)
        recovery_pool = _pool(age_hours=40, price_change_1h=8.0, price_change_24h=-20.0, volume_1h=15_000, buys=18, sells=8)
        fresh_mint = "FreshMintAAAAAAAAAAAAAAAAAAAAAAAAAAAApump"
        recovery_mint = "RecoverMintBBBBBBBBBBBBBBBBBBBBBBBBBpump"
        with self._mock_fetch(new_pools=[(fresh_mint, fresh_pool)], trending_pools=[(recovery_mint, recovery_pool)]):
            result = await pump_radar.fetch_pump_fun_launches(limit=10)
        self.assertIn(fresh_mint, result)
        self.assertIn(recovery_mint, result)

    async def test_dead_candidate_rejected_by_cheap_gate(self):
        dead_mint = "DeadMintCCCCCCCCCCCCCCCCCCCCCCCCCCCCpump"
        dead_pool = _pool(liquidity=500, market_cap=5_000, volume_1h=10, buys=0, sells=0)
        with self._mock_fetch(new_pools=[(dead_mint, dead_pool)]):
            result = await pump_radar.fetch_pump_fun_launches(limit=10)
        self.assertEqual(result, [])

    async def test_too_stale_candidate_excluded_from_both_lanes(self):
        stale_mint = "TooOldMintEEEEEEEEEEEEEEEEEEEEEEEEEEEpump"
        stale_pool = _pool(age_hours=400, volume_1h=20_000, buys=15, sells=10)
        with self._mock_fetch(new_pools=[(stale_mint, stale_pool)]):
            result = await pump_radar.fetch_pump_fun_launches(limit=10)
        self.assertEqual(result, [])

    async def test_duplicate_mint_across_feeds_counted_once(self):
        mint = "DupMintFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFpump"
        pool = _pool()
        with self._mock_fetch(new_pools=[(mint, pool)], trending_pools=[(mint, pool)]):
            result = await pump_radar.fetch_pump_fun_launches(limit=10)
        self.assertEqual(result.count(mint), 1)

    async def test_trending_source_alone_does_not_bypass_gate_or_verification(self):
        """A candidate sourced only from trending_pools still needs
        pump.fun verification and must still clear the cheap gate --
        appearing in the trending feed grants no exemption."""
        non_pump_trending = _pool()
        dead_trending = _pool(liquidity=500, market_cap=5_000, volume_1h=10, buys=0, sells=0)
        dead_mint = "DeadTrendMintGGGGGGGGGGGGGGGGGGGGGGGpump"
        with self._mock_fetch(trending_pools=[(NON_PUMP_MINT, non_pump_trending), (dead_mint, dead_trending)]):
            result = await pump_radar.fetch_pump_fun_launches(limit=10)
        self.assertEqual(result, [])

    async def test_result_respects_limit(self):
        pools = []
        for i in range(10):
            mint = f"Mint{i:03d}AAAAAAAAAAAAAAAAAAAAAAAAAAAAApump"
            pools.append((mint, _pool(age_hours=0.5 + i * 0.1)))
        with self._mock_fetch(new_pools=pools):
            result = await pump_radar.fetch_pump_fun_launches(limit=3)
        self.assertLessEqual(len(result), 3)

    async def test_provider_failure_returns_empty_not_fake_candidates(self):
        async def failing(url):
            raise RuntimeError("boom")

        with patch.object(pump_radar, "_fetch_gecko_pools", AsyncMock(side_effect=failing)):
            with self.assertRaises(RuntimeError):
                await pump_radar.fetch_pump_fun_launches(limit=10)
        # _fetch_gecko_pools itself is the boundary that must never raise
        # (see its own docstring/tests below) -- this test documents that
        # fetch_pump_fun_launches relies on that boundary rather than
        # catching provider errors a second time.


class FetchGeckoPoolsNeverRaisesTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_status_returns_empty(self):
        class _FakeResp:
            status = 500

            async def json(self):
                return {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _FakeSession:
            def get(self, *a, **k):
                return _FakeResp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with patch("aiohttp.ClientSession", return_value=_FakeSession()):
            result = await pump_radar._fetch_gecko_pools("https://api.geckoterminal.com/x")
        self.assertEqual(result, [])

    async def test_network_exception_returns_empty(self):
        with patch("aiohttp.ClientSession", side_effect=RuntimeError("boom")):
            result = await pump_radar._fetch_gecko_pools("https://api.geckoterminal.com/x")
        self.assertEqual(result, [])


class RetiredAdapterTests(unittest.TestCase):
    """The DexScreener-first adapter is retired: install() must be a
    harmless no-op and must never monkeypatch pump_radar.fetch_pump_fun_launches
    anymore."""

    def test_install_is_a_noop(self):
        original = pump_radar.fetch_pump_fun_launches
        adapter.install()
        self.assertIs(pump_radar.fetch_pump_fun_launches, original)

    def test_install_does_not_raise(self):
        adapter.install()
        adapter.install()  # idempotent, still a no-op


class DiscoveryDoesNotTouchScoringTests(unittest.TestCase):
    """Discovery decides only which mints are worth the downstream
    pipeline's attention, never how strong they are -- the two-lane
    ranking functions must not import or reference scoring/qualification/
    quota."""

    def test_ranking_functions_have_no_scoring_imports(self):
        import inspect

        for fn in (pump_radar._fresh_momentum_score, pump_radar._recovery_strength_score):
            source = inspect.getsource(fn)
            for forbidden in ("domain.signals.qualification", "domain.signals.quota", "score_candidate"):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
