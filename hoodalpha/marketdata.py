"""
hoodalpha/marketdata.py

A minimal, standalone DexScreener price lookup -- used ONLY as a
fallback when a watched (contract, chain) has fallen out of
WhaleAlpha's active signal_tokens set (piggyback read in
hoodalpha/db.py::fetch_current_prices returns nothing for it) and
HOOD_FALLBACK_DEXSCREENER_ENABLED is true.

Deliberately NOT an import of providers/marketdata/dexscreener.py:
that module is WhaleAlpha's own shared HTTP/cache plumbing
(providers/cache.py's get_json, circuit breakers, etc.), and reusing it
would (a) reintroduce Python import coupling to WhaleAlpha's provider
layer and (b) share WhaleAlpha's request cache/circuit-breaker state
with HoodAlpha's own traffic, which the "completely separate and
independent" requirement rules out. This is a plain, direct aiohttp
call with its own short-lived in-memory cache.

DexScreener is a shared public rate-limited API either way -- see
hoodalpha/README.md's "Rate limits" section for why this path should
stay a fallback, not the primary price source.
"""

from __future__ import annotations

import logging
import time

import aiohttp

logger = logging.getLogger("HoodAlpha.MarketData")

_DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{contract}"

# contract -> (fetched_at_monotonic, parsed_pairs)
_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL_SECONDS = 20.0


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, "N/A", ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def get_price(contract: str, chain: str) -> float | None:
    """Best-effort current USD price for (contract, chain), or None if
    unavailable. Never raises -- callers treat None as "skip this cycle
    for this token", matching WhaleAlpha's own per-token error isolation
    pattern in signal_tracker.py::signal_lifecycle_loop."""
    now = time.monotonic()
    cached = _cache.get(contract)
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        pairs = cached[1]
    else:
        pairs = await _fetch_pairs(contract)
        _cache[contract] = (now, pairs)

    if not pairs:
        return None

    chain_pairs = [p for p in pairs if p.get("chainId") == chain]
    usable = chain_pairs or pairs
    if not usable:
        return None

    best = max(usable, key=lambda p: _to_float((p.get("liquidity") or {}).get("usd", 0)))
    price = _to_float(best.get("priceUsd"), default=0.0)
    return price if price > 0 else None


async def _fetch_pairs(contract: str) -> list[dict]:
    url = _DEXSCREENER_TOKENS_URL.format(contract=contract)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("DexScreener fallback fetch failed (%s) for %s", resp.status, contract)
                    return []
                data = await resp.json()
                return data.get("pairs") or []
    except Exception as e:
        logger.warning("DexScreener fallback fetch error for %s: %s", contract, e)
        return []
