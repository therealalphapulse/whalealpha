"""Regression tests for the surgical DexScreener Pump.fun/PumpSwap discovery adapter."""

import time
import unittest
from unittest.mock import AsyncMock, patch

import domain.signals._radar_discovery_adapter as adapter
import domain.signals.pump_radar as pump_radar

PUMP_MINT = "FreshMintAAAAAAAAAAAAAAAAAAAAAAAAAAAApump"
OTHER_MINT = "OtherMintBBBBBBBBBBBBBBBBBBBBBBBBBBBBpump"


def _card(*, dex="pumpfun", liquidity=20_000, market_cap=100_000, age_hours=1.0):
    return {
        "dex": dex,
        "liquidity": liquidity,
        "market_cap": market_cap,
        "pair_created": (time.time() - age_hours * 3600) * 1000,
    }


class CandidateFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_exact_pumpfun_filter(self):
        with patch.object(adapter, "get_token_card_info", AsyncMock(return_value=_card())):
            self.assertIsNotNone(await adapter._validate_candidate(PUMP_MINT))

    async def test_accepts_pumpswap_filter(self):
        with patch.object(adapter, "get_token_card_info", AsyncMock(return_value=_card(dex="pumpswap"))):
            self.assertIsNotNone(await adapter._validate_candidate(PUMP_MINT))

    async def test_rejects_non_pumpfun_or_pumpswap_dex(self):
        with patch.object(adapter, "get_token_card_info", AsyncMock(return_value=_card(dex="raydium"))):
            self.assertIsNone(await adapter._validate_candidate(PUMP_MINT))

    async def test_liquidity_is_strictly_above_15k(self):
        for liquidity in (15_000, 14_999):
            with patch.object(adapter, "get_token_card_info", AsyncMock(return_value=_card(liquidity=liquidity))):
                self.assertIsNone(await adapter._validate_candidate(PUMP_MINT))
        with patch.object(adapter, "get_token_card_info", AsyncMock(return_value=_card(liquidity=15_001))):
            self.assertIsNotNone(await adapter._validate_candidate(PUMP_MINT))

    async def test_market_cap_is_50k_to_1m(self):
        for market_cap in (49_999, 1_000_001):
            with patch.object(adapter, "get_token_card_info", AsyncMock(return_value=_card(market_cap=market_cap))):
                self.assertIsNone(await adapter._validate_candidate(PUMP_MINT))
        for market_cap in (50_000, 1_000_000):
            with patch.object(adapter, "get_token_card_info", AsyncMock(return_value=_card(market_cap=market_cap))):
                self.assertIsNotNone(await adapter._validate_candidate(PUMP_MINT))

    async def test_pair_age_is_strictly_less_than_6h(self):
        for age_hours in (6.0, 6.01, 12.0):
            with patch.object(adapter, "get_token_card_info", AsyncMock(return_value=_card(age_hours=age_hours))):
                self.assertIsNone(await adapter._validate_candidate(PUMP_MINT))
        with patch.object(adapter, "get_token_card_info", AsyncMock(return_value=_card(age_hours=5.99))):
            self.assertIsNotNone(await adapter._validate_candidate(PUMP_MINT))

    async def test_missing_data_is_rejected(self):
        with patch.object(adapter, "get_token_card_info", AsyncMock(return_value=None)):
            self.assertIsNone(await adapter._validate_candidate(PUMP_MINT))


class DiscoveryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_solana_filter_and_duplicate_prevention(self):
        feeds = [[
            {"chainId": "solana", "tokenAddress": PUMP_MINT},
            {"chainId": "ethereum", "tokenAddress": "eth-token"},
        ], [
            {"chainId": "solana", "tokenAddress": PUMP_MINT},
            {"chainId": "solana", "tokenAddress": OTHER_MINT},
        ]]
        self.assertEqual(adapter._seed_candidates(feeds), [PUMP_MINT, OTHER_MINT])

    async def test_one_candidate_failure_does_not_stop_next_candidate(self):
        original = pump_radar.fetch_pump_fun_launches
        profiles = [
            {"chainId": "solana", "tokenAddress": PUMP_MINT},
            {"chainId": "solana", "tokenAddress": OTHER_MINT},
        ]

        async def fake_card(mint):
            if mint == PUMP_MINT:
                raise RuntimeError("provider failure")
            return _card()

        try:
            with patch.object(adapter, "get_latest_token_profiles", AsyncMock(return_value=profiles)), \
                 patch.object(adapter, "get_latest_boosted_tokens", AsyncMock(return_value=[])), \
                 patch.object(adapter, "get_token_card_info", AsyncMock(side_effect=fake_card)):
                adapter.install()
                result = await pump_radar.fetch_pump_fun_launches(limit=10)
            self.assertEqual(result, [OTHER_MINT])
        finally:
            pump_radar.fetch_pump_fun_launches = original

    async def test_dexscreener_feed_failure_returns_no_fake_candidates(self):
        original = pump_radar.fetch_pump_fun_launches
        try:
            with patch.object(adapter, "get_latest_token_profiles", AsyncMock(side_effect=RuntimeError("boom"))), \
                 patch.object(adapter, "get_latest_boosted_tokens", AsyncMock(side_effect=RuntimeError("boom"))):
                adapter.install()
                result = await pump_radar.fetch_pump_fun_launches(limit=10)
            self.assertEqual(result, [])
        finally:
            pump_radar.fetch_pump_fun_launches = original


class ScopeTests(unittest.TestCase):
    def test_adapter_does_not_contain_downstream_decision_logic(self):
        import inspect
        source = inspect.getsource(adapter)
        for forbidden in ("score_candidate", "qualify_candidate", "execute_paper_buy", "execute_real_buy"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
