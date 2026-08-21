from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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
