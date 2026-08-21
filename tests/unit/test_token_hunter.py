from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot

from whale_alpha.engines import token_hunter
from whale_alpha.engines.token_hunter import (
    acceleration_score,
    cheap_filter,
    imbalance_score,
    prefilter_candidates,
    score_token,
)
from whale_alpha.integrations.token_hunter_sources import DiscoveryCandidate, _candidate, _created_at_ms, _mint


def env():
    return SimpleNamespace(
        TOKEN_HUNTER_MIN_AGE_MINUTES=0,
        TOKEN_HUNTER_MAX_UNIQUE_PER_CYCLE=120,
        TOKEN_HUNTER_MAX_AGE_MINUTES=720,
        TOKEN_HUNTER_MIN_MARKET_CAP_USD=10_000,
        TOKEN_HUNTER_MAX_MARKET_CAP_USD=5_000_000,
        TOKEN_HUNTER_MIN_LIQUIDITY_USD=8_000,
        TOKEN_HUNTER_MIN_VOLUME_5M_USD=3_000,
        TOKEN_HUNTER_MIN_TXNS_5M=6,
        TOKEN_HUNTER_MIN_BUYS_5M=3,
        TOKEN_HUNTER_MAX_ENRICHED_AGE_MINUTES=360,
        TOKEN_HUNTER_ONCHAIN_AGE_MAX_CANDIDATES=25,
        TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY=3,
        DISCOVERY_DEXSCREENER_ENABLED=True,
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



def test_provider_timestamp_flows_into_token_age():
    now = datetime(2026, 8, 21, 20, 0, tzinfo=UTC)
    created_ms = int((now - timedelta(minutes=25)).timestamp() * 1000)
    candidate = _candidate(
        {
            "mint": "PROVIDER",
            "created_timestamp": created_ms // 1000,
            "marketCap": 100_000,
            "liquidity": {"usd": 20_000},
        },
        "pumpfun",
        "PROVIDER",
    )
    assert candidate.snapshot.created_at_ms == created_ms
    assert token_hunter._age(candidate.snapshot.created_at_ms, now) == pytest.approx(25.0)


def test_provider_timestamp_parser_supports_milliseconds_and_seconds():
    assert _created_at_ms({"created_timestamp": 1_700_000_000}) == 1_700_000_000_000
    assert _created_at_ms({"created_at": 1_700_000_000_000}) == 1_700_000_000_000


def test_token_age_normalizes_seconds_and_milliseconds():
    now = datetime(2026, 8, 21, 20, 0, tzinfo=UTC)
    expected = int((now - timedelta(minutes=10)).timestamp() * 1000)
    assert token_hunter._normalize_created_at_ms(expected // 1000, now) == expected
    assert token_hunter._normalize_created_at_ms(expected, now) == expected


def test_missing_malformed_and_future_timestamps_are_unknown():
    now = datetime(2026, 8, 21, 20, 0, tzinfo=UTC)
    assert token_hunter._age(None, now) is None
    assert token_hunter._age("not-a-time", now) is None
    future = int((now + timedelta(minutes=5)).timestamp() * 1000)
    assert token_hunter._age(future, now) is None


def test_timezone_handling_is_utc():
    now = datetime(2026, 8, 21, 20, 0, tzinfo=UTC)
    created = int(datetime(2026, 8, 21, 19, 30, tzinfo=UTC).timestamp() * 1000)
    assert token_hunter._age(created, now) == pytest.approx(30.0)


def test_minimum_and_maximum_age_filters():
    cfg = env()
    cfg.TOKEN_HUNTER_MIN_AGE_MINUTES = 15
    ok, reason = cheap_filter(snapshot(), age_minutes=10, env=cfg)
    assert not ok and reason == "AGE_OUTSIDE_WINDOW"
    ok, reason = cheap_filter(snapshot(), age_minutes=721, env=env())
    assert not ok and reason == "AGE_OUTSIDE_WINDOW"


@pytest.mark.asyncio
async def test_dexscreener_pair_created_at_fallback(monkeypatch):
    now = datetime.now(UTC)
    candidate = DiscoveryCandidate(snapshot(created_at_ms=None), "raydium")
    dex = snapshot(created_at_ms=int((now - timedelta(minutes=12)).timestamp() * 1000))
    async def fake_enrich(*args, **kwargs):
        return {"X": dex}
    monkeypatch.setattr(token_hunter, "enrich_tokens", fake_enrich)
    result = await token_hunter._resolve_candidate_ages([candidate], client=object(), connection=None, env=env(), now=now)
    assert result[0].snapshot.created_at_ms == dex.created_at_ms


@pytest.mark.asyncio
async def test_onchain_fallback(monkeypatch):
    now = datetime.now(UTC)
    candidate = DiscoveryCandidate(snapshot(created_at_ms=None), "raydium")
    monkeypatch.setattr(token_hunter, "enrich_tokens", lambda *a, **k: _empty_async())
    async def fake_rpc(*args, **kwargs):
        return int((now - timedelta(minutes=8)).timestamp() * 1000)
    monkeypatch.setattr(token_hunter, "get_token_first_seen_at_ms", fake_rpc)
    result = await token_hunter._resolve_candidate_ages([candidate], client=object(), connection=object(), env=env(), now=now)
    assert result[0].snapshot.created_at_ms is not None


async def _empty_async():
    return {}


@pytest.mark.asyncio
async def test_missing_timestamp_remains_unknown_and_does_not_get_fabricated(monkeypatch):
    candidate = DiscoveryCandidate(snapshot(created_at_ms=None), "raydium")
    monkeypatch.setattr(token_hunter, "enrich_tokens", lambda *a, **k: _empty_async())
    result = await token_hunter._resolve_candidate_ages([candidate], client=object(), connection=None, env=env(), now=datetime.now(UTC))
    assert result[0].snapshot.created_at_ms is None


@pytest.mark.asyncio
async def test_provider_failure_isolation(monkeypatch):
    import whale_alpha.integrations.token_hunter_sources as sources
    async def fake_fetch(client, env, provider, url, **kwargs):
        if provider == "pumpfun":
            raise RuntimeError("boom")
        return [DiscoveryCandidate(snapshot(created_at_ms=1), provider)]
    monkeypatch.setattr(sources, "_fetch_candidates", fake_fetch)
    monkeypatch.setattr(sources._cache, "get", lambda key: None)
    monkeypatch.setattr(sources._cache, "set", lambda *args: None)
    cfg = SimpleNamespace(**{**env().__dict__, "DISCOVERY_PUMPFUN_ENABLED": True, "DISCOVERY_LAUNCHLAB_ENABLED": True, "DISCOVERY_RAYDIUM_ENABLED": False, "DISCOVERY_METEORA_ENABLED": False, "DISCOVERY_DEXSCREENER_ENABLED": False, "TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY": 3, "DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD": 3, "DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS": 30, "DISCOVERY_PROVIDER_MAX_RETRIES": 1, "DISCOVERY_PROVIDER_RETRY_BASE_SECONDS": 0.01, "DISCOVERY_PROVIDER_RETRY_MAX_SECONDS": 0.02, "TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE": 2, "DISCOVERY_PUMPFUN_API_BASE":"https://frontend-api-v3.pump.fun", "DISCOVERY_LAUNCHLAB_API_BASE":"https://launch-mint-v1.raydium.io"})
    result = await sources.discover_token_candidates(object(), cfg)
    assert "launchlab" in result and result["launchlab"]


def test_token_normalization_supports_nested_mints():
    from whale_alpha.integrations.token_hunter_sources import _mint
    assert _mint({"token_x": {"address": "NESTED"}}, "missing") == "NESTED"


@pytest.mark.asyncio
async def test_onchain_resolved_age_survives_enrichment_without_timestamp(monkeypatch):
    now = datetime.now(UTC)
    resolved_ms = int((now - timedelta(minutes=8)).timestamp() * 1000)
    candidate = DiscoveryCandidate(snapshot(created_at_ms=resolved_ms), "raydium")
    async def fake_discover(*args):
        return {"raydium": [candidate]}
    async def fake_enrich(*args):
        return {"X": snapshot(created_at_ms=None)}
    async def fake_smart(*args):
        return None
    async def fake_outcomes(*args):
        return None
    async def fake_persist(*args):
        return SimpleNamespace(last_alerted_at=None, alert_attempted_at=None, alert_status=None, detected_at=now, alert_delivered_at=None, alert_error=None)
    monkeypatch.setattr(token_hunter, "discover_token_candidates", fake_discover)
    monkeypatch.setattr(token_hunter, "enrich_tokens", fake_enrich)
    monkeypatch.setattr(token_hunter, "_smart_money", fake_smart)
    monkeypatch.setattr(token_hunter, "_outcomes", fake_outcomes)
    monkeypatch.setattr(token_hunter, "_persist", fake_persist)
    monkeypatch.setattr(token_hunter, "score_token", lambda *a, **k: token_hunter.TokenScore(70, {"age": 70}, "MEDIUM", (), ("Age",)))
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def commit(self): pass
    class Factory:
        def __call__(self): return Session()
    cfg = env()
    cfg.admin_telegram_ids = set()
    cfg.TOKEN_HUNTER_ALERT_MIN_SCORE = 82
    result = await token_hunter.run_hunter_cycle(cfg, Factory(), SimpleNamespace(), object(), object())
    assert result["scored"] == 1


@pytest.mark.asyncio
async def test_complete_hunter_pipeline_reaches_telegram(monkeypatch):
    candidate = DiscoveryCandidate(snapshot(created_at_ms=int((datetime.now(UTC) - timedelta(minutes=10)).timestamp()*1000)), "test")
    async def fake_discover(*args): return {"test": [candidate]}
    async def fake_enrich(*args): return {"X": candidate.snapshot}
    async def fake_smart(*args): return None
    async def fake_outcomes(*args): return None
    async def fake_persist(*args): return SimpleNamespace(last_alerted_at=None, alert_attempted_at=None, alert_status=None, detected_at=datetime.now(UTC), alert_delivered_at=None, alert_error=None)
    monkeypatch.setattr(token_hunter, "discover_token_candidates", fake_discover)
    monkeypatch.setattr(token_hunter, "enrich_tokens", fake_enrich)
    monkeypatch.setattr(token_hunter, "_smart_money", fake_smart)
    monkeypatch.setattr(token_hunter, "_outcomes", fake_outcomes)
    monkeypatch.setattr(token_hunter, "_persist", fake_persist)
    monkeypatch.setattr(token_hunter, "score_token", lambda *a, **k: token_hunter.TokenScore(90, {"age":90}, "LOW", (), ("Age",)))
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def commit(self): pass
    class Factory:
        def __call__(self): return Session()
    class Bot:
        def __init__(self):
            self.sent = []
        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
    bot = Bot()
    cfg = env()
    cfg.admin_telegram_ids = {"123"}
    cfg.TOKEN_HUNTER_ALERT_MIN_SCORE = 82
    result = await token_hunter.run_hunter_cycle(cfg, Factory(), bot, object(), None)
    assert result["discovered"] == 1 and result["basic_filter_passed"] == 1 and result["enriched"] == 1 and result["scored"] == 1
    assert result["alert_delivered"] == 1 and bot.sent
