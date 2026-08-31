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


# --- Discovery pipeline: GeckoTerminal-only pump.fun sourcing
# (engines/reversal_hunter.discover_meme_candidates). GeckoTerminal is the
# ONLY discovery source -- no Birdeye/DexScreener fallback runs inside
# discover_meme_candidates anymore. No scenario below loosens a filter or
# asserts on fabricated data: every candidate returned is a parsed
# GeckoTerminal pool whose relationships.dex.data.id matched the
# configured pump.fun dex-id set; provider failures degrade to an empty
# list rather than raising or fabricating a result.

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from whale_alpha.engines import reversal_hunter
from whale_alpha.utils.http_retry import HttpFetchResult
import base64

from whale_alpha.engines.reversal_hunter import DiscoveryCandidate, TokenMarketSnapshot


def _gt_env(**overrides):
    data = dict(
        DISCOVERY_GECKOTERMINAL_ENABLED=True,
        DISCOVERY_GECKOTERMINAL_API_BASE="https://api.geckoterminal.com/api/v2",
        DISCOVERY_GECKOTERMINAL_PUMPFUN_DEX_IDS="pump-fun,pumpswap",
        DISCOVERY_GECKOTERMINAL_PAGES=1,
        TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY=5,
        DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD=1,
        DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS=60,
        DISCOVERY_PROVIDER_MAX_RETRIES=2,
        DISCOVERY_PROVIDER_RETRY_BASE_SECONDS=0.1,
        DISCOVERY_PROVIDER_RETRY_MAX_SECONDS=1.0,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _gt_pool(mint: str, dex_id: str, name: str = "Cat / SOL"):
    return {
        "id": f"solana_{mint}_pool",
        "type": "pool",
        "attributes": {
            "base_token_price_usd": "0.0000044",
            "name": name,
            "pool_created_at": "2026-08-30T10:44:17Z",
            "fdv_usd": "4830.43",
            "market_cap_usd": None,
            "price_change_percentage": {"m5": "30.0", "h1": "12.0"},
            "transactions": {
                "m5": {"buys": 40, "sells": 26},
                "h1": {"buys": 90, "sells": 50},
            },
            "volume_usd": {"m5": "6612.31", "h1": "20000.0"},
            "reserve_in_usd": "42076.57",
        },
        "relationships": {
            "base_token": {"data": {"id": f"solana_{mint}", "type": "token"}},
            "quote_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112", "type": "token"}},
            "dex": {"data": {"id": dex_id, "type": "dex"}},
        },
    }


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


def _gt_patch(monkeypatch, result: HttpFetchResult):
    monkeypatch.setattr(reversal_hunter, "get_provider_client", lambda *a, **k: _FakeProvider(result))


@pytest.mark.asyncio
async def test_geckoterminal_keeps_pumpfun_and_pumpswap_drops_other_dex(monkeypatch):
    payload = {"data": [
        _gt_pool("MINT_PUMPFUN", "pump-fun"),
        _gt_pool("MINT_PUMPSWAP", "pumpswap"),
        _gt_pool("MINT_RAYDIUM", "raydium"),
    ]}
    _gt_patch(monkeypatch, HttpFetchResult(response=_FakeResponse(200, payload), transient=False))

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _gt_env(), datetime.now(UTC))

    mints = {c.snapshot.mint for c in out}
    assert mints == {"MINT_PUMPFUN", "MINT_PUMPSWAP"}
    assert all(c.source == "geckoterminal_pumpfun" for c in out)


@pytest.mark.asyncio
async def test_geckoterminal_maps_market_data_fields(monkeypatch):
    payload = {"data": [_gt_pool("MINT1", "pumpswap")]}
    _gt_patch(monkeypatch, HttpFetchResult(response=_FakeResponse(200, payload), transient=False))

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _gt_env(), datetime.now(UTC))

    assert len(out) == 1
    snap = out[0].snapshot
    assert snap.liquidity_usd == pytest.approx(42076.57)
    assert snap.volume_1h_usd == pytest.approx(20000.0)
    assert snap.buys_1h == 90
    assert snap.sells_1h == 50
    assert snap.price_change_1h_pct == pytest.approx(12.0)


@pytest.mark.asyncio
async def test_geckoterminal_disabled_returns_empty_no_fallback(monkeypatch):
    fetch_mock = AsyncMock()
    monkeypatch.setattr(reversal_hunter, "get_provider_client", fetch_mock)

    out = await reversal_hunter.discover_meme_candidates(
        AsyncMock(), _gt_env(DISCOVERY_GECKOTERMINAL_ENABLED=False), datetime.now(UTC)
    )

    assert out == []
    fetch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_geckoterminal_circuit_open_returns_empty_not_fabricated(monkeypatch):
    _gt_patch(monkeypatch, HttpFetchResult(response=None, transient=True, circuit_open=True))

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _gt_env(), datetime.now(UTC))

    assert out == []


@pytest.mark.asyncio
async def test_geckoterminal_http_failure_returns_empty_not_fabricated(monkeypatch):
    _gt_patch(monkeypatch, HttpFetchResult(response=None, transient=True))

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _gt_env(), datetime.now(UTC))

    assert out == []


@pytest.mark.asyncio
async def test_geckoterminal_http_error_status_returns_empty(monkeypatch):
    _gt_patch(monkeypatch, HttpFetchResult(response=_FakeResponse(403, {"error": "forbidden"}), transient=False))

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _gt_env(), datetime.now(UTC))

    assert out == []


@pytest.mark.asyncio
async def test_geckoterminal_invalid_json_returns_empty_not_raises(monkeypatch):
    class _BadJsonResponse:
        status_code = 200

        def json(self):
            raise ValueError("bad json")

    _gt_patch(monkeypatch, HttpFetchResult(response=_BadJsonResponse(), transient=False))

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _gt_env(), datetime.now(UTC))

    assert out == []


@pytest.mark.asyncio
async def test_geckoterminal_empty_page_returns_empty_list(monkeypatch):
    _gt_patch(monkeypatch, HttpFetchResult(response=_FakeResponse(200, {"data": []}), transient=False))

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _gt_env(), datetime.now(UTC))

    assert out == []


@pytest.mark.asyncio
async def test_geckoterminal_dedupes_repeated_mints(monkeypatch):
    payload = {"data": [
        _gt_pool("MINT_DUP", "pump-fun"),
        _gt_pool("MINT_DUP", "pumpswap"),
    ]}
    _gt_patch(monkeypatch, HttpFetchResult(response=_FakeResponse(200, payload), transient=False))

    out = await reversal_hunter.discover_meme_candidates(AsyncMock(), _gt_env(), datetime.now(UTC))

    assert len(out) == 1


# --- evaluate_candidate: authority-data availability vs. genuine findings.
# All-provider RPC outages (Helius rate-limited, dRPC 400s, Ankr 403s --
# exactly what production saw) must degrade to "we don't know", never to a
# hard reject or an uncaught exception. A real MINT_AUTHORITY_ACTIVE /
# FREEZE_AUTHORITY_ACTIVE finding, when data IS available, must still
# hard-reject -- this is not a weakening of the safety gate.

class _RaisingConnection:
    """Simulates every routed RPC provider failing -- exactly the
    production log signature (Helius 429, dRPC 400, Ankr 403 -> all
    routes exhausted -> _FailoverAsyncClient raises)."""

    async def get_account_info(self, *args, **kwargs):
        raise RuntimeError("all routed providers failed")


class _MintAuthorityActiveConnection:
    """A connection that returns real, parseable mint-account data with
    a live mint authority -- a genuine security finding, not a missing-data
    case."""

    async def get_account_info(self, *args, **kwargs):
        raw = bytearray(82)
        raw[0:4] = (1).to_bytes(4, "little")
        return SimpleNamespace(value=SimpleNamespace(data=(base64.b64encode(bytes(raw)).decode(), "base64")))


def _permissive_env(**overrides):
    data = dict(
        BIRDEYE_API_KEY="k", BITQUERY_API_KEY="k",
        WHALE_ALPHA_MIN_MC_USD=0, WHALE_ALPHA_MAX_MC_USD=1e18,
        WHALE_ALPHA_MIN_LIQ_USD=0, WHALE_ALPHA_MAX_LIQ_USD=1e18,
        WHALE_ALPHA_MIN_LIQ_MC_RATIO=0, WHALE_ALPHA_MAX_LIQ_MC_RATIO=1e18,
        WHALE_ALPHA_MIN_PAIR_AGE_HOURS=0, WHALE_ALPHA_MAX_PAIR_AGE_DAYS=1e9,
        WHALE_ALPHA_BUY_SELL_RATIO_MIN=0, WHALE_ALPHA_MAX_TOP10_PCT=1e9,
        WHALE_ALPHA_MAX_SINGLE_WALLET_PCT=1e9, WHALE_ALPHA_MAX_DEV_HOLD_PCT=1e9,
        WHALE_ALPHA_MAX_TAGGED_RISK_PCT=1e9, WHALE_ALPHA_MIN_CONFIDENCE=0,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _snapshot(mint="6cxUz3xUZ4de87ETGnVnLAURKuVrjPnX1cMa3wP4pump"):
    return TokenMarketSnapshot(
        mint=mint, name="Test", symbol="TST", pair_address="pair", dex_id="pumpswap",
        created_at_ms=None, price_usd=1.0, market_cap_usd=100_000.0, liquidity_usd=20_000.0,
        volume_5m_usd=0.0, volume_1h_usd=0.0, buys_5m=0, sells_5m=0, buys_1h=0, sells_1h=0,
        price_change_5m_pct=0.0, price_change_1h_pct=0.0, metadata_present=True,
    )


def _patch_evaluate_candidate_deps(monkeypatch, snapshot):
    monkeypatch.setattr(reversal_hunter, "enrich_token", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(reversal_hunter, "_fetch_ohlcv", AsyncMock(return_value=[]))
    monkeypatch.setattr(reversal_hunter, "_fetch_market_overview", AsyncMock(return_value=None))
    monkeypatch.setattr(reversal_hunter, "_fetch_trade_data", AsyncMock(return_value=None))
    monkeypatch.setattr(reversal_hunter, "_fetch_holder_profile", AsyncMock(return_value=None))
    monkeypatch.setattr(reversal_hunter, "_fetch_top_holders", AsyncMock(return_value=[]))
    monkeypatch.setattr(reversal_hunter, "_fetch_risk_positions", AsyncMock(return_value=[]))
    monkeypatch.setattr(reversal_hunter, "_fetch_top_traders", AsyncMock(return_value=[]))
    monkeypatch.setattr(reversal_hunter, "_fetch_security", AsyncMock(return_value=None))
    monkeypatch.setattr(reversal_hunter, "_fetch_bitquery_flow", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_all_rpc_providers_down_does_not_crash_or_hard_reject(monkeypatch):
    """Reproduces the exact production scenario (Helius 429 + dRPC 400 +
    Ankr 403 -> every routed provider fails -> _FailoverAsyncClient raises)
    that was silently converted into EVALUATION_ERROR for every single
    candidate, driving approved=0 despite discovered=38-40 per cycle."""
    snapshot = _snapshot()
    candidate = DiscoveryCandidate(snapshot=snapshot, source="geckoterminal_pumpfun")
    _patch_evaluate_candidate_deps(monkeypatch, snapshot)

    result = await reversal_hunter.evaluate_candidate(
        AsyncMock(), _permissive_env(), candidate, _RaisingConnection(), datetime.now(UTC), []
    )

    assert "EVALUATION_ERROR" not in result.hard_rejects
    assert "AUTHORITY_DATA_MISSING" not in result.hard_rejects


@pytest.mark.asyncio
async def test_no_connection_does_not_hard_reject_on_missing_authority_data(monkeypatch):
    snapshot = _snapshot()
    candidate = DiscoveryCandidate(snapshot=snapshot, source="geckoterminal_pumpfun")
    _patch_evaluate_candidate_deps(monkeypatch, snapshot)

    result = await reversal_hunter.evaluate_candidate(
        AsyncMock(), _permissive_env(), candidate, None, datetime.now(UTC), []
    )

    assert "AUTHORITY_DATA_MISSING" not in result.hard_rejects


@pytest.mark.asyncio
async def test_genuine_mint_authority_active_still_hard_rejects(monkeypatch):
    """The safety gate itself must be untouched: when authority data IS
    available and shows a live mint authority, that must still block."""
    snapshot = _snapshot()
    candidate = DiscoveryCandidate(snapshot=snapshot, source="geckoterminal_pumpfun")
    _patch_evaluate_candidate_deps(monkeypatch, snapshot)

    result = await reversal_hunter.evaluate_candidate(
        AsyncMock(), _permissive_env(), candidate, _MintAuthorityActiveConnection(), datetime.now(UTC), []
    )

    assert "MINT_AUTHORITY_ACTIVE" in result.hard_rejects
