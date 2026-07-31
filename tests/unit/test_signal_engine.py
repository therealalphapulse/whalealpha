"""Port of tests/unit/signalEngine.test.ts — same cases, same assertions."""

from __future__ import annotations

from datetime import datetime, timedelta

from whale_alpha.engines.signal import (
    SignalEngineConfig,
    TokenSafetyContext,
    WhaleAccumulationEvent,
    evaluate_token_cluster,
)

config = SignalEngineConfig(min_wallets=3, window_minutes=30, min_confidence=50)


def make_event(wallet_id: str, wallet_score: float, seconds_ago: float = 0) -> WhaleAccumulationEvent:
    return WhaleAccumulationEvent(
        wallet_id=wallet_id,
        wallet_score=wallet_score,
        token_mint="TOKEN_MINT_ABC",
        amount_usd=2000,
        observed_at=datetime.now() - timedelta(seconds=seconds_ago),
    )


def test_returns_none_when_fewer_than_min_wallets_contributed():
    events = [make_event("w1", 80), make_event("w2", 80)]
    result = evaluate_token_cluster("TOKEN_MINT_ABC", events, None, config)
    assert result is None


def test_produces_a_signal_when_enough_high_quality_wallets_cluster_together():
    events = [make_event("w1", 85), make_event("w2", 80), make_event("w3", 90)]
    result = evaluate_token_cluster("TOKEN_MINT_ABC", events, None, config)
    assert result is not None
    assert result.wallet_count == 3
    assert result.confidence_score >= config.min_confidence


def test_ignores_events_outside_the_time_window():
    events = [
        make_event("w1", 85, 0),
        make_event("w2", 85, 10),
        make_event("w3", 85, 60 * 60),  # 1h ago, outside 30-min window
    ]
    result = evaluate_token_cluster("TOKEN_MINT_ABC", events, None, config)
    assert result is None


def test_flags_high_risk_when_safety_context_is_poor():
    events = [make_event("w1", 85), make_event("w2", 80), make_event("w3", 90)]
    bad_safety = TokenSafetyContext(
        market_cap_usd=500000,
        liquidity_usd=3000,
        holder_concentration_top10_pct=0.8,
        lp_locked=False,
        mint_authority_revoked=False,
        freeze_authority_revoked=False,
    )
    config_zero_conf = SignalEngineConfig(
        min_wallets=config.min_wallets, window_minutes=config.window_minutes, min_confidence=0
    )
    result = evaluate_token_cluster("TOKEN_MINT_ABC", events, bad_safety, config_zero_conf)
    assert result is not None
    assert result.risk_level == "HIGH"
