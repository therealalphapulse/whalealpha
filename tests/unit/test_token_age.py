from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from whale_alpha.integrations import token_age
from whale_alpha.integrations.token_age import calculate_age_seconds, parse_timestamp_ms, resolve_token_ages
from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot
from whale_alpha.integrations.token_hunter_sources import DiscoveryCandidate


def candidate(created_at_ms=None, mint="M"):
    return DiscoveryCandidate(
        TokenMarketSnapshot(
            mint=mint, name="N", symbol="S", pair_address="P", dex_id="test", created_at_ms=created_at_ms,
            price_usd=1, market_cap_usd=100_000, liquidity_usd=20_000, volume_5m_usd=10_000,
            volume_1h_usd=50_000, buys_5m=20, sells_5m=10, buys_1h=80, sells_1h=40,
            price_change_5m_pct=1, price_change_1h_pct=2, metadata_present=True,
        ), "test"
    )


def test_provider_timestamp_to_age():
    now = datetime(2026, 8, 22, 5, 0, tzinfo=UTC)
    created = int((now - timedelta(minutes=20)).timestamp() * 1000)
    assert calculate_age_seconds(created, now) == pytest.approx(1200)


@pytest.mark.asyncio
async def test_dexscreener_pair_created_at_fallback(monkeypatch):
    now = datetime.now(UTC)
    c = candidate(None)
    dex = candidate(int((now - timedelta(minutes=15)).timestamp() * 1000)).snapshot
    monkeypatch.setattr(token_age, "enrich_tokens", AsyncMock(return_value={"M": dex}))
    result = await resolve_token_ages(AsyncMock(), SimpleNamespace(TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY=2), [c], now)
    assert result["M"].source == "dexscreener"
    assert result["M"].age_seconds == pytest.approx(900, abs=2)


@pytest.mark.asyncio
async def test_onchain_fallback_if_supported(monkeypatch):
    now = datetime.now(UTC)
    c = candidate(None)
    monkeypatch.setattr(token_age, "enrich_tokens", AsyncMock(return_value={}))
    monkeypatch.setattr(token_age, "_onchain_first_seen", AsyncMock(return_value=int((now - timedelta(minutes=8)).timestamp() * 1000)))
    result = await resolve_token_ages(AsyncMock(), SimpleNamespace(TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY=2), [c], now, object())
    assert result["M"].source == "onchain"


def test_missing_timestamp_is_unknown():
    assert parse_timestamp_ms(None) is None


def test_malformed_timestamp_is_unknown():
    assert parse_timestamp_ms("not-a-time") is None


def test_future_timestamp_is_invalid_for_age():
    now = datetime.now(UTC)
    future = int((now + timedelta(minutes=1)).timestamp() * 1000)
    assert calculate_age_seconds(future, now) is None


def test_timezone_handling_is_normalized_to_utc():
    utc_ms = parse_timestamp_ms("2026-08-22T05:00:00Z")
    offset_ms = parse_timestamp_ms("2026-08-22T06:00:00+01:00")
    assert utc_ms == offset_ms
