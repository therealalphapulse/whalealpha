"""Free-tier market-data sources for the Hybrid Discovery Engine — Phase 1
refactor, new module.

This is the piece that actually eliminates the discovery engine's cold-start
loop. Everything in integrations/wallet_discovery_source.py either requires
an existing Signal (`find_candidates_from_token_holders`, fed by already-
tracked whales) or an API key (`find_candidates_from_trending_tokens`,
Jupiter-only). Both of those remain in place and keep running — they're now
just two sources among many, not THE source — but neither can be relied on
alone to produce a first candidate from zero tracked wallets with zero paid
API keys.

Two families of functions live here, matching the architecture's priority
order:

  Priority 1 — Real-time on-chain launch discovery (`find_candidates_from_*`
  for pump.fun / LaunchLab / Raydium / Meteora): polls each platform's public,
  keyless "recent launches / new pools" endpoint, then reuses
  wallet_discovery_source.find_candidates_from_token_holders (plain Solana
  RPC, no key) to resolve each freshly-launched mint's largest holders —
  i.e. its earliest/largest accumulators. This needs neither a tracked
  wallet, a Signal, nor any API key, so it works from absolute zero.

  Priority 2 — Trending-token provider fallback chain
  (`find_trending_tokens_multi_provider`): Jupiter Tokens API V2 first (via
  the existing wallet_discovery_source.find_candidates_from_trending_tokens,
  unchanged), then Birdeye's free tier, then DexScreener, in that order —
  stopping at the first provider that returns candidates. One provider being
  down, rate-limited, or missing a key never stops discovery; it just falls
  through to the next.

ASSUMPTION (flagged, same convention as wallet_discovery_source.py): the
exact endpoint paths/response shapes below (pump.fun's `frontend-api`,
Raydium's `api-v3`, Meteora's AMM API, Birdeye's free `defi/token_trending`
endpoint, DexScreener's public token-profiles/pairs API) are best-effort as
of this port and are NOT guaranteed to be stable, versioned, or officially
documented — several are informal public APIs that can change shape without
notice. Every parser below is defensive (tries several plausible key names,
returns [] rather than raising on an unexpected shape) for exactly that
reason. Verify against a live response before depending on any one of them in
production, and swap the parsing helper for a different provider's shape if
you point these settings at something else — everything downstream
(DiscoveredCandidate, engines/discovery.py) is provider-agnostic.

Nothing here invents data: every function performs a real HTTP request to a
real public endpoint, or returns [] if that request fails/is disabled. No
mock launches, no fabricated holders.
"""

from __future__ import annotations

from typing import Any

import httpx
from solana.rpc.async_api import AsyncClient

from whale_alpha.config import Env
from whale_alpha.integrations.wallet_discovery_source import (
    DiscoveredCandidate,
    find_candidates_from_token_holders,
)
from whale_alpha.utils.http_retry import TTLCache, get_provider_client
from whale_alpha.utils.logger import child_logger

log = child_logger("freeMarketSources")

# Every free-tier discovery-source provider below shares the same
# resilience layer (semaphore, retry/backoff, circuit breaker, metrics —
# see utils/http_retry.py) via one named ProviderClient each, plus its own
# short-TTL positive/negative mint-list cache so re-polling the same
# "latest launches" endpoint every DISCOVERY_INTERVAL_SECONDS doesn't
# refetch identical results mid-cycle (e.g. pump.fun + Raydium both getting
# checked from the same run_discovery_cycle pass).
_mint_list_cache: TTLCache[list[str]] = TTLCache(ttl_seconds=60, max_entries=64)


def _provider(env: Env, name: str):
    return get_provider_client(
        name,
        max_concurrency=env.DISCOVERY_PROVIDER_MAX_CONCURRENCY,
        failure_threshold=env.DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds=env.DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
    )


def _provider_retry_kwargs(env: Env) -> dict[str, float | int]:
    return {
        "max_retries": env.DISCOVERY_PROVIDER_MAX_RETRIES,
        "base_backoff_seconds": env.DISCOVERY_PROVIDER_RETRY_BASE_SECONDS,
        "max_backoff_seconds": env.DISCOVERY_PROVIDER_RETRY_MAX_SECONDS,
    }

# Common wrapped/stable mints that show up as one side of nearly every pool
# and are never themselves an interesting "candidate token" to source
# holders from.
_IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def _extract_mint(entry: dict[str, Any], *keys: str) -> str | None:
    """Tries each key in order, returning the first non-empty string value.
    Different free-tier providers name the same concept differently
    (`mint`, `baseMint`, `tokenAddress`, `id`, ...); this keeps every parser
    below tolerant of that instead of hard-coding one provider's field name.
    """
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value and value not in _IGNORED_MINTS:
            return value
    return None


async def _candidates_for_mints(
    connection: AsyncClient,
    env: Env,
    mints: list[str],
    *,
    source: str,
    max_holders_per_token: int,
) -> list[DiscoveredCandidate]:
    """Shared tail end for every on-chain launch source: resolve each freshly
    discovered mint's largest holders via plain RPC (no API key), tagged
    with the given source name for logging/attribution.
    """
    candidates: list[DiscoveredCandidate] = []
    for mint in mints:
        holders = await find_candidates_from_token_holders(
            connection,
            mint,
            max_holders_per_token,
            min_interval_seconds=env.DISCOVERY_RPC_MIN_INTERVAL_SECONDS,
            max_retries=env.DISCOVERY_RPC_MAX_RETRIES,
        )
        candidates.extend(
            DiscoveredCandidate(address=h.address, source=source, discovered_from_token_mint=mint)
            for h in holders
        )
    return candidates


# --------------------------------------------------------------------------
# Priority 1 — Real-time on-chain launch discovery
# --------------------------------------------------------------------------


async def find_candidates_from_pumpfun_launches(
    client: httpx.AsyncClient,
    connection: AsyncClient,
    env: Env,
    *,
    max_launches: int,
    max_holders_per_token: int,
) -> list[DiscoveredCandidate]:
    """Pump.fun's public, keyless "latest coins" listing — no tracked
    wallets or Signals required. Returns [] (logged) if disabled, the
    request fails, or the response shape doesn't match what's expected —
    never raises, so one dead provider never stops the discovery cycle.
    """
    if not env.DISCOVERY_PUMPFUN_ENABLED:
        return []

    cached = _mint_list_cache.get("pumpfun_launches")
    if cached is not None:
        return await _candidates_for_mints(
            connection, env, cached, source="pumpfun_launch", max_holders_per_token=max_holders_per_token
        )

    url = f"{env.DISCOVERY_PUMPFUN_API_BASE}/coins"
    result = await _provider(env, "pumpfun").get(
        client,
        url,
        params={"offset": "0", "limit": str(max_launches), "sort": "created_timestamp", "order": "DESC"},
        **_provider_retry_kwargs(env),
    )
    if result.response is None or result.response.status_code >= 400:
        log.warning(
            "Pump.fun launches request failed",
            status=result.response.status_code if result.response else None,
            transient=result.transient,
            circuit_open=result.circuit_open,
        )
        return []
    try:
        payload = result.response.json()
    except Exception as err:  # noqa: BLE001 — a provider hiccup shouldn't stop the discovery cycle
        log.warning("Pump.fun launches response unparseable", err=str(err))
        return []

    entries = payload if isinstance(payload, list) else payload.get("coins") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []

    mints: list[str] = []
    for entry in entries[:max_launches]:
        if not isinstance(entry, dict):
            continue
        mint = _extract_mint(entry, "mint", "tokenMint", "address")
        if mint:
            mints.append(mint)

    _mint_list_cache.set("pumpfun_launches", mints)
    return await _candidates_for_mints(
        connection, env, mints, source="pumpfun_launch", max_holders_per_token=max_holders_per_token
    )


async def find_candidates_from_launchlab_launches(
    client: httpx.AsyncClient,
    connection: AsyncClient,
    env: Env,
    *,
    max_launches: int,
    max_holders_per_token: int,
) -> list[DiscoveredCandidate]:
    """Raydium LaunchLab's public launch listing — same cold-start-breaking
    role as pump.fun above, for tokens launching via LaunchLab instead.
    """
    if not env.DISCOVERY_LAUNCHLAB_ENABLED:
        return []

    cached = _mint_list_cache.get("launchlab_launches")
    if cached is not None:
        return await _candidates_for_mints(
            connection, env, cached, source="launchlab_launch", max_holders_per_token=max_holders_per_token
        )

    url = f"{env.DISCOVERY_LAUNCHLAB_API_BASE}/list"
    result = await _provider(env, "launchlab").get(
        client,
        url,
        params={"sort": "new", "size": str(max_launches)},
        **_provider_retry_kwargs(env),
    )
    if result.response is None or result.response.status_code >= 400:
        log.warning(
            "LaunchLab launches request failed",
            status=result.response.status_code if result.response else None,
            transient=result.transient,
            circuit_open=result.circuit_open,
        )
        return []
    try:
        payload = result.response.json()
    except Exception as err:  # noqa: BLE001
        log.warning("LaunchLab launches response unparseable", err=str(err))
        return []

    entries = _unwrap_list(payload, ("data", "list", "items"))
    if entries is None:
        return []

    mints: list[str] = []
    for entry in entries[:max_launches]:
        if not isinstance(entry, dict):
            continue
        mint = _extract_mint(entry, "mint", "mintA", "tokenMint", "id")
        if mint:
            mints.append(mint)

    _mint_list_cache.set("launchlab_launches", mints)
    return await _candidates_for_mints(
        connection, env, mints, source="launchlab_launch", max_holders_per_token=max_holders_per_token
    )


async def find_candidates_from_raydium_new_pools(
    client: httpx.AsyncClient,
    connection: AsyncClient,
    env: Env,
    *,
    max_launches: int,
    max_holders_per_token: int,
) -> list[DiscoveredCandidate]:
    """Raydium's public pool list, sorted by creation time — fresh liquidity
    events (Priority 1) independent of LaunchLab specifically.
    """
    if not env.DISCOVERY_RAYDIUM_ENABLED:
        return []

    cached = _mint_list_cache.get("raydium_pools")
    if cached is not None:
        return await _candidates_for_mints(
            connection, env, cached, source="raydium_new_pool", max_holders_per_token=max_holders_per_token
        )

    url = f"{env.DISCOVERY_RAYDIUM_API_BASE}/pools/info/list"
    result = await _provider(env, "raydium").get(
        client,
        url,
        params={
            "poolType": "all",
            "poolSortField": "default",
            "sortType": "desc",
            "pageSize": str(max_launches),
            "page": "1",
        },
        **_provider_retry_kwargs(env),
    )
    if result.response is None or result.response.status_code >= 400:
        log.warning(
            "Raydium new pools request failed",
            status=result.response.status_code if result.response else None,
            transient=result.transient,
            circuit_open=result.circuit_open,
        )
        return []
    try:
        payload = result.response.json()
    except Exception as err:  # noqa: BLE001
        log.warning("Raydium new pools response unparseable", err=str(err))
        return []

    entries = _unwrap_list(payload, ("data", "list", "items"))
    if entries is None:
        return []

    mints: list[str] = []
    for entry in entries[:max_launches]:
        if not isinstance(entry, dict):
            continue
        # Pools have two mints (base/quote); take whichever isn't a
        # common wrapped/stable mint — that's the actual token of interest.
        mint_a = entry.get("mintA") if isinstance(entry.get("mintA"), str) else _dig(entry, "mintA", "address")
        mint_b = entry.get("mintB") if isinstance(entry.get("mintB"), str) else _dig(entry, "mintB", "address")
        for candidate_mint in (mint_a, mint_b):
            if isinstance(candidate_mint, str) and candidate_mint and candidate_mint not in _IGNORED_MINTS:
                mints.append(candidate_mint)
                break

    _mint_list_cache.set("raydium_pools", mints)
    return await _candidates_for_mints(
        connection, env, mints, source="raydium_new_pool", max_holders_per_token=max_holders_per_token
    )


async def find_candidates_from_meteora_pools(
    client: httpx.AsyncClient,
    connection: AsyncClient,
    env: Env,
    *,
    max_launches: int,
    max_holders_per_token: int,
) -> list[DiscoveredCandidate]:
    """Meteora's public AMM pool listing — same "fresh liquidity event" role
    as the Raydium source, for tokens whose primary liquidity launched on
    Meteora instead.
    """
    if not env.DISCOVERY_METEORA_ENABLED:
        return []

    cached = _mint_list_cache.get("meteora_pools")
    if cached is not None:
        return await _candidates_for_mints(
            connection, env, cached, source="meteora_new_pool", max_holders_per_token=max_holders_per_token
        )

    url = f"{env.DISCOVERY_METEORA_API_BASE}/pools"
    result = await _provider(env, "meteora").get(
        client, url, params={"limit": str(max_launches)}, **_provider_retry_kwargs(env)
    )
    if result.response is None or result.response.status_code >= 400:
        log.warning(
            "Meteora pools request failed",
            status=result.response.status_code if result.response else None,
            transient=result.transient,
            circuit_open=result.circuit_open,
        )
        return []
    try:
        payload = result.response.json()
    except Exception as err:  # noqa: BLE001
        log.warning("Meteora pools response unparseable", err=str(err))
        return []

    entries = _unwrap_list(payload, ("data", "pools", "items"))
    if entries is None:
        return []

    mints: list[str] = []
    for entry in entries[:max_launches]:
        if not isinstance(entry, dict):
            continue
        pool_mints = entry.get("pool_token_mints") or entry.get("poolTokenMints")
        if isinstance(pool_mints, list):
            for m in pool_mints:
                if isinstance(m, str) and m not in _IGNORED_MINTS:
                    mints.append(m)
                    break
        else:
            mint = _extract_mint(entry, "mint", "tokenMint", "address")
            if mint:
                mints.append(mint)

    _mint_list_cache.set("meteora_pools", mints)
    return await _candidates_for_mints(
        connection, env, mints, source="meteora_new_pool", max_holders_per_token=max_holders_per_token
    )


def _unwrap_list(payload: Any, keys: tuple[str, ...]) -> list[Any] | None:
    """Free-tier APIs vary on whether the array is the whole response body
    or nested under a wrapper key (`data`, `list`, `items`, ...) — this
    tries the plain-list case first, then each nested key in turn."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _unwrap_list(value, keys)
            if nested is not None:
                return nested
    return None


def _dig(entry: dict[str, Any], *path: str) -> Any:
    current: Any = entry
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


# --------------------------------------------------------------------------
# Priority 2 — Trending-token provider fallback chain
# --------------------------------------------------------------------------


async def find_trending_mints_from_birdeye(client: httpx.AsyncClient, env: Env, *, max_tokens: int) -> list[str]:
    """Birdeye's free-tier trending-tokens endpoint. `BIRDEYE_API_KEY` is
    optional — Birdeye's free tier allows a limited number of unauthenticated
    requests; set the key once you have one to raise that ceiling. Returns []
    or errors are treated identically by the caller (fall through to the
    next provider), so a missing key just means "try Birdeye without one".
    """
    if not env.DISCOVERY_BIRDEYE_ENABLED:
        return []

    cached = _mint_list_cache.get("birdeye_trending")
    if cached is not None:
        return cached

    url = f"{env.DISCOVERY_BIRDEYE_API_BASE}/defi/token_trending"
    headers = {"x-chain": "solana"}
    if env.BIRDEYE_API_KEY:
        headers["X-API-KEY"] = env.BIRDEYE_API_KEY

    result = await _provider(env, "birdeye").get(
        client,
        url,
        params={"sort_by": "volume24hUSD", "sort_type": "desc", "limit": str(max_tokens)},
        headers=headers,
        **_provider_retry_kwargs(env),
    )
    if result.response is None or result.response.status_code >= 400:
        log.info(
            "Birdeye trending request failed, will fall through",
            status=result.response.status_code if result.response else None,
            transient=result.transient,
            circuit_open=result.circuit_open,
        )
        return []
    try:
        payload = result.response.json()
    except Exception as err:  # noqa: BLE001
        log.info("Birdeye trending response unparseable, will fall through", err=str(err))
        return []

    entries = _unwrap_list(payload, ("data", "tokens", "items"))
    if entries is None:
        return []

    mints: list[str] = []
    for entry in entries[:max_tokens]:
        if isinstance(entry, dict):
            mint = _extract_mint(entry, "address", "mint")
            if mint:
                mints.append(mint)
    _mint_list_cache.set("birdeye_trending", mints)
    return mints


async def find_trending_mints_from_dexscreener(client: httpx.AsyncClient, env: Env, *, max_tokens: int) -> list[str]:
    """DexScreener's public, fully keyless token-boosts/latest-pairs feed —
    the last-resort free provider in the fallback chain, always available
    regardless of any API key configuration.
    """
    if not env.DISCOVERY_DEXSCREENER_ENABLED:
        return []

    cached = _mint_list_cache.get("dexscreener_trending")
    if cached is not None:
        return cached

    url = f"{env.DISCOVERY_DEXSCREENER_API_BASE}/token-boosts/latest/v1"
    result = await _provider(env, "dexscreener").get(client, url, **_provider_retry_kwargs(env))
    if result.response is None or result.response.status_code >= 400:
        log.info(
            "DexScreener trending request failed, will fall through",
            status=result.response.status_code if result.response else None,
            transient=result.transient,
            circuit_open=result.circuit_open,
        )
        return []
    try:
        payload = result.response.json()
    except Exception as err:  # noqa: BLE001
        log.info("DexScreener trending response unparseable, will fall through", err=str(err))
        return []

    entries = _unwrap_list(payload, ("data", "pairs", "items"))
    if entries is None:
        return []

    mints: list[str] = []
    for entry in entries[:max_tokens]:
        if not isinstance(entry, dict):
            continue
        if entry.get("chainId") not in (None, "solana"):
            continue
        mint = _extract_mint(entry, "tokenAddress", "address", "mint")
        if mint:
            mints.append(mint)
    _mint_list_cache.set("dexscreener_trending", mints)
    return mints


async def find_trending_tokens_multi_provider(
    client: httpx.AsyncClient,
    connection: AsyncClient,
    env: Env,
    *,
    max_tokens: int,
    max_holders_per_token: int,
    jupiter_mints: list[str] | None = None,
) -> list[DiscoveredCandidate]:
    """Priority 2's fallback chain: Jupiter (already resolved by the caller
    via wallet_discovery_source.find_candidates_from_trending_tokens and
    passed in as `jupiter_mints`, so this module doesn't duplicate that
    request) -> Birdeye free tier -> DexScreener. Stops at the first
    provider that yields at least one mint; never combines partial results
    across providers, so callers get one clean, deduplicated source per
    cycle. Returns [] only if every provider in the chain is disabled,
    unreachable, or empty.
    """
    provider_name = None
    mints: list[str] = jupiter_mints or []
    if mints:
        provider_name = "jupiter_trending"
    else:
        mints = await find_trending_mints_from_birdeye(client, env, max_tokens=max_tokens)
        if mints:
            provider_name = "birdeye_trending"
        else:
            mints = await find_trending_mints_from_dexscreener(client, env, max_tokens=max_tokens)
            if mints:
                provider_name = "dexscreener_trending"

    if not mints or provider_name is None:
        log.info("Trending-token fallback chain exhausted with no results this cycle")
        return []

    return await _candidates_for_mints(
        connection, env, mints, source=provider_name, max_holders_per_token=max_holders_per_token
    )


# --------------------------------------------------------------------------
# Priority 8 — Social / KOL influence (enrichment only, never a hard source)
# --------------------------------------------------------------------------


async def token_has_social_signal(client: httpx.AsyncClient, env: Env, token_mint: str) -> bool:
    """Best-effort, free-tier-only check for whether a token has an
    established social presence (website/Twitter/Telegram listed on
    DexScreener's public pair metadata, or a pump.fun description/socials
    block). Used only to enrich confidence and the "KOL" label
    (engines/wallet_labels.py) — never depended on for anything else, and a
    False here never blocks a candidate, it only skips one optional label.
    """
    if not env.DISCOVERY_DEXSCREENER_ENABLED:
        return False

    url = f"{env.DISCOVERY_DEXSCREENER_API_BASE}/latest/dex/tokens/{token_mint}"
    result = await _provider(env, "dexscreener").get(client, url, **_provider_retry_kwargs(env))
    if result.response is None or result.response.status_code >= 400:
        return False
    try:
        payload = result.response.json()
    except Exception:  # noqa: BLE001 — enrichment only, never worth surfacing an error for
        return False

    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    if not isinstance(pairs, list):
        return False

    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        info = pair.get("info")
        if isinstance(info, dict) and (info.get("socials") or info.get("websites")):
            return True
    return False
