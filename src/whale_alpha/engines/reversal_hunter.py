"""Strict data-only Solana meme dip -> consolidation -> reversal hunter.

This module intentionally fails closed. A token is never promoted from partial,
stale, contradictory, or missing evidence.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from solders.pubkey import Pubkey
from whale_alpha.config import Env
from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot, enrich_token
from whale_alpha.integrations.token_hunter_sources import DiscoveryCandidate, discover_dexscreener_fallback_candidates
from whale_alpha.utils.http_retry import get_provider_client
from whale_alpha.utils.logger import child_logger

log = child_logger("reversalHunter")

SOLANA = "solana"
STALE_SECONDS = 300
RISK_LABELS = ("dev", "bundler", "sniper", "insider")


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class PatternEvidence:
    dip_pct: float
    dip_lookback_hours: float
    consolidation_minutes: float
    consolidation_range_pct: float
    breakout_confirmed: bool
    breakout_volume_5m_mult: float
    breakout_volume_15m_mult: float
    current_price: float


@dataclass(frozen=True)
class FlowEvidence:
    volume_5m: float
    volume_15m: float
    volume_5m_vs_avg: float
    volume_15m_vs_avg: float
    buy_sell_ratio: float
    net_buy_pressure: bool
    smart_money_status: str
    top_trader_status: str
    buy_volume_15m: float
    sell_volume_15m: float


@dataclass(frozen=True)
class OnChainEvidence:
    top10_pct: float
    largest_wallet_pct: float
    dev_hold_pct: float
    tagged_risk_pct: float
    tagged_net_buy: bool
    security_flags: tuple[str, ...]
    authority_flags: tuple[str, ...]


@dataclass(frozen=True)
class ReversalAnalysis:
    candidate: DiscoveryCandidate
    snapshot: TokenMarketSnapshot
    pattern: PatternEvidence | None
    flow: FlowEvidence | None
    onchain: OnChainEvidence | None
    score: float
    tier: str
    hard_rejects: tuple[str, ...]
    reasons: tuple[str, ...]
    invalidation: str
    evidence: dict[str, Any]

    @property
    def approved(self) -> bool:
        return not self.hard_rejects and self.pattern is not None and self.flow is not None and self.onchain is not None and self.score >= 80


def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "tokens", "results", "rows", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = _rows(value)
            if nested:
                return nested
    return []


def _obj(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _deep_find(obj: Any, keys: tuple[str, ...]) -> Any:
    wanted = {k.lower() for k in keys}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in wanted:
                return v
        for v in obj.values():
            found = _deep_find(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find(v, keys)
            if found is not None:
                return found
    return None


def _parse_candles(payload: Any) -> list[Candle]:
    raw = _rows(payload)
    if not raw:
        data = _obj(payload)
        for key in ("items", "candles", "ohlcv", "data"):
            if isinstance(data.get(key), list):
                raw = [x for x in data[key] if isinstance(x, dict)]
                break
    candles: list[Candle] = []
    for row in raw:
        ts = _num(row.get("unixTime") or row.get("timestamp") or row.get("time") or row.get("t"))
        o = _num(row.get("o") or row.get("open"))
        h = _num(row.get("h") or row.get("high"))
        l = _num(row.get("l") or row.get("low"))
        c = _num(row.get("c") or row.get("close"))
        v = _num(row.get("v") or row.get("volume"))
        if None not in (ts, o, h, l, c, v) and h > 0 and l > 0 and c > 0:
            candles.append(Candle(int(ts), float(o), float(h), float(l), float(c), max(0.0, float(v))))
    return sorted({c.ts: c for c in candles}.values(), key=lambda x: x.ts)


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _std(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def _returns(candles: list[Candle]) -> list[float]:
    out: list[float] = []
    for a, b in zip(candles, candles[1:]):
        if a.close > 0:
            out.append((b.close / a.close) - 1.0)
    return out


def _volume_mult(current: float, baseline: list[float]) -> float:
    avg = _mean(baseline)
    return current / avg if avg > 0 else 0.0


def detect_dip_consolidation_breakout(candles: list[Candle], now_ts: int) -> PatternEvidence | None:
    """Find a fully confirmed sequence in 5m candles; fail closed if ambiguous."""
    if len(candles) < 60:
        return None
    recent = [c for c in candles if now_ts - 72 * 3600 <= c.ts <= now_ts]
    if len(recent) < 60:
        return None
    # Require two closes above the consolidation range high: this rejects a one-candle spike.
    breakout = recent[-2:]
    if len(breakout) < 2:
        return None
    for n in range(9, min(216, len(recent) - 12) + 1):  # 45m .. 18h, 5m candles
        cons_end = len(recent) - 2
        cons_start = cons_end - n
        if cons_start < 3:
            continue
        window = recent[cons_start:cons_end]
        range_high = max(c.high for c in window)
        range_low = min(c.low for c in window)
        mid = _mean([c.close for c in window])
        if mid <= 0:
            continue
        range_pct = (range_high - range_low) / mid * 100.0
        if range_pct > 12.0:
            continue
        prior = recent[max(0, cons_start - n):cons_start]
        if len(prior) < max(6, n // 2):
            continue
        if _std(_returns(window)) >= _std(_returns(prior)) * 0.95 and _std(_returns(window)) > 0.002:
            continue
        if breakout[0].close <= range_high or breakout[1].close <= range_high:
            continue
        if breakout[1].close < breakout[1].high * 0.95:
            continue
        low_idx = min(range(cons_start), key=lambda i: recent[i].low)
        if low_idx <= 0:
            continue
        low = recent[low_idx].low
        high_candidates = recent[max(0, low_idx - 864):low_idx]
        if not high_candidates:
            continue
        high = max(c.high for c in high_candidates)
        if high <= 0:
            continue
        dip_pct = (high - low) / high * 100.0
        if not 15.0 <= dip_pct <= 50.0:
            continue
        lookback_h = (recent[low_idx].ts - high_candidates[0].ts) / 3600.0
        # The high must be 6h-72h before the latest breakout, not a micro-pullback.
        high_ts = max(c.ts for c in high_candidates if c.high == high)
        if not 6 * 3600 <= breakout[0].ts - high_ts <= 72 * 3600:
            continue
        # Consolidation must occur after the dip and price must stop making aggressive new lows.
        if low_idx >= cons_start - 1:
            continue
        if min(c.low for c in window) < low * 0.995:
            continue
        vol5 = breakout[1].volume
        prev_5m = [c.volume for c in recent[max(0, len(recent)-14):len(recent)-2]]
        vol5_mult = _volume_mult(vol5, prev_5m)
        # 15m breakout volume vs preceding 15m buckets.
        last15 = sum(c.volume for c in recent[-3:])
        baseline15: list[float] = []
        for i in range(max(0, len(recent)-14), len(recent)-2, 3):
            chunk = recent[i:i+3]
            if len(chunk) == 3:
                baseline15.append(sum(x.volume for x in chunk))
        vol15_mult = _volume_mult(last15, baseline15)
        if vol5_mult < 1.8 or vol15_mult < 1.8:
            continue
        return PatternEvidence(
            dip_pct=round(dip_pct, 2),
            dip_lookback_hours=round((breakout[1].ts - high_ts) / 3600.0, 2),
            consolidation_minutes=round(n * 5.0, 2),
            consolidation_range_pct=round(range_pct, 2),
            breakout_confirmed=True,
            breakout_volume_5m_mult=round(vol5_mult, 2),
            breakout_volume_15m_mult=round(vol15_mult, 2),
            current_price=breakout[1].close,
        )
    return None


async def _birdeye_get(client: httpx.AsyncClient, env: Env, path: str, params: dict[str, Any]) -> Any | None:
    if not env.BIRDEYE_API_KEY:
        return None
    provider = get_provider_client(
        "birdeye_reversal",
        max_concurrency=env.TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY,
        failure_threshold=env.DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds=env.DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
    )
    result = await provider.get(
        client,
        f"{env.DISCOVERY_BIRDEYE_API_BASE.rstrip('/')}{path}",
        params=params,
        headers={"X-API-KEY": env.BIRDEYE_API_KEY, "x-chain": SOLANA},
        max_retries=env.DISCOVERY_PROVIDER_MAX_RETRIES,
        base_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_BASE_SECONDS,
        max_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_MAX_SECONDS,
    )
    if result.response is None or result.response.status_code >= 400:
        return None
    try:
        return result.response.json()
    except ValueError:
        return None


async def _discover_birdeye_meme_candidates(
    client: httpx.AsyncClient, env: Env, now: datetime
) -> tuple[list[DiscoveryCandidate], str]:
    """Birdeye meme-list discovery. Returns (candidates, status) instead of
    silently collapsing every failure mode to an empty list — the caller
    (discover_meme_candidates) needs to know *why* Birdeye produced nothing
    so it can decide whether to fall back, and so the reason is visible in
    Railway logs instead of just a bare `discovered=0`. No filter or
    candidate-shape change from the previous implementation."""
    if not env.BIRDEYE_API_KEY:
        return [], "no_api_key"
    if not env.DISCOVERY_BIRDEYE_ENABLED:
        return [], "disabled"
    params = {
        "sort_type": "desc",
        "source": "all",
        "min_creation_time": int((now - timedelta(days=30)).timestamp()),
        "max_creation_time": int(now.timestamp()),
        "min_liquidity": env.WHALE_ALPHA_MIN_LIQ_USD,
        "max_liquidity": env.WHALE_ALPHA_MAX_LIQ_USD,
        "min_market_cap": env.WHALE_ALPHA_MIN_MC_USD,
        "max_market_cap": env.WHALE_ALPHA_MAX_MC_USD,
        "limit": min(env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE, 50),
    }
    provider = get_provider_client(
        "birdeye_reversal",
        max_concurrency=env.TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY,
        failure_threshold=env.DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds=env.DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
    )
    result = await provider.get(
        client,
        f"{env.DISCOVERY_BIRDEYE_API_BASE.rstrip('/')}/defi/v3/token/meme/list",
        params=params,
        headers={"X-API-KEY": env.BIRDEYE_API_KEY, "x-chain": SOLANA},
        max_retries=env.DISCOVERY_PROVIDER_MAX_RETRIES,
        base_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_BASE_SECONDS,
        max_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_MAX_SECONDS,
    )
    if result.circuit_open:
        return [], "circuit_open"
    if result.response is None:
        return [], "http_failure"
    if result.response.status_code >= 400:
        return [], f"http_{result.response.status_code}"
    try:
        payload = result.response.json()
    except ValueError:
        return [], "invalid_json"
    rows = _rows(payload)
    out: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    for row in rows:
        mint = row.get("address") or row.get("token_address") or row.get("mint")
        if not isinstance(mint, str) or not mint or mint in seen:
            continue
        seen.add(mint)
        out.append(DiscoveryCandidate(
            snapshot=TokenMarketSnapshot(
                mint=mint,
                name=row.get("name") if isinstance(row.get("name"), str) else None,
                symbol=row.get("symbol") if isinstance(row.get("symbol"), str) else None,
                pair_address=None,
                dex_id=None,
                created_at_ms=int((_num(row.get("creation_time") or row.get("created_at")) or 0) * (1000 if (_num(row.get("creation_time") or row.get("created_at")) or 0) < 10_000_000_000 else 1)) or None,
                price_usd=_num(row.get("price")),
                market_cap_usd=_num(row.get("market_cap") or row.get("marketCap")),
                liquidity_usd=_num(row.get("liquidity")),
                volume_5m_usd=0.0,
                volume_1h_usd=0.0,
                buys_5m=0,
                sells_5m=0,
                buys_1h=0,
                sells_1h=0,
                price_change_5m_pct=0.0,
                price_change_1h_pct=0.0,
                metadata_present=bool(row.get("website") or row.get("twitter") or row.get("telegram") or row.get("name")),
                source="birdeye_meme",
            ),
            source="birdeye_meme",
        ))
    return out, ("ok" if out else "empty_payload")


async def discover_meme_candidates(client: httpx.AsyncClient, env: Env, now: datetime) -> list[DiscoveryCandidate]:
    """Strict Whale Alpha candidate discovery. Birdeye's meme-list is the
    primary source; if it produces zero candidates for any reason (missing
    key, disabled, open circuit breaker, HTTP failure, or a genuinely empty
    result) we fall back to the already-approved DexScreener discovery path
    (integrations/token_hunter_sources.py) rather than silently returning an
    empty pipeline. This changes *where candidates come from*, never *what
    counts as a valid candidate* — every candidate, from either source, still
    goes through the same prefilter/evaluate/final-audit hard gates
    downstream. No filter is loosened and nothing here fabricates data."""
    birdeye_candidates, birdeye_status = await _discover_birdeye_meme_candidates(client, env, now)
    log.info(
        "discovery_source_result",
        source="birdeye_meme",
        candidates=len(birdeye_candidates),
        status=birdeye_status,
    )
    if birdeye_candidates:
        return birdeye_candidates

    log.warning(
        "discovery_birdeye_empty_falling_back",
        reason=birdeye_status,
        fallback_source="dexscreener",
    )
    try:
        fallback_candidates = await discover_dexscreener_fallback_candidates(
            client, env, limit=min(env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE, 50)
        )
    except Exception as err:  # noqa: BLE001 — one provider must never take down discovery
        log.warning("discovery_source_failed", source="dexscreener", err=str(err))
        fallback_candidates = []
    log.info(
        "discovery_source_result",
        source="dexscreener",
        candidates=len(fallback_candidates),
        status="fallback_used" if fallback_candidates else "empty",
    )
    return fallback_candidates


async def _fetch_ohlcv(client: httpx.AsyncClient, env: Env, mint: str, now: datetime) -> list[Candle]:
    payload = await _birdeye_get(
        client, env, "/defi/v3/ohlcv", {
            "address": mint,
            "type": "5m",
            "time_from": int((now - timedelta(hours=72)).timestamp()),
            "time_to": int(now.timestamp()),
            "currency": "usd",
            "mode": "range",
            "padding": "false",
            "outlier": "false",
        }
    )
    return _parse_candles(payload)


async def _fetch_market_overview(client: httpx.AsyncClient, env: Env, mint: str) -> dict[str, Any] | None:
    return _obj(await _birdeye_get(client, env, "/defi/token_overview", {"address": mint, "frames": "5m,30m,1h,4h,24h"}))


async def _fetch_trade_data(client: httpx.AsyncClient, env: Env, mint: str) -> dict[str, Any] | None:
    return _obj(await _birdeye_get(client, env, "/defi/v3/token/trade-data/single", {"address": mint, "frames": "5m,15m,1h"}))


async def _fetch_holder_profile(client: httpx.AsyncClient, env: Env, mint: str) -> dict[str, Any] | None:
    return _obj(await _birdeye_get(client, env, "/token/v1/holder-profile", {"token_address": mint, "interval": "1h", "include_zero_balance": "true"}))


async def _fetch_top_holders(client: httpx.AsyncClient, env: Env, mint: str) -> list[dict[str, Any]]:
    payload = await _birdeye_get(client, env, "/defi/v3/token/holder", {"address": mint, "mode": "wallet", "limit": 10, "sort_by": "amount", "sort_type": "desc"})
    return _rows(payload)


async def _fetch_risk_positions(client: httpx.AsyncClient, env: Env, mint: str) -> list[dict[str, Any]]:
    payload = await _birdeye_get(client, env, "/token/v1/holder-positions", {
        "token_address": mint,
        "labels": ",".join(RISK_LABELS),
        "sort_by": "amount",
        "order_type": "desc",
        "include_zero_balance": "true",
        "limit": 50,
    })
    return _rows(payload)


async def _fetch_top_traders(client: httpx.AsyncClient, env: Env, mint: str) -> list[dict[str, Any]]:
    payload = await _birdeye_get(client, env, "/defi/v2/tokens/top_traders", {
        "address": mint,
        "time_frame": "24h",
        "sort_by": "volume_usd",
        "sort_type": "desc",
        "limit": 10,
    })
    return _rows(payload)


async def _fetch_smart_money_token_list(client: httpx.AsyncClient, env: Env) -> list[dict[str, Any]] | None:
    payload = await _birdeye_get(client, env, "/smart-money/v1/token/list", {
        "interval": "1d", "trader_style": "all", "sort_by": "net_flow", "sort_type": "desc", "limit": 20,
    })
    if payload is None:
        return None
    return _rows(payload)


async def _fetch_security(client: httpx.AsyncClient, env: Env, mint: str) -> dict[str, Any] | None:
    return _obj(await _birdeye_get(client, env, "/defi/token_security", {"address": mint}))


async def _fetch_bitquery_flow(client: httpx.AsyncClient, env: Env, mint: str, pair: str | None) -> tuple[float, float] | None:
    if not env.BITQUERY_API_KEY:
        return None
    where = f"""where: {{Block: {{Time: {{since_relative: {{minutes_ago: 15}}}}}}, Transaction: {{Result: {{Success: true}}}}, Trade: {{Currency: {{MintAddress: {{is: "{mint}"}}}}, Side: {{Currency: {{MintAddress: {{is: "So11111111111111111111111111111111111111112"}}}}}}}}}}}}"""
    query = f"""query WhaleAlphaFlow {{ Solana(dataset: realtime) {{ DEXTradeByTokens({where}) {{ buy_volume: sum(of: Trade_Side_AmountInUSD, if: {{Trade: {{Side: {{Type: {{is: buy}}}}}}}}) sell_volume: sum(of: Trade_Side_AmountInUSD, if: {{Trade: {{Side: {{Type: {{is: sell}}}}}}}}) }} }} }}"""
    provider = get_provider_client("bitquery_reversal", max_concurrency=1, failure_threshold=1, cooldown_seconds=60)
    result = await provider.post(
        client, env.BITQUERY_API_BASE, json={"query": query},
        headers={"Authorization": f"Bearer {env.BITQUERY_API_KEY}", "Content-Type": "application/json"},
        max_retries=env.DISCOVERY_PROVIDER_MAX_RETRIES,
        base_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_BASE_SECONDS,
        max_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_MAX_SECONDS,
    )
    if result.response is None or result.response.status_code >= 400:
        return None
    try:
        payload = result.response.json()
        rows = payload.get("data", {}).get("Solana", {}).get("DEXTradeByTokens", [])
        if not rows:
            return (0.0, 0.0)
        row = rows[0]
        return (_num(row.get("buy_volume")) or 0.0, _num(row.get("sell_volume")) or 0.0)
    except (ValueError, TypeError, AttributeError):
        return None


def _frame(obj: dict[str, Any], frame: str) -> dict[str, Any]:
    for container_key in ("frames", "data", "stats"):
        container = obj.get(container_key)
        if isinstance(container, dict) and isinstance(container.get(frame), dict):
            return container[frame]
    if isinstance(obj.get(frame), dict):
        return obj[frame]
    return {}


def _flow_from_trade_data(data: dict[str, Any] | None) -> tuple[float, float, float, float] | None:
    if not data:
        return None
    f5 = _frame(data, "5m")
    f15 = _frame(data, "15m")
    def pick(frame: dict[str, Any], *keys: str) -> float:
        for k in keys:
            v = _deep_find(frame, (k,))
            n = _num(v)
            if n is not None:
                return n
        return 0.0
    buy5 = pick(f5, "buy_volume_usd", "buyVolume", "buy_volume", "buy_volume_usd")
    sell5 = pick(f5, "sell_volume_usd", "sellVolume", "sell_volume", "sell_volume_usd")
    buy15 = pick(f15, "buy_volume_usd", "buyVolume", "buy_volume", "buy_volume_usd")
    sell15 = pick(f15, "sell_volume_usd", "sellVolume", "sell_volume", "sell_volume_usd")
    return buy5, sell5, buy15, sell15


def _security_flags(security: dict[str, Any] | None) -> tuple[str, ...]:
    if not security:
        return ("SECURITY_DATA_MISSING",)
    flags: list[str] = []
    checks = {
        "fake_token": "FAKE_TOKEN",
        "honeypot": "HONEYPOT",
        "freezable": "FREEZABLE",
        "freeze_authority": "FREEZE_AUTHORITY_ACTIVE",
        "mintable": "MINTABLE",
        "buy_tax": "BUY_TAX",
        "sell_tax": "SELL_TAX",
        "transfer_fee": "TRANSFER_FEE",
        "transfer_fee_config_authority": "TRANSFER_FEE_AUTHORITY_ACTIVE",
        "mutable": "MUTABLE_INFO",
    }
    for key, label in checks.items():
        value = _deep_find(security, (key,))
        if isinstance(value, bool) and value:
            flags.append(label)
        elif isinstance(value, (int, float)) and value > 0:
            flags.append(label)
        elif isinstance(value, str) and value.lower() in {"true", "fail", "critical", "high"}:
            flags.append(label)
    # Security response may expose a generic score/risk status.
    risk = _deep_find(security, ("risk", "risk_level", "security_level"))
    if isinstance(risk, str) and risk.lower() in {"critical", "high"}:
        flags.append(f"SECURITY_{risk.upper()}")
    return tuple(dict.fromkeys(flags))


def _authority_flags(account_info: Any) -> tuple[str, ...]:
    try:
        value = account_info.value if hasattr(account_info, "value") else account_info
        if value is None:
            return ("AUTHORITY_DATA_MISSING",)
        data = value.data if hasattr(value, "data") else value.get("data")
        if isinstance(data, tuple):
            encoded = data[0]
            raw = base64.b64decode(encoded)
        elif isinstance(data, list):
            raw = base64.b64decode(data[0])
        else:
            return ("AUTHORITY_DATA_MISSING",)
        if len(raw) < 82:
            return ("AUTHORITY_DATA_MISSING",)
        mint_opt = int.from_bytes(raw[0:4], "little")
        freeze_opt = int.from_bytes(raw[46:50], "little")
        flags: list[str] = []
        if mint_opt != 0:
            flags.append("MINT_AUTHORITY_ACTIVE")
        if freeze_opt != 0:
            flags.append("FREEZE_AUTHORITY_ACTIVE")
        return tuple(flags)
    except Exception:
        return ("AUTHORITY_DATA_MISSING",)


def _extract_pct(obj: Any, *keys: str) -> float | None:
    v = _deep_find(obj, tuple(keys))
    n = _num(v)
    if n is None:
        return None
    # APIs may return 0..1 or 0..100.
    return n * 100.0 if 0 <= n <= 1.0 else n


def _holder_evidence(profile: dict[str, Any] | None, holders: list[dict[str, Any]], positions: list[dict[str, Any]]) -> tuple[float, float, float, float, bool] | None:
    if not profile or not holders:
        return None
    top10 = _extract_pct(profile, "top10_holder", "top10_holder_pct", "top10_percent", "top10HolderPercentage")
    if top10 is None:
        top10 = _extract_pct(holders, "percent_of_supply", "percentage", "percent")
    largest = _extract_pct(holders[0], "percent_of_supply", "percentage", "percent", "share")
    tags = profile.get("tags") if isinstance(profile, dict) else None
    dev = _extract_pct(tags.get("dev") if isinstance(tags, dict) else profile, "percent_of_supply", "percent", "supply_share")
    tagged = 0.0
    if isinstance(tags, dict):
        for label in RISK_LABELS:
            tagged += _extract_pct(tags.get(label), "percent_of_supply", "percent", "supply_share") or 0.0
    else:
        for row in positions:
            tagged += _extract_pct(row, "percent_of_supply", "percentage", "percent") or 0.0
    if top10 is None or largest is None:
        return None
    buy = sell = 0.0
    for row in positions:
        buy += _num(row.get("buy_volume") or row.get("buy_volume_usd") or row.get("buyVolume")) or 0.0
        sell += _num(row.get("sell_volume") or row.get("sell_volume_usd") or row.get("sellVolume")) or 0.0
    if not positions and isinstance(tags, dict):
        for label in RISK_LABELS:
            cohort = tags.get(label)
            buy += _num(_deep_find(cohort, ("buy_volume", "buyVolume", "buy_volume_usd"))) or 0.0
            sell += _num(_deep_find(cohort, ("sell_volume", "sellVolume", "sell_volume_usd"))) or 0.0
    if buy == 0 and sell == 0:
        return None
    return top10, largest, dev or 0.0, tagged, buy >= sell


def _flow_evidence(trade: tuple[float, float, float, float] | None, candles: list[Candle], smart_rows: list[dict[str, Any]], top_rows: list[dict[str, Any]], mint: str, bitquery: tuple[float, float] | None) -> FlowEvidence | None:
    if not trade or len(candles) < 20:
        return None
    buy5, sell5, buy15, sell15 = trade
    if buy15 <= 0 and sell15 <= 0:
        return None
    ratio = buy15 / sell15 if sell15 > 0 else math.inf
    if bitquery is not None:
        bq_buy, bq_sell = bitquery
        if (buy15 + sell15) > 0 and (bq_buy + bq_sell) > 0:
            a = buy15 / (buy15 + sell15)
            b = bq_buy / (bq_buy + bq_sell)
            if abs(a - b) > 0.30:
                return None
    last = candles[-3:]
    volume5 = last[-1].volume
    volume15 = sum(c.volume for c in last)
    baseline5 = [c.volume for c in candles[-15:-3]]
    baseline15 = [sum(c.volume for c in candles[i:i+3]) for i in range(max(0, len(candles)-39), len(candles)-3, 3) if len(candles[i:i+3]) == 3]
    smart = any(str(r.get("address") or r.get("token_address") or r.get("mint") or "") == mint for r in smart_rows)
    smart_flow = "SUPPORTIVE" if smart else "NOT_AGGRESSIVELY_SELLING"
    top_buy = top_sell = 0.0
    for row in top_rows:
        top_buy += _num(row.get("buy_volume") or row.get("buy_volume_usd") or row.get("buyVolume")) or 0.0
        top_sell += _num(row.get("sell_volume") or row.get("sell_volume_usd") or row.get("sellVolume")) or 0.0
    top_status = "UNKNOWN" if top_buy == 0 and top_sell == 0 else ("SUPPORTIVE" if top_buy >= top_sell else "SELLING")
    return FlowEvidence(
        volume_5m=round(volume5, 2), volume_15m=round(volume15, 2),
        volume_5m_vs_avg=round(_volume_mult(volume5, baseline5), 2),
        volume_15m_vs_avg=round(_volume_mult(volume15, baseline15), 2),
        buy_sell_ratio=round(ratio, 3) if math.isfinite(ratio) else 999.0,
        net_buy_pressure=buy15 > sell15 and buy5 >= sell5,
        smart_money_status=smart_flow,
        top_trader_status=top_status,
        buy_volume_15m=round(buy15, 2), sell_volume_15m=round(sell15, 2),
    )


def _tier(score: float) -> str:
    if score >= 93: return "HIGH-CONVICTION WATCH"
    if score >= 87: return "STRONG WATCH"
    return "EARLY WATCH"


def score_reversal(pattern: PatternEvidence, flow: FlowEvidence, onchain: OnChainEvidence, security_ok: bool, liquidity_quality: float, market_quality: float, smart_quality: float) -> tuple[float, tuple[str, ...]]:
    price = min(100.0, 55 + min(pattern.dip_pct, 50) * 0.8 + min(pattern.breakout_volume_5m_mult, 4) * 4 + min(pattern.breakout_volume_15m_mult, 4) * 4)
    liquidity = liquidity_quality
    holder = max(0.0, 100 - onchain.top10_pct * 1.8 - onchain.largest_wallet_pct * 2.0 - onchain.dev_hold_pct * 2.0 - onchain.tagged_risk_pct * 0.8)
    smart = smart_quality
    security = 100.0 if security_ok else 0.0
    total = round(price * 0.30 + liquidity * 0.20 + holder * 0.20 + smart * 0.15 + security * 0.15, 2)
    reasons = []
    if pattern.dip_pct >= 15: reasons.append(f"Confirmed {pattern.dip_pct:.1f}% dip before reversal")
    if pattern.consolidation_minutes >= 45: reasons.append(f"{pattern.consolidation_minutes:.0f}m compressed consolidation")
    if pattern.breakout_volume_5m_mult >= 1.8: reasons.append(f"5m breakout volume {pattern.breakout_volume_5m_mult:.2f}x baseline")
    if flow.net_buy_pressure and flow.buy_sell_ratio >= 1.15: reasons.append(f"Net buy pressure with {flow.buy_sell_ratio:.2f} buy/sell ratio")
    if flow.smart_money_status == "SUPPORTIVE": reasons.append("Smart-money flow supportive")
    return total, tuple(reasons[:3])


async def evaluate_candidate(client: httpx.AsyncClient, env: Env, candidate: DiscoveryCandidate, connection: Any | None, now: datetime, smart_rows: list[dict[str, Any]]) -> ReversalAnalysis:
    snapshot = await enrich_token(client, env, candidate.snapshot.mint)
    if snapshot is None:
        return ReversalAnalysis(candidate, candidate.snapshot, None, None, None, 0, "NO SIGNAL", ("DEX_DATA_MISSING",), (), "Invalid if market data cannot be refreshed within 5 minutes.", {})
    mcap = snapshot.market_cap_usd or 0.0
    liq = snapshot.liquidity_usd or 0.0
    ratio = liq / mcap if mcap else 0.0
    rejects: list[str] = []
    if not env.BIRDEYE_API_KEY: rejects.append("BIRDEYE_DATA_UNAVAILABLE")
    if not env.BITQUERY_API_KEY: rejects.append("BITQUERY_DATA_UNAVAILABLE")
    if not (env.WHALE_ALPHA_MIN_MC_USD <= mcap <= env.WHALE_ALPHA_MAX_MC_USD): rejects.append("MARKET_CAP_OUT_OF_BAND")
    if not (env.WHALE_ALPHA_MIN_LIQ_USD <= liq <= env.WHALE_ALPHA_MAX_LIQ_USD): rejects.append("LIQUIDITY_OUT_OF_BAND")
    if not (env.WHALE_ALPHA_MIN_LIQ_MC_RATIO <= ratio <= env.WHALE_ALPHA_MAX_LIQ_MC_RATIO): rejects.append("LIQUIDITY_MC_RATIO_OUT_OF_BAND")
    if snapshot.created_at_ms:
        pair_age_hours = (now.timestamp() * 1000 - snapshot.created_at_ms) / 3_600_000
        if pair_age_hours < env.WHALE_ALPHA_MIN_PAIR_AGE_HOURS or pair_age_hours > env.WHALE_ALPHA_MAX_PAIR_AGE_DAYS * 24:
            rejects.append("PAIR_AGE_OUT_OF_BAND")
    else:
        rejects.append("PAIR_AGE_DATA_MISSING")
    candles, overview, trades, profile, holders, positions, top_traders, security, bq = await asyncio.gather(
        _fetch_ohlcv(client, env, candidate.snapshot.mint, now),
        _fetch_market_overview(client, env, candidate.snapshot.mint),
        _fetch_trade_data(client, env, candidate.snapshot.mint),
        _fetch_holder_profile(client, env, candidate.snapshot.mint),
        _fetch_top_holders(client, env, candidate.snapshot.mint),
        _fetch_risk_positions(client, env, candidate.snapshot.mint),
        _fetch_top_traders(client, env, candidate.snapshot.mint),
        _fetch_security(client, env, candidate.snapshot.mint),
        _fetch_bitquery_flow(client, env, candidate.snapshot.mint, snapshot.pair_address),
    )
    if not candles or int(now.timestamp()) - candles[-1].ts > STALE_SECONDS:
        rejects.append("OHLCV_STALE_OR_MISSING")
    pattern = detect_dip_consolidation_breakout(candles, int(now.timestamp()))
    if pattern is None: rejects.append("DIP_CONSOLIDATION_BREAKOUT_NOT_CONFIRMED")
    if not overview or not trades or not top_traders or not security or smart_rows is None:
        rejects.append("REQUIRED_PROVIDER_DATA_MISSING")
    last_trade = _num(_deep_find(overview or {}, ("lastTradeUnixTime", "last_trade_unix_time", "lastTradeTime")))
    if last_trade is not None and int(now.timestamp()) - int(last_trade) > STALE_SECONDS:
        rejects.append("MARKET_DATA_STALE")
    flow_raw = _flow_from_trade_data(trades)
    flow = _flow_evidence(flow_raw, candles, smart_rows or [], top_traders, candidate.snapshot.mint, bq)
    if flow is None: rejects.append("FLOW_DATA_MISSING_OR_CONTRADICTORY")
    if flow and (flow.buy_sell_ratio < env.WHALE_ALPHA_BUY_SELL_RATIO_MIN or not flow.net_buy_pressure): rejects.append("NET_BUY_PRESSURE_FAILED")
    on_raw = _holder_evidence(profile, holders, positions)
    security_flags = _security_flags(security)
    authority_flags = _authority_flags(await connection.get_account_info(Pubkey.from_string(candidate.snapshot.mint))) if connection is not None else ("AUTHORITY_DATA_MISSING",)
    if on_raw is None: rejects.append("HOLDER_DATA_MISSING")
    onchain = OnChainEvidence(*(on_raw or (0,0,0,0,False)), security_flags, authority_flags)
    if on_raw and (on_raw[0] > env.WHALE_ALPHA_MAX_TOP10_PCT or on_raw[1] > env.WHALE_ALPHA_MAX_SINGLE_WALLET_PCT or on_raw[2] > env.WHALE_ALPHA_MAX_DEV_HOLD_PCT or on_raw[3] > env.WHALE_ALPHA_MAX_TAGGED_RISK_PCT): rejects.append("CONCENTRATION_HARD_REJECT")
    if on_raw and not on_raw[4]: rejects.append("TAGGED_RISK_WALLETS_NET_DISTRIBUTING")
    rejects.extend(security_flags)
    rejects.extend(authority_flags)
    if flow and (flow.smart_money_status == "SUPPORTIVE" and flow.top_trader_status == "SUPPORTIVE"): smart_quality = 100
    elif flow and flow.smart_money_status == "SUPPORTIVE": smart_quality = 85
    elif flow and flow.top_trader_status == "SUPPORTIVE": smart_quality = 75
    else: smart_quality = 55
    liquidity_quality = 100.0 if 0.04 <= ratio <= 0.35 and liq > 0 else 0.0
    if liq < env.WHALE_ALPHA_MIN_LIQ_USD: liquidity_quality = 0
    overview_mc = _num(_deep_find(overview or {}, ("marketCap", "market_cap", "market_cap_usd")))
    overview_liq = _num(_deep_find(overview or {}, ("liquidity", "liquidity_usd")))
    if overview_mc and mcap and abs(overview_mc - mcap) / mcap > 0.30: rejects.append("MARKET_CAP_SOURCE_DISAGREEMENT")
    if overview_liq and liq and abs(overview_liq - liq) / liq > 0.30: rejects.append("LIQUIDITY_SOURCE_DISAGREEMENT")
    if snapshot.liquidity_usd and candidate.snapshot.liquidity_usd and abs(snapshot.liquidity_usd-candidate.snapshot.liquidity_usd)/snapshot.liquidity_usd > 0.35: rejects.append("LIQUIDITY_SOURCE_DISAGREEMENT")
    score = 0.0
    reasons: tuple[str, ...] = ()
    if pattern and flow and on_raw and not rejects:
        score, reasons = score_reversal(pattern, flow, onchain, not security_flags and not authority_flags, liquidity_quality, 100.0, smart_quality)
        if score < env.WHALE_ALPHA_MIN_CONFIDENCE: rejects.append("CONFIDENCE_BELOW_80")
    evidence = {
        "pattern": pattern.__dict__ if pattern else None,
        "flow": flow.__dict__ if flow else None,
        "onchain": onchain.__dict__,
        "market_cap_usd": mcap,
        "liquidity_usd": liq,
        "liquidity_mc_ratio": ratio,
        "data_fetched_at": now.isoformat(),
    }
    return ReversalAnalysis(candidate, snapshot, pattern, flow, onchain, score, _tier(score) if score >= 80 else "NO SIGNAL", tuple(dict.fromkeys(rejects)), reasons, "Breaks if price closes back inside the consolidation range, buy/sell ratio falls below 1.15, or security/liquidity/concentration hard filters fail.", evidence)


async def evaluate_candidates(client: httpx.AsyncClient, env: Env, candidates: list[DiscoveryCandidate], connection: Any | None, now: datetime) -> list[ReversalAnalysis]:
    smart_rows = await _fetch_smart_money_token_list(client, env)
    results: list[ReversalAnalysis] = []
    for candidate in candidates[: env.TOKEN_HUNTER_MAX_UNIQUE_PER_CYCLE]:
        try:
            results.append(await evaluate_candidate(client, env, candidate, connection, now, smart_rows))
        except Exception as exc:  # fail closed per token
            log.warning("reversal_candidate_failed", mint=candidate.snapshot.mint, error=str(exc))
            results.append(ReversalAnalysis(candidate, candidate.snapshot, None, None, None, 0, "NO SIGNAL", ("EVALUATION_ERROR",), (), "Invalid on any evaluation error.", {}))
    return results
