from __future__ import annotations

from whale_alpha.engines.reversal_hunter import Candle, detect_dip_consolidation_breakout


def _fixture(two_candle_breakout: bool = True) -> list[Candle]:
    now = 1_787_770_000
    candles: list[Candle] = []
    for i in range(864):
        ts = now - (863 - i) * 300
        if i < 500:
            price = 1.8
        elif i <= 600:
            price = 2.0 - (i - 500) * 0.008
        elif i < 700:
            price = 1.2 + (i - 600) * 0.0002
        elif i < 762:
            price = 1.22 + (0.002 if i % 2 else -0.002)
        else:
            price = 1.22
        candles.append(Candle(ts, price, price * 1.002, price * 0.998, price, 1000))
    candles[-2] = Candle(now - 300, 1.22, 1.31, 1.21, 1.30, 2200)
    candles[-1] = Candle(now, 1.30, 1.36, 1.29, 1.35 if two_candle_breakout else 1.22, 2500)
    return candles


def test_confirms_dip_consolidation_and_breakout():
    result = detect_dip_consolidation_breakout(_fixture(), 1_787_770_000)
    assert result is not None
    assert 15 <= result.dip_pct <= 50
    assert result.consolidation_minutes >= 45
    assert result.consolidation_range_pct <= 12
    assert result.breakout_confirmed
    assert result.breakout_volume_5m_mult >= 1.8
    assert result.breakout_volume_15m_mult >= 1.8


def test_rejects_single_candle_spike_without_follow_through():
    candles = _fixture(two_candle_breakout=False)
    result = detect_dip_consolidation_breakout(candles, 1_787_770_000)
    assert result is None


# --- Discovery pipeline: Birdeye failure / silent-empty-result fix, and the
# DexScreener fallback wiring (engines/reversal_hunter.discover_meme_candidates).
# No scenario below loosens a filter or asserts on fabricated data — every
# candidate returned is either a parsed Birdeye row or a candidate handed
# back by a mocked (already-approved) DexScreener fallback; provider
# failures degrade to an empty list rather than raising or fabricating.

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from whale_alpha.engines import reversal_hunter
from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot
from whale_alpha.integrations.token_hunter_sources import DiscoveryCandidate
from whale_alpha.utils.http_retry import HttpFetchResult


def _env(**overrides):
    data = dict(
        BIRDEYE_API_KEY="test-key",
        DISCOVERY_BIRDEYE_ENABLED=True,
        DISCOVERY_BIRDEYE_API_BASE="https://public-api.birdeye.so",
        WHALE_ALPHA_MIN_LIQ_USD=10_000,
        WHALE_ALPHA_MAX_LIQ_USD=5_000_000,
        WHALE_ALPHA_MIN_MC_USD=50_000,
        WHALE_ALPHA_MAX_MC_USD=10_000_000,
        TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE=50,
        TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY=5,
        DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD=1,
        DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS=60,
        DISCOVERY_PROVIDER_MAX_RETRIES=2,
        DISCOVERY_PROVIDER_RETRY_BASE_SECONDS=0.1,
        DISCOVERY_PROVIDER_RETRY_MAX_SECONDS=1.0,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _fallback_candidate(mint: str = "DEX_MINT") -> DiscoveryCandidate:
    return DiscoveryCandidate(
        TokenMarketSnapshot(
            mint=mint, name="Fallback Token", symbol="FBK", pair_address="P", dex_id="raydium",
            created_at_ms=0, price_usd=0.01, market_cap_usd=100_000, liquidity_usd=20_000,
            volume_5m_usd=1_000, volume_1h_usd=5_000, buys_5m=10, sells_5m=2, buys_1h=40, sells_1h=20,
            price_change_5m_pct=5, price_change_1h_pct=10, metadata_present=True,
        ),
        source="dexscreener",
    )


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeProvider:
    def __init__(self, result: HttpFetchResult):
        self._result = result

    async def get(self, *args, **kwargs):
        return self._result


def _patch_provider(monkeypatch, result: HttpFetchResult):
    monkeypatch.setattr(reversal_hunter, "get_provider_client", lambda *a, **k: _FakeProvider(result))


@pytest.mark.asyncio
async def test_birdeye_success_returns_birdeye_candidates_and_skips_fallback(monkeypatch):
    payload = {"data": {"items": [{"address": "MINT1", "name": "Test", "symbol": "TST", "liquidity": 50_000, "market_cap": 200_000}]}}
    _patch_provider(monkeypatch, HttpFetchResult(response=_FakeResponse(200, payload), transient=False))
    fallback = AsyncMock()
    monkeypatch.setattr(reversal_hunter, "discover_dexscreener_fallback_candidates", fallback)

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _env(), datetime.now(UTC))

    assert len(out) == 1
    assert out[0].snapshot.mint == "MINT1"
    assert out[0].source == "birdeye_meme"
    fallback.assert_not_called()


@pytest.mark.asyncio
async def test_missing_birdeye_key_falls_back_to_dexscreener(monkeypatch):
    fallback = AsyncMock(return_value=[_fallback_candidate()])
    monkeypatch.setattr(reversal_hunter, "discover_dexscreener_fallback_candidates", fallback)

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _env(BIRDEYE_API_KEY=None), datetime.now(UTC))

    assert len(out) == 1
    assert out[0].source == "dexscreener"
    fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_birdeye_circuit_open_falls_back_to_dexscreener(monkeypatch):
    _patch_provider(monkeypatch, HttpFetchResult(response=None, transient=True, circuit_open=True))
    fallback = AsyncMock(return_value=[_fallback_candidate()])
    monkeypatch.setattr(reversal_hunter, "discover_dexscreener_fallback_candidates", fallback)

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _env(), datetime.now(UTC))

    assert len(out) == 1
    assert out[0].source == "dexscreener"
    fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_birdeye_http_failure_falls_back_to_dexscreener(monkeypatch):
    _patch_provider(monkeypatch, HttpFetchResult(response=None, transient=True))
    fallback = AsyncMock(return_value=[_fallback_candidate()])
    monkeypatch.setattr(reversal_hunter, "discover_dexscreener_fallback_candidates", fallback)

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _env(), datetime.now(UTC))

    assert len(out) == 1
    assert out[0].source == "dexscreener"


@pytest.mark.asyncio
async def test_birdeye_empty_payload_falls_back_to_dexscreener(monkeypatch):
    _patch_provider(monkeypatch, HttpFetchResult(response=_FakeResponse(200, {"data": {"items": []}}), transient=False))
    fallback = AsyncMock(return_value=[_fallback_candidate()])
    monkeypatch.setattr(reversal_hunter, "discover_dexscreener_fallback_candidates", fallback)

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _env(), datetime.now(UTC))

    assert len(out) == 1
    assert out[0].source == "dexscreener"


@pytest.mark.asyncio
async def test_both_providers_empty_returns_empty_list_not_fabricated(monkeypatch):
    _patch_provider(monkeypatch, HttpFetchResult(response=None, transient=True))
    fallback = AsyncMock(return_value=[])
    monkeypatch.setattr(reversal_hunter, "discover_dexscreener_fallback_candidates", fallback)

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _env(), datetime.now(UTC))

    assert out == []


@pytest.mark.asyncio
async def test_dexscreener_fallback_exception_is_contained_not_raised(monkeypatch):
    _patch_provider(monkeypatch, HttpFetchResult(response=None, transient=True))
    fallback = AsyncMock(side_effect=RuntimeError("dexscreener boom"))
    monkeypatch.setattr(reversal_hunter, "discover_dexscreener_fallback_candidates", fallback)

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _env(), datetime.now(UTC))

    assert out == []
