from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from whale_alpha.integrations.token_hunter_sources import _candidate


def test_pumpfun_prefers_usd_market_cap_over_raw_bonding_curve_units():
    """pump.fun's `market_cap` field is a raw SOL/bonding-curve number, not USD.

    Regression for: this raw value was previously preferred over the genuine
    `usd_market_cap` field, undercounting real market cap by ~2 orders of
    magnitude and causing legitimately-qualifying tokens to be rejected as
    MARKET_CAP_TOO_LOW.
    """
    entry = {
        "mint": "D8zGM7Wbag1Tf9wUzXASsXn8XNjHWdRcd7o9TGLHpump",
        "market_cap": 27.969344383289943,  # raw bonding-curve units — must NOT be used
        "usd_market_cap": 2620.8703169299006,  # genuine USD value — must be used
    }
    candidate = _candidate(entry, "pumpfun", entry["mint"])
    assert candidate.snapshot.market_cap_usd == 2620.8703169299006


def test_dexscreener_shaped_marketcap_field_still_preferred():
    """DexScreener-shaped payloads (camelCase `marketCap`) must still win when present."""
    entry = {
        "mint": "SomeMint111111111111111111111111111111111",
        "marketCap": 45000.0,
        "market_cap": 12.0,  # would be wrong if used, confirms marketCap takes priority
    }
    candidate = _candidate(entry, "dexscreener", entry["mint"])
    assert candidate.snapshot.market_cap_usd == 45000.0


def test_fdv_used_before_ambiguous_raw_market_cap_fallback():
    """When no USD-denominated field is present, fdv is a better proxy than the
    ambiguous `market_cap` field, which may not be USD-denominated at all."""
    entry = {
        "mint": "SomeMint222222222222222222222222222222222",
        "fdv": 99000.0,
        "market_cap": 5.0,
    }
    candidate = _candidate(entry, "raydium", entry["mint"])
    assert candidate.snapshot.market_cap_usd == 99000.0


def test_market_cap_none_when_no_recognized_field_present():
    """Providers whose payload shape has none of the known market-cap fields
    (e.g. Meteora and Raydium pools, which only expose tvl/price/reserves, not
    market cap or supply) must resolve to None rather than a wrong guess — this
    is the UNKNOWN case, distinct from a genuinely-zero or below-threshold
    market cap."""
    entry = {"mint": "SomeMint333333333333333333333333333333333"}
    candidate = _candidate(entry, "meteora", entry["mint"])
    assert candidate.snapshot.market_cap_usd is None


@pytest.mark.asyncio
async def test_dexscreener_discovery_resolves_real_pair_data_via_two_step_lookup(monkeypatch):
    """DexScreener's boost list is promotional metadata only (no market data).

    Regression for: discovery previously built candidates directly from that
    metadata, so market_cap_usd (and liquidity/volume/age) was always None,
    causing every DexScreener-sourced candidate to be rejected as
    MARKET_CAP_TOO_LOW regardless of the token's real market cap. Discovery
    must now seed addresses from the boost list, filter to Solana, and resolve
    real pair data via the tokens endpoint.
    """
    from whale_alpha.integrations import token_hunter_sources as sources

    calls: list[str] = []

    class FakeProvider:
        async def get(self, client, url, **kwargs):
            calls.append(url)
            if "token-boosts" in url:
                payload = [
                    {"chainId": "solana", "tokenAddress": "MintA1111111111111111111111111111111111111"},
                    {"chainId": "ethereum", "tokenAddress": "0xNotSolana"},
                ]
            else:
                assert "MintA1111111111111111111111111111111111111" in url
                assert "0xNotSolana" not in url
                payload = [
                    {
                        "baseToken": {"address": "MintA1111111111111111111111111111111111111"},
                        "pairAddress": "PAIR1",
                        "marketCap": 45000.0,
                        "liquidity": {"usd": 12000.0},
                        "pairCreatedAt": 1787373000000,
                    }
                ]
            return SimpleNamespace(response=SimpleNamespace(status_code=200, json=lambda: payload))

    monkeypatch.setattr(sources, "_provider", lambda env, name: FakeProvider())
    fake_env = SimpleNamespace(
        DISCOVERY_DEXSCREENER_API_BASE="https://dex",
        DISCOVERY_PROVIDER_MAX_RETRIES=0,
        DISCOVERY_PROVIDER_RETRY_BASE_SECONDS=0,
        DISCOVERY_PROVIDER_RETRY_MAX_SECONDS=0,
    )
    candidates = await sources._discover_dexscreener_candidates(AsyncMock(), fake_env, limit=10)
    assert len(candidates) == 1
    assert candidates[0].snapshot.mint == "MintA1111111111111111111111111111111111111"
    assert candidates[0].snapshot.market_cap_usd == 45000.0
    assert candidates[0].snapshot.liquidity_usd == 12000.0
    assert any("token-boosts" in url for url in calls)
    assert any("tokens/v1/solana/" in url for url in calls)
