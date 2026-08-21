"""Cheap token discovery for Whale Alpha's high-potential hunter."""

from __future__ import annotations

from typing import Any

import httpx

from whale_alpha.config import Env
from whale_alpha.utils.http_retry import TTLCache, get_provider_client
from whale_alpha.utils.logger import child_logger

log = child_logger("tokenHunterSources")
_IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
_cache: TTLCache[list[str]] = TTLCache(ttl_seconds=30, max_entries=32)


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


async def _fetch_mints(
    client: httpx.AsyncClient,
    env: Env,
    provider: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    limit: int,
) -> list[str]:
    result = await _provider(env, provider).get(client, url, params=params, **_retry(env))
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
    mints: list[str] = []
    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            continue
        mint = _mint(entry, "mint", "tokenAddress", "address", "baseMint", "id")
        if mint and mint not in mints:
            mints.append(mint)
    return mints


async def discover_token_mints(client: httpx.AsyncClient, env: Env) -> dict[str, list[str]]:
    sources: dict[str, list[str]] = {}

    async def cached(name: str, provider: str, url: str, params: dict[str, str] | None = None) -> None:
        key = f"{name}:mints"
        hit = _cache.get(key)
        if hit is not None:
            sources[name] = hit
            return
        mints = await _fetch_mints(
            client, env, provider, url, params=params, limit=env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE
        )
        _cache.set(key, mints)
        sources[name] = mints

    if env.DISCOVERY_PUMPFUN_ENABLED:
        await cached(
            "pumpfun",
            "pumpfun",
            f"{env.DISCOVERY_PUMPFUN_API_BASE}/coins",
            {
                "offset": "0",
                "limit": str(env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE),
                "sort": "created_timestamp",
                "order": "DESC",
            },
        )
    if env.DISCOVERY_LAUNCHLAB_ENABLED:
        await cached(
            "launchlab",
            "launchlab",
            f"{env.DISCOVERY_LAUNCHLAB_API_BASE}/list",
            {"sort": "new", "size": str(env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE)},
        )
    if env.DISCOVERY_RAYDIUM_ENABLED:
        await cached(
            "raydium",
            "raydium",
            f"{env.DISCOVERY_RAYDIUM_API_BASE}/pools/info/list",
            {
                "poolType": "all",
                "poolSortField": "default",
                "sortType": "desc",
                "pageSize": str(env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE),
                "page": "1",
            },
        )
    if env.DISCOVERY_METEORA_ENABLED:
        await cached(
            "meteora",
            "meteora",
            f"{env.DISCOVERY_METEORA_API_BASE}/pools",
            {"limit": str(env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE)},
        )
    if env.DISCOVERY_DEXSCREENER_ENABLED:
        await cached(
            "dexscreener", "dexscreener", f"{env.DISCOVERY_DEXSCREENER_API_BASE}/token-boosts/latest/v1"
        )
    return sources
