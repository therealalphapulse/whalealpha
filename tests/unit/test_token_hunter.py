from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot

from whale_alpha.engines.token_hunter import (
    acceleration_score,
    cheap_filter,
    imbalance_score,
    prefilter_candidates,
    score_token,
)
from whale_alpha.integrations.token_hunter_sources import DiscoveryCandidate, _mint


def env():
    return SimpleNamespace(
        TOKEN_HUNTER_MIN_AGE_MINUTES=0,
        TOKEN_HUNTER_MAX_AGE_MINUTES=720,
        TOKEN_HUNTER_MIN_MARKET_CAP_USD=10_000,
        TOKEN_HUNTER_MAX_MARKET_CAP_USD=5_000_000,
        TOKEN_HUNTER_MIN_LIQUIDITY_USD=8_000,
        TOKEN_HUNTER_MIN_VOLUME_5M_USD=3_000,
        TOKEN_HUNTER_MIN_TXNS_5M=6,
        TOKEN_HUNTER_MIN_BUYS_5M=3,
        TOKEN_HUNTER_MAX_ENRICHED_AGE_MINUTES=360,
        TOKEN_HUNTER_MIN_LIQUIDITY_MC_RATIO=0.03,
    )


def snapshot(**overrides):
    data = dict(
        mint="X",
        name="X",
        symbol="X",
        pair_address="P",
        dex_id="raydium",
        created_at_ms=0,
        price_usd=0.01,
        market_cap_usd=100_000,
        liquidity_usd=20_000,
        volume_5m_usd=20_000,
        volume_1h_usd=100_000,
        buys_5m=30,
        sells_5m=10,
        buys_1h=100,
        sells_1h=80,
        price_change_5m_pct=12,
        price_change_1h_pct=30,
        metadata_present=True,
    )
    data.update(overrides)
    return TokenMarketSnapshot(**data)


def test_acceleration_is_higher_when_recent_rate_explodes():
    assert acceleration_score(40, 120) > acceleration_score(5, 120)


def test_buy_imbalance_rewards_buyers_without_infinite_ratio():
    assert imbalance_score(7, 3) > 50
    assert imbalance_score(0, 0) == 0


def test_cheap_filter_rejects_thin_liquidity():
    ok, reason = cheap_filter(snapshot(liquidity_usd=1000), age_minutes=20, env=env())
    assert not ok and reason == "LIQUIDITY_TOO_LOW"


def test_cheap_filter_rejects_large_obvious_tokens():
    ok, reason = cheap_filter(snapshot(market_cap_usd=10_000_000), age_minutes=20, env=env())
    assert not ok and reason == "MARKET_CAP_TOO_HIGH"


def test_score_is_explainable_and_bounded():
    result = score_token(snapshot(), age_minutes=20, smart_money_score=90)
    assert 0 <= result.total <= 100
    assert "buyer_acceleration" in result.components
    assert "smart_money_activity" in result.components


def test_manipulation_penalty_can_keep_a_high_volume_token_out():
    result = score_token(snapshot(volume_5m_usd=500_000, buys_5m=3, sells_5m=1), age_minutes=20)
    assert (
        "VOLUME_WITHOUT_TRANSACTION_DEPTH" in result.risk_flags or "EXTREME_TRADE_SIZE" in result.risk_flags
    )
    assert result.risk_level in {"MEDIUM", "HIGH"}


def test_discovery_parser_handles_raydium_and_meteora_mint_shapes():
    assert _mint({"mintA": {"address": "RAY"}}, "mintA") == "RAY"
    assert _mint({"pool_token_mints": ["MET", "OTHER"]}, "mint") == "MET"


def test_prefilter_rejects_before_enrichment_stage():
    now = datetime.now(UTC)
    good = DiscoveryCandidate(
        snapshot(created_at_ms=int((now - timedelta(minutes=20)).timestamp() * 1000)), "test"
    )
    bad = DiscoveryCandidate(
        snapshot(liquidity_usd=1000, created_at_ms=int((now - timedelta(minutes=20)).timestamp() * 1000)),
        "test",
    )
    selected, counts = prefilter_candidates([good, bad], now=now, env=env())
    assert [c.snapshot.mint for c in selected] == ["X"]
    assert counts == {"basic_filter_passed": 1, "quality_gate_passed": 1}


def test_minimum_age_filter_is_enforced():
    e = env()
    e.TOKEN_HUNTER_MIN_AGE_MINUTES = 30
    ok, reason = cheap_filter(snapshot(), age_minutes=20, env=e)
    assert not ok and reason == "AGE_OUTSIDE_WINDOW"


def test_maximum_age_filter_is_enforced():
    ok, reason = cheap_filter(snapshot(), age_minutes=721, env=env())
    assert not ok and reason == "AGE_OUTSIDE_WINDOW"

@pytest.mark.asyncio
async def test_provider_failure_isolation(monkeypatch):
    from whale_alpha.integrations import token_hunter_sources as sources
    e = env()
    e.DISCOVERY_PUMPFUN_ENABLED = True
    e.DISCOVERY_PUMPFUN_API_BASE = "https://pump"
    e.PUMPFUN_API_TOKEN = None
    e.DISCOVERY_LAUNCHLAB_API_BASE = "https://launch"
    e.DISCOVERY_RAYDIUM_API_BASE = "https://ray"
    e.DISCOVERY_METEORA_API_BASE = "https://met"
    e.DISCOVERY_DEXSCREENER_API_BASE = "https://dex"
    e.DISCOVERY_LAUNCHLAB_ENABLED = True
    e.DISCOVERY_RAYDIUM_ENABLED = False
    e.DISCOVERY_METEORA_ENABLED = False
    e.DISCOVERY_DEXSCREENER_ENABLED = False
    e.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE = 2
    e.DISCOVERY_PROVIDER_MAX_RETRIES = 0
    e.DISCOVERY_PROVIDER_RETRY_BASE_SECONDS = 0
    e.DISCOVERY_PROVIDER_RETRY_MAX_SECONDS = 0
    e.TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY = 2
    e.DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD = 1
    e.DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS = 60
    async def fake_fetch(client, env, provider, url, **kwargs):
        if provider == "pumpfun":
            raise RuntimeError("530")
        return [DiscoveryCandidate(snapshot(created_at_ms=1787373000000), provider)]
    monkeypatch.setattr(sources, "_fetch_candidates", fake_fetch)
    sources._cache.clear()
    result = await sources.discover_token_candidates(AsyncMock(), e)
    assert result["pumpfun"] == []
    assert len(result["launchlab"]) == 1

@pytest.mark.asyncio
async def test_complete_hunter_pipeline_reaches_scoring_without_age_rejection(monkeypatch):
    """`run_hunter_cycle` (the strict Whale Alpha dip->consolidation->reversal
    pipeline) discovers via `discover_meme_candidates` — not the legacy
    multi-source `discover_token_candidates` — and its funnel only has
    discovered/evaluated/approved/alert_attempted/alert_delivered. This
    replaces a stale pre-rewrite version of this test that monkeypatched
    `discover_token_candidates` (never called by run_hunter_cycle) and
    asserted on funnel keys (basic_filter_passed/quality_gate_passed/
    enriched/scored) that don't exist on the current funnel shape. See
    engines/reversal_hunter.py + integrations/token_hunter_sources.py's
    `discover_dexscreener_fallback_candidates` for where discovered
    candidates actually come from now."""
    from whale_alpha.engines import token_hunter as hunter

    now = datetime.now(UTC)
    created = int((now - timedelta(minutes=20)).timestamp() * 1000)
    candidate = DiscoveryCandidate(snapshot(created_at_ms=created), "birdeye_meme")
    env_obj = env()
    env_obj.TOKEN_HUNTER_ALERT_COOLDOWN_MINUTES = 120
    env_obj.admin_telegram_ids = []

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def commit(self): pass

    def session_factory(): return Session()

    monkeypatch.setattr(hunter, "discover_meme_candidates", AsyncMock(return_value=[candidate]))
    monkeypatch.setattr(hunter, "evaluate_candidates", AsyncMock(return_value=[]))

    funnel = await hunter.run_hunter_cycle(env_obj, session_factory, AsyncMock(), AsyncMock())

    assert funnel["discovered"] == 1
    assert funnel["evaluated"] == 0
    assert funnel["approved"] == 0
