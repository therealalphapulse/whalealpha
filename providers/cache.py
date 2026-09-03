"""
providers/cache.py

NEW in v4 (Architecture Bible §5 — State Management Strategy).

The audit confirmed the market-data provider layer (DexScreener,
GeckoTerminal, CoinGecko) has zero caching today — every call hits the
upstream API fresh. It also confirmed `multi_rpc_manager`'s existing TTL
cache is a good design that is simply trapped in one process's memory,
which is fine for a single instance but breaks the moment there is more
than one replica (each replica would cache independently, defeating a
shared rate ceiling).

`Cache` is a small abstraction with two implementations:

- `InMemoryCache` — process-local, used automatically when REDIS_URL is not
  configured. This is what keeps the codebase runnable today, in this
  sandbox, and in any single-instance/dev deployment, without requiring a
  live Redis server.
- `RedisCache` — the production, multi-instance-safe backend described in
  the Bible. Only imports `redis.asyncio` when actually constructed, so a
  machine without the `redis` package installed can still import this
  module and run in single-instance mode.

`get_cache()` is the one place that decides which backend to use, based on
`REDIS_URL` in settings — callers never branch on this themselves.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol


class Cache(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl_seconds: int) -> None: ...
    async def delete(self, key: str) -> None: ...


class InMemoryCache:
    """Process-local TTL cache. Same semantics as the cache already proven
    inside multi_rpc_manager — this is a small, generic extraction of that
    pattern so market-data providers can use it too, without duplicating
    the logic a third time.

    Entries only get cleaned up when something happens to GET that exact
    key again after expiry — a key that's written once and never re-read
    (e.g. a token scanned once and never revisited) would otherwise sit in
    memory for the life of the process. This matters here specifically
    because this is also the fallback path for any deployment without
    REDIS_URL configured, including long-running single-instance setups.
    _MAX_ENTRIES is a hard backstop against unbounded growth; a sweep of
    already-expired entries runs opportunistically on write so the common
    case (an expired entry being replaced by a new TTL window) never even
    needs the hard cap."""

    _MAX_ENTRIES = 20_000

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    async def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if len(self._store) >= self._MAX_ENTRIES:
            self._evict(target_size=self._MAX_ENTRIES // 2)
        self._store[key] = (time.monotonic() + ttl_seconds, value)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def _evict(self, target_size: int) -> None:
        now = time.monotonic()
        # Pass 1: drop anything already expired — the cheap, common case.
        expired = [k for k, (expires_at, _) in self._store.items() if now >= expires_at]
        for k in expired:
            self._store.pop(k, None)
        # Pass 2 (rare): still over target after clearing dead weight —
        # evict the entries closest to expiry first.
        if len(self._store) > target_size:
            by_expiry = sorted(self._store.items(), key=lambda item: item[1][0])
            for k, _ in by_expiry[: len(self._store) - target_size]:
                self._store.pop(k, None)


class RedisCache:
    """Multi-instance-safe cache backend. Requires `redis` (redis-asyncio)
    to be installed and a reachable REDIS_URL — neither is guaranteed in
    every environment, so this class is only imported/constructed lazily
    by `get_cache()`, never at module import time."""

    def __init__(self, redis_url: str, namespace: str = "ap") -> None:
        import redis.asyncio as redis  # local import: optional dependency

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._namespace = namespace

    def _key(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    async def get(self, key: str) -> Any | None:
        import json

        raw = await self._redis.get(self._key(key))
        if raw is None:
            return None
        return json.loads(raw)

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        import json

        # redis-py's SET ... EX requires a whole number of seconds; callers
        # in this codebase pass settings sourced from _env_float (e.g.
        # HELIUS_HOLDER_CACHE_TTL_SECONDS = 420.0), so a float reaching here
        # is expected, not a caller bug. Round up so we never cache for
        # less than the caller asked for.
        import math

        ttl_int = max(1, math.ceil(ttl_seconds))
        await self._redis.set(self._key(key), json.dumps(value), ex=ttl_int)

    async def delete(self, key: str) -> None:
        await self._redis.delete(self._key(key))


_cache_singleton: Cache | None = None
_cache_singleton_lock: "asyncio.Lock | None" = None


def _get_singleton_lock() -> "asyncio.Lock":
    # Created lazily (not at module import time) since asyncio.Lock() binds
    # to the running event loop in some Python versions; this must only be
    # constructed once we're actually inside async code.
    global _cache_singleton_lock
    if _cache_singleton_lock is None:
        _cache_singleton_lock = asyncio.Lock()
    return _cache_singleton_lock


async def get_cache() -> Cache:
    """Returns the process-wide cache instance. Chooses Redis when
    REDIS_URL is configured (production, multi-instance), falls back to
    the in-memory cache otherwise (local dev, this sandbox, or a
    deliberate single-instance deployment) — mirrors the same
    "Redis-if-configured, else in-memory" pattern used for FSM storage in
    `platform/gateway/app.py`.

    Construction is lock-guarded: without it, a burst of concurrent
    first-callers (e.g. several tokens scored in the same scan cycle) can
    each see `_cache_singleton is None` before any of them finishes
    constructing one, resulting in multiple RedisCache instances (and
    connections) instead of one shared client."""

    global _cache_singleton
    if _cache_singleton is not None:
        return _cache_singleton

    async with _get_singleton_lock():
        if _cache_singleton is not None:
            return _cache_singleton

        import os

        redis_url = os.getenv("REDIS_URL", "").strip()
        if redis_url:
            try:
                _cache_singleton = RedisCache(redis_url)
            except ImportError:
                _cache_singleton = InMemoryCache()
        else:
            _cache_singleton = InMemoryCache()
        return _cache_singleton
