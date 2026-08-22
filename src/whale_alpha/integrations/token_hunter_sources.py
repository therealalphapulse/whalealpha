"""Cheap token discovery for Whale Alpha's high-potential hunter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncio

import httpx
from whale_alpha.config import Env
from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot
from whale_alpha.integrations.token_age import parse_timestamp_ms
from whale_alpha.utils.logger import child_logger

from whale_alpha.utils.http_retry import TTLCache, get_provider_client

log = child_logger("tokenHunterSources")
_IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}


@dataclass(frozen=True)
class DiscoveryCandidate:
    snapshot: TokenMarketSnapshot
    source: str


_cache: TTLCache[list[DiscoveryCandidate]] = TTLCache(ttl_seconds=30, max_entries=32)


def _provider(env: Env, name: str) -> Any:
    return get_provider_client(
        name,
        max_concurrency=env.TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY,
        failure_threshold=env.DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds=env.DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
    )


def _retry(env: Env) -> dict[str, float | int]:
    return {
        "max_retries": env.DISCOVERY_PROVIDER_MAX_RETRIES,
        "base_backoff_seconds": env.DISCOVERY_PROVIDER_RETRY_BASE_SECONDS,
        "max_backoff_seconds": env.DISCOVERY_PROVIDER_RETRY_MAX_SECONDS,
    }


def _list(payload: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _list(value, keys)
            if nested:
                return nested
    return []


def _mint(entry: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value and value not in _IGNORED_MINTS:
            return value
        if isinstance(value, dict):
            address = value.get("address")
            if isinstance(address, str) and address and address not in _IGNORED_MINTS:
                return address
    pool_mints = entry.get("pool_token_mints") or entry.get("poolTokenMints")
    if isinstance(pool_mints, list):
        for value in pool_mints:
            if isinstance(value, str) and value and value not in _IGNORED_MINTS:
                return value
    return None


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _created_at_ms(entry: dict[str, Any]) -> int | None:
    for key in ("pairCreatedAt", "createdAt", "created_at", "created_timestamp"):
        parsed = parse_timestamp_ms(entry.get(key))
        if parsed is not None:
            return parsed
    return None


def _candidate(entry: dict[str, Any], source: str, mint: str) -> DiscoveryCandidate:
    base = _dict(entry.get("baseToken"))
    liquidity = _dict(entry.get("liquidity"))
    volume = _dict(entry.get("volume"))
    txns = _dict(entry.get("txns"))
    m5 = _dict(txns.get("m5"))
    h1 = _dict(txns.get("h1"))
    market_cap = _number(entry.get("marketCap"), _number(entry.get("market_cap")))
    if market_cap is None:
        market_cap = _number(entry.get("usd_market_cap"), _number(entry.get("fdv")))
    log.info(
        "MARKET CAP FIELD DEBUG",
        provider=source,
        mint=mint,
        resolved_market_cap_usd=market_cap,
        raw_marketCap=entry.get("marketCap"),
        raw_market_cap=entry.get("market_cap"),
        raw_usd_market_cap=entry.get("usd_market_cap"),
        raw_fdv=entry.get("fdv"),
    )
    liquidity_usd = _number(liquidity.get("usd"), _number(entry.get("liquidityUsd")))
    volume_5m = (
        _number(volume.get("m5"), _number(entry.get("volume_5m"), _number(entry.get("volume5m"), 0.0))) or 0.0
    )
    volume_1h = (
        _number(volume.get("h1"), _number(entry.get("volume_1h"), _number(entry.get("volume1h"), 0.0))) or 0.0
    )
    buys_5m = _int(m5.get("buys") or entry.get("buys_5m"))
    sells_5m = _int(m5.get("sells") or entry.get("sells_5m"))
    buys_1h = _int(h1.get("buys") or entry.get("buys_1h"))
    sells_1h = _int(h1.get("sells") or entry.get("sells_1h"))
    name = base.get("name") or entry.get("name")
    symbol = base.get("symbol") or entry.get("symbol")
    snapshot = TokenMarketSnapshot(
        mint=mint,
        name=name if isinstance(name, str) else None,
        symbol=symbol if isinstance(symbol, str) else None,
        pair_address=entry.get("pairAddress") if isinstance(entry.get("pairAddress"), str) else None,
        dex_id=entry.get("dexId") if isinstance(entry.get("dexId"), str) else source,
        created_at_ms=_created_at_ms(entry),
        price_usd=_number(entry.get("priceUsd")),
        market_cap_usd=market_cap,
        liquidity_usd=liquidity_usd,
        volume_5m_usd=volume_5m,
        volume_1h_usd=volume_1h,
        buys_5m=buys_5m,
        sells_5m=sells_5m,
        buys_1h=buys_1h,
        sells_1h=sells_1h,
        price_change_5m_pct=_number(_dict(entry.get("priceChange")).get("m5"), 0.0) or 0.0,
        price_change_1h_pct=_number(_dict(entry.get("priceChange")).get("h1"), 0.0) or 0.0,
        metadata_present=bool(name or symbol or entry.get("metadata") or entry.get("uri")),
        source=source,
    )
    return DiscoveryCandidate(snapshot=snapshot, source=source)


async def _fetch_candidates(
    client: httpx.AsyncClient,
    env: Env,
    provider: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    limit: int,
) -> list[DiscoveryCandidate]:
    result = await _provider(env, provider).get(client, url, params=params, headers=headers, **_retry(env))
    if result.response is None or result.response.status_code >= 400:
        log.warning(
            "Token discovery provider failed",
            provider=provider,
            status=result.response.status_code if result.response else None,
        )
        return []
    try:
        payload = result.response.json()
    except ValueError as err:
        log.warning("Token discovery provider returned invalid JSON", provider=provider, err=str(err))
        return []
    entries = _list(payload, ("coins", "data", "list", "items", "pairs"))
    candidates: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            continue
        mint = _mint(entry, "mint", "tokenAddress", "address", "baseMint", "id")
        if mint and mint not in seen:
            seen.add(mint)
            candidates.append(_candidate(entry, provider, mint))
    return candidates


async def discover_token_candidates(
    client: httpx.AsyncClient, env: Env
) -> dict[str, list[DiscoveryCandidate]]:
    sources: dict[str, list[DiscoveryCandidate]] = {}

    async def cached(
        name: str,
        provider: str,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        key = f"{name}:candidates"
        hit = _cache.get(key)
        if hit is not None:
            sources[name] = hit
            return
        try:
            candidates = await _fetch_candidates(
                client, env, provider, url, params=params, headers=headers, limit=env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE
            )
        except Exception as err:  # noqa: BLE001 — one provider must never stop discovery
            log.warning("Token discovery provider isolated failure", provider=provider, err=str(err))
            candidates = []
        _cache.set(key, candidates)
        sources[name] = candidates

    tasks = []
    if env.DISCOVERY_PUMPFUN_ENABLED:
        tasks.append(cached(
            "pumpfun", "pumpfun", f"{env.DISCOVERY_PUMPFUN_API_BASE}/coins",
            {"offset": "0", "limit": str(env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE), "sort": "created_timestamp", "order": "DESC"},
            {"Authorization": f"Bearer {env.PUMPFUN_API_TOKEN}"} if env.PUMPFUN_API_TOKEN else None,
        ))
    if env.DISCOVERY_LAUNCHLAB_ENABLED:
        tasks.append(cached(
            "launchlab", "launchlab", f"{env.DISCOVERY_LAUNCHLAB_API_BASE}/get/list",
            {"sort": "new"},
        ))
    if env.DISCOVERY_RAYDIUM_ENABLED:
        tasks.append(cached(
            "raydium", "raydium", f"{env.DISCOVERY_RAYDIUM_API_BASE}/pools/info/list",
            {"poolType": "all", "poolSortField": "default", "sortType": "desc", "pageSize": str(env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE), "page": "1"},
        ))
    if env.DISCOVERY_METEORA_ENABLED:
        tasks.append(cached(
            "meteora", "meteora", f"{env.DISCOVERY_METEORA_API_BASE}/pools",
            {"page": "1", "page_size": str(env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE)},
        ))
    if env.DISCOVERY_DEXSCREENER_ENABLED:
        tasks.append(cached(
            "dexscreener", "dexscreener", f"{env.DISCOVERY_DEXSCREENER_API_BASE}/token-boosts/latest/v1"
        ))
    if tasks:
        await asyncio.gather(*tasks)
    return sources


async def discover_token_mints(client: httpx.AsyncClient, env: Env) -> dict[str, list[str]]:
    candidates = await discover_token_candidates(client, env)
    return {
        source: [candidate.snapshot.mint for candidate in values] for source, values in candidates.items()
    }
