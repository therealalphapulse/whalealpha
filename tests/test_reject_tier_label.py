"""
Regression test for the "REJECT" tier-label bug: a delivered alert card
(Solana classic Signal Alert, or Engine A/Wallet-Consensus /
Engine B/Robinhood-Chain) must never literally display the word
"REJECT" in its tier badge, since a card only ever gets built for a
candidate that already cleared its own engine's gate -- see
domain.signals.pump_radar._build_pump_card_text().
"""

import unittest

from domain.signals.pump_radar import _build_pump_card_text

_BASE_CANDIDATE_DATA = {
    "name": "Galaxy Exchange",
    "symbol": "GALX",
    "contract": "0xdeadbeef",
    "price": "0.00123",
    "market_cap": 90000,
    "fdv": 90000,
    "liquidity": 14700,
    "volume_1h": 224322,
    "volume_24h": 900000,
    "price_change_5m": "1",
    "price_change_1h": "-2",
    "price_change_6h": "3",
    "price_change_24h": "-4",
    "txns_1h_buys": 40,
    "txns_1h_sells": 25,
    "txns_24h_buys": 400,
    "txns_24h_sells": 250,
    "pair_created": 0,
    "dex": "uniswap",
    "pair_url": "https://dexscreener.com/robinhoodchain/0xdeadbeef",
    "pool_address": "0xpool",
    "image_url": "",
    "website_url": "",
    "twitter_url": "",
    "telegram_url": "",
}


def _candidate(tier: str) -> dict:
    return {
        "contract": "0xdeadbeef",
        "data": dict(_BASE_CANDIDATE_DATA),
        "pump": {
            "score": 60,
            "tier": tier,
            "reasons": ["Some reason"],
            "breakdown": {},
        },
        "security_data": {},
        "holder_analysis": {},
        "smart_money": [],
        "whale_holders": [],
        "confidence": {"confidence_score": 50, "confirmed_count": 0, "checked_count": 1},
    }


class RejectTierLabelTests(unittest.TestCase):
    def test_reject_tier_never_shown_verbatim_on_a_delivered_card(self):
        text = _build_pump_card_text(_candidate("❌ REJECT"))
        self.assertNotIn("REJECT", text)
        self.assertIn("SUB-FLOOR", text)

    def test_other_tiers_pass_through_unchanged(self):
        for tier in ["🌟 LEGENDARY", "💎 ELITE", "🟡 WATCHLIST", "⚪ SUB-FLOOR (tracked only)"]:
            text = _build_pump_card_text(_candidate(tier))
            self.assertIn(tier, text)


if __name__ == "__main__":
    unittest.main()
