"""Shared HTTP retry/backoff + TTL caching helpers — Phase 1 production
hardening (rate-limit resilience).

Problem this closes: every free-tier HTTP provider the discovery engine
talks to (Helius wallet-history, Jupiter/Birdeye/DexScreener trending) can
return 429 under normal discovery load, and prior to this module a single
429 was treated exactly like a permanent failure — the candidate got
rejected outright instead of retried. This module gives every HTTP call in
integrations/ a single, consistent way to:

  * cap concurrent in-flight requests to a provider (`asyncio.Semaphore`),
  * back off exponentially (with jitter) on 429/5xx/network errors, honoring
    a `Retry-After` header when the provider sends one,
  * distinguish TRANSIENT failures (429, 5xx, connect/timeout errors — worth
    retrying) from PERMANENT ones (4xx other than 429, or "no provider
    configured" — retrying can never succeed), so callers can route
    transient failures to a retry queue instead of a permanent rejection,
  * cache successful responses briefly (`TTLCache`) so re-discovering the
    same address from multiple sources in one cycle doesn't refetch it, and
    negative-cache permanent failures so a known-bad address isn't retried
    every cycle either.

Nothing here is provider-specific — every integrations/ module wraps its own
`httpx.AsyncClient.get` calls with `fetch_with_retry` and gets a
`HttpFetchResult` back rather than raising or returning bare None.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import httpx

from whale_alpha.utils.logger import child_logger

log = child_logger("httpRetry")

T = TypeVar("T")


@dataclass(frozen=True)
class HttpFetchResult:
    """Outcome of `fetch_with_retry`.

    `response` is set only on a genuine 2xx-ish success (whatever the caller
    considered a usable status code). `transient` distinguishes "worth
    retrying later" (429 / 5xx / network error, retries exhausted) from a
    permanent failure (4xx other than 429) — callers use this to decide
    retry-queue vs. permanent-reject. `retried` and `rate_limited` feed
    per-cycle observability counters (see engines/discovery.py).
    """

    response: httpx.Response | None
    transient: bool
    retried: int = 0
    rate_limited: bool = False


async def fetch_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    semaphore: asyncio.Semaphore | None = None,
    max_retries: int = 3,
    base_backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 30.0,
    jitter_seconds: float = 0.25,
    timeout: float = 15.0,
    **request_kwargs: Any,
) -> HttpFetchResult:
    """Single entry point every provider integration should use instead of
    calling `client.get`/`client.post` directly.

    Retries on 429 (honoring `Retry-After` if present, otherwise exponential
    backoff) and on 5xx / network errors (exponential backoff only — no
    server-provided hint). A 4xx other than 429 is treated as permanent and
    returned immediately without retrying (retrying a 401/403/404 wastes a
    request budget it can never recover). `semaphore` bounds concurrent
    in-flight requests to this provider across the whole process, so a batch
    of concurrent candidate lookups (see evaluate_candidates) never fires
    more than `semaphore._value` requests at once regardless of how many
    coroutines are scheduled.
    """
    attempt = 0
    rate_limited = False
    while True:
        try:
            async with _maybe(semaphore):
                res = await client.request(method, url, timeout=timeout, **request_kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as err:
            if attempt >= max_retries:
                log.warning(
                    "HTTP request failed after retries (network error)",
                    url=_mask_url(url),
                    attempts=attempt + 1,
                    err=str(err),
                )
                return HttpFetchResult(response=None, transient=True, retried=attempt)
            await _sleep_backoff(attempt, base_backoff_seconds, max_backoff_seconds, jitter_seconds)
            attempt += 1
            continue

        if res.status_code == 429:
            rate_limited = True
            if attempt >= max_retries:
                log.warning(
                    "HTTP request still rate-limited after retries",
                    url=_mask_url(url),
                    attempts=attempt + 1,
                )
                return HttpFetchResult(response=None, transient=True, retried=attempt, rate_limited=True)
            retry_after = _parse_retry_after(res.headers.get("retry-after"))
            if retry_after is not None:
                await asyncio.sleep(retry_after + random.uniform(0, jitter_seconds))
            else:
                await _sleep_backoff(attempt, base_backoff_seconds, max_backoff_seconds, jitter_seconds)
            attempt += 1
            continue

        if res.status_code >= 500:
            if attempt >= max_retries:
                log.warning(
                    "HTTP request failed after retries (server error)",
                    url=_mask_url(url),
                    status=res.status_code,
                    attempts=attempt + 1,
                )
                return HttpFetchResult(response=None, transient=True, retried=attempt)
            await _sleep_backoff(attempt, base_backoff_seconds, max_backoff_seconds, jitter_seconds)
            attempt += 1
            continue

        if res.status_code >= 400:
            # Permanent: bad key, not found, bad request — retrying can't help.
            return HttpFetchResult(response=res, transient=False, retried=attempt, rate_limited=rate_limited)

        return HttpFetchResult(response=res, transient=False, retried=attempt, rate_limited=rate_limited)


def _maybe(semaphore: asyncio.Semaphore | None):
    return semaphore if semaphore is not None else _NULL_LOCK


class _NullAsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> None:
        return None


_NULL_LOCK = _NullAsyncContext()


async def _sleep_backoff(attempt: int, base: float, cap: float, jitter: float) -> None:
    delay = min(cap, base * (2**attempt)) + random.uniform(0, jitter)
    await asyncio.sleep(delay)


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delay-seconds (most providers) or an HTTP-date;
    only the common delay-seconds form is handled — an unparseable/HTTP-date
    value falls back to plain exponential backoff rather than guessing.
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, seconds)


def _mask_url(url: str) -> str:
    """Strips query params (API keys are commonly passed as `?api-key=...` /
    `?x-api-key=...`) before the URL ever reaches a log line."""
    return url.split("?", 1)[0]


class TTLCache(Generic[T]):
    """Tiny in-process TTL cache — positive results cached for `ttl_seconds`,
    used to dedupe repeated fetches of the same address within one discovery
    cycle (a wallet can surface from several sources at once) and across
    closely-spaced cycles. Not shared across processes; fine for a
    single-worker discovery engine, and a cache miss just means "fetch it",
    never a correctness issue.
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 5000) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._store: dict[str, tuple[float, T]] = {}

    def get(self, key: str) -> T | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: T) -> None:
        if len(self._store) >= self._max_entries:
            # Cheap eviction: drop an arbitrary (oldest-inserted-ish, dict
            # ordering) entry rather than maintaining a full LRU — this cache
            # exists to cut duplicate requests within a cycle, not to be a
            # durable store, so approximate eviction is fine.
            self._store.pop(next(iter(self._store)), None)
        self._store[key] = (time.monotonic() + self._ttl, value)

    def __len__(self) -> int:
        return len(self._store)
