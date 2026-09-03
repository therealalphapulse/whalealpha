"""
providers/marketdata/_resilience.py

NEW in v4. This is the fix for the single most consequential finding in
the provider-layer audit (§4): DexScreener, GeckoTerminal, CoinGecko, and
GoPlus each opened a fresh `aiohttp.ClientSession()` per call, had no
shared cache, no retry, and (in three of four DexScreener functions) no
timeout at all.

This module does NOT reimplement `multi_rpc_manager`'s queue/circuit-
breaker machinery — that stays exactly as built, per the Bible's
non-negotiable preservation list, and is scoped to RPC traffic behind the
Provider Gateway. This is the lighter-weight fix appropriate for the
market-data family: one shared, reused `aiohttp.ClientSession` per
process, a TTL cache via `providers.cache.get_cache()` (Redis-backed in
production, in-memory in dev/this sandbox), bounded retry with backoff,
and an enforced timeout on every call. Each provider adapter's own
field-mapping logic (untouched, verified correct in the audit) calls
through this helper instead of rolling its own transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

import aiohttp

from providers.cache import get_cache
from providers.marketdata import _provider_circuit_breaker as _breaker

logger = logging.getLogger("AlphaPulse.ProviderResilience")

_session: aiohttp.ClientSession | None = None
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def _get_session() -> aiohttp.ClientSession:
    """One shared, reused session per process instead of one per call —
    closes the "no connection pooling" gap the audit found across every
    market-data adapter."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT)
    return _session


def _cache_key(url: str) -> str:
    return "marketdata:" + hashlib.sha256(url.encode()).hexdigest()[:24]


async def get_json(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    cache_ttl_seconds: int = 20,
    max_retries: int = 2,
    timeout_seconds: float = 10.0,
    provider_name: str | None = None,
) -> dict | list | None:
    """
    Cached, retried, timeout-bounded GET-JSON. Returns None on any
    failure after retries are exhausted — preserves the codebase's
    existing, deliberate "None = unknown, not zero" convention
    (documented in multi_rpc_manager and confirmed sound in the audit),
    so callers do not need to change their None-handling logic.

    provider_name (optional, default None): opts this call into the
    lightweight per-provider circuit breaker in
    providers.marketdata._provider_circuit_breaker — see that module's
    docstring for the full design (AlphaPulse Provider Resilience task,
    2026-08-28). When omitted (the default), behavior is byte-for-byte
    identical to before this parameter existed: every other caller of
    get_json (coingecko, dexscreener, geckoterminal, goplus, rugcheck) is
    completely unaffected. When given, a persistently unhealthy provider
    stops being called (this function returns None immediately, without
    touching the network) until its cooldown elapses, and a 401/402/403
    is correctly recorded as a provider-health failure instead of being
    silently treated the same as "no data for this query".
    """
    cache = await get_cache()
    cache_key_source = url + repr(sorted((params or {}).items()))
    key = _cache_key(cache_key_source)

    cached = await cache.get(key)
    if cached is not None:
        return cached

    if provider_name is not None and not _breaker.allow_request(provider_name):
        logger.info(
            "Market-data fetch skipped — %s circuit breaker open: %s",
            provider_name,
            url,
        )
        return None

    session = await _get_session()
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 2):  # e.g. max_retries=2 -> 3 attempts total
        try:
            async with session.get(url, params=params, headers=headers, timeout=timeout) as resp:
                if resp.status == 429:
                    # Rate-limited: honor a short backoff before retrying,
                    # do not hammer the free-tier endpoint further.
                    await asyncio.sleep(0.5 * attempt)
                    continue
                if resp.status in (401, 402, 403):
                    # Auth failure / plan restriction / out of credits —
                    # will not clear up by itself, so this is the fast-trip
                    # failure class (see _provider_circuit_breaker docstring),
                    # not a "no data" result to be confused with a genuine
                    # empty/negative provider response.
                    if provider_name is not None:
                        _breaker.record_failure(provider_name, _breaker.FAILURE_AUTH_OR_CREDITS)
                    return None
                if resp.status != 200:
                    if provider_name is not None:
                        _breaker.record_failure(provider_name, _breaker.FAILURE_TRANSIENT)
                    return None
                data = await resp.json()
                await cache.set(key, data, cache_ttl_seconds)
                if provider_name is not None:
                    _breaker.record_success(provider_name)
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = exc
            if attempt <= max_retries:
                await asyncio.sleep(0.25 * attempt)
                continue
            logger.warning("Market-data fetch failed after retries: %s (%s)", url, exc)
            if provider_name is not None:
                _breaker.record_failure(provider_name, _breaker.FAILURE_TRANSIENT)
            return None

    # Loop exhausted without an explicit return above — either every
    # attempt hit an exception (last_error set, already logged/recorded in
    # the except branch on the final attempt) or every attempt was 429'd
    # (last_error is None; record that as a transient failure here so a
    # provider that never responds with anything but 429 still trips the
    # breaker instead of being retried forever with no health signal).
    if last_error:
        logger.warning("Market-data fetch exhausted retries: %s (%s)", url, last_error)
    elif provider_name is not None:
        _breaker.record_failure(provider_name, _breaker.FAILURE_TRANSIENT)
    return None
