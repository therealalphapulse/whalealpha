"""Shared HTTP retry/backoff/circuit-breaker + TTL caching layer — used by
every HTTP-based Phase 1 discovery provider (Helius wallet-history, Jupiter,
Birdeye, DexScreener, pump.fun, LaunchLab, Raydium, Meteora).

Problem this closes: every free-tier HTTP provider the discovery engine
talks to can return 429 under normal discovery load, and a single 429 used
to be treated exactly like a permanent failure — the candidate got rejected
outright instead of retried, and there was no shared way to stop hammering
a provider that's clearly down. This module gives every HTTP call in
integrations/ one consistent way to:

  * cap concurrent in-flight requests to a provider (`asyncio.Semaphore`),
  * back off exponentially (with jitter) on 429/5xx/network errors, honoring
    a `Retry-After` header when the provider sends one,
  * distinguish TRANSIENT failures (429, 5xx, connect/timeout errors, or a
    currently-open circuit breaker — worth retrying) from PERMANENT ones
    (4xx other than 429, or "no provider configured" — retrying can never
    succeed), so callers can route transient failures to a retry queue
    instead of a permanent rejection,
  * cache successful responses briefly (`TTLCache`) so re-discovering the
    same address/token from multiple sources in one cycle doesn't refetch
    it, and negative-cache permanent failures so a known-bad address isn't
    retried every cycle either,
  * trip a per-provider circuit breaker (`CircuitBreaker`) after repeated
    consecutive transient failures, so a provider that's clearly down stops
    burning retry budget and wall-clock time on every candidate in a batch,
  * track per-provider observability metrics (`ProviderMetrics`) — request
    count, success rate, 429 count, retry count, cache hit ratio, average
    latency, circuit-breaker skips — surfaced via `get_all_provider_metrics`
    for the discovery cycle's structured logs.

Two ways to use this module:

  * `fetch_with_retry` directly — the low-level primitive (retry/backoff/
    Retry-After only, no breaker/metrics/naming). Used internally by
    `ProviderClient.get` and directly by `wallet_discovery_source.
    fetch_wallet_swap_history` (which already has its own dedicated
    semaphore/cache and doesn't need a circuit breaker — see that module).
  * `get_provider_client(name, ...).get(client, url, ...)` — the
    recommended entry point for every *discovery-source* provider (Jupiter,
    Birdeye, DexScreener, pump.fun, LaunchLab, Raydium, Meteora): one
    named, process-wide `ProviderClient` per provider bundles the
    semaphore + circuit breaker + metrics together so every integrations/
    function for that provider shares the same resilience state.
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
    retrying later" (429 / 5xx / network error, retries exhausted, or the
    provider's circuit breaker is currently open) from a permanent failure
    (4xx other than 429) — callers use this to decide retry-queue vs.
    permanent-reject. `retried` and `rate_limited` feed per-cycle
    observability counters (see engines/discovery.py). `circuit_open` is set
    only by `ProviderClient.get` when the call was skipped entirely because
    the breaker was open (no network call was made at all).
    """

    response: httpx.Response | None
    transient: bool
    retried: int = 0
    rate_limited: bool = False
    circuit_open: bool = False


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


_SENSITIVE_HEADER_KEYS = {"authorization", "x-api-key", "api-key", "apikey", "x-auth-token"}


def mask_headers_for_log(headers: dict[str, str] | None) -> dict[str, str]:
    """Redacts credential-bearing headers before they could ever reach a log
    line — same intent as `_mask_url` for query-string keys. No call site in
    this repo currently logs headers, but every provider integration is
    expected to route through this if it ever needs to (see the Phase 1
    security requirement: never log API keys/bearer tokens)."""
    if not headers:
        return {}
    return {k: ("[REDACTED]" if k.lower() in _SENSITIVE_HEADER_KEYS else v) for k, v in headers.items()}


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


# --------------------------------------------------------------------------
# Provider client — binds a semaphore + circuit breaker + metrics to one
# named provider, so every discovery-source integration (Jupiter, Birdeye,
# DexScreener, pump.fun, LaunchLab, Raydium, Meteora, Helius, ...) goes
# through the exact same resilience + observability path instead of each
# reimplementing it. See engines/discovery.py's run_discovery_cycle for how
# `get_all_provider_metrics()` feeds the structured per-cycle log.
# --------------------------------------------------------------------------


@dataclass
class ProviderMetrics:
    """Cumulative, process-lifetime counters for one named provider. Reset
    only on process restart — engines/discovery.py logs the running totals
    (and derived rates) every cycle rather than resetting them per-cycle, so
    "success rate" reflects the provider's overall reliability, not just the
    last 15 minutes.
    """

    requests: int = 0
    successes: int = 0
    failures: int = 0
    rate_limited: int = 0
    retries: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    circuit_open_skips: int = 0
    total_latency_seconds: float = 0.0

    @property
    def success_rate(self) -> float:
        return round(self.successes / self.requests, 3) if self.requests else 0.0

    @property
    def cache_hit_ratio(self) -> float:
        total = self.cache_hits + self.cache_misses
        return round(self.cache_hits / total, 3) if total else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return round((self.total_latency_seconds / self.requests) * 1000, 1) if self.requests else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": self.success_rate,
            "rate_limited": self.rate_limited,
            "retries": self.retries,
            "cache_hit_ratio": self.cache_hit_ratio,
            "circuit_open_skips": self.circuit_open_skips,
            "avg_latency_ms": self.avg_latency_ms,
        }


class CircuitBreaker:
    """Minimal per-provider circuit breaker: opens after
    `failure_threshold` *consecutive* transient failures, fails fast (no
    network call at all) for `cooldown_seconds`, then allows a single trial
    request ("half-open") — a success closes it again, a failure re-opens
    the cooldown. A provider that's clearly down stops burning retry budget
    and wall-clock time on every candidate in the batch; the discovery cycle
    just treats every skipped call as a transient failure and moves on
    (never a permanent reject — see ProviderClient.get).

    Only *transient* failures count against the threshold — a permanent one
    (bad API key, 404) says nothing about whether the provider is reachable,
    so it doesn't trip the breaker.
    """

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 60.0) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._cooldown = cooldown_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def allow_request(self) -> bool:
        if self._opened_at is None:
            return True
        return (time.monotonic() - self._opened_at) >= self._cooldown

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None and not self.allow_request()

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_transient_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold and self._opened_at is None:
            self._opened_at = time.monotonic()


_provider_clients: dict[str, "ProviderClient"] = {}


class ProviderClient:
    """One instance per named provider (module-level registry, see
    `get_provider_client`) — every call through `.get()` shares that
    provider's semaphore, circuit breaker, and metrics regardless of which
    integrations/ function or discovery-cycle candidate triggered it.
    """

    def __init__(
        self,
        name: str,
        *,
        max_concurrency: int,
        failure_threshold: int,
        cooldown_seconds: float,
    ) -> None:
        self.name = name
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.breaker = CircuitBreaker(failure_threshold, cooldown_seconds)
        self.metrics = ProviderMetrics()

    async def get(
        self,
        client: httpx.AsyncClient,
        url: str,
        **kwargs: Any,
    ) -> HttpFetchResult:
        return await self._request(client, "GET", url, **kwargs)

    async def post(
        self,
        client: httpx.AsyncClient,
        url: str,
        **kwargs: Any,
    ) -> HttpFetchResult:
        """Same resilience path as `.get()` (semaphore + circuit breaker +
        retry/backoff/Retry-After + metrics) for POST requests — added for
        the blockchain-first discovery engine's JSON-RPC calls
        (`getBlock`/`getSlot` are POST-only per the Solana JSON-RPC spec),
        see integrations/chain_scanner.py. Purely additive: `.get()`'s
        behavior/signature is unchanged.
        """
        return await self._request(client, "POST", url, **kwargs)

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> HttpFetchResult:
        if not self.breaker.allow_request():
            self.metrics.circuit_open_skips += 1
            log.debug("Provider circuit open, skipping request", provider=self.name, url=_mask_url(url))
            return HttpFetchResult(response=None, transient=True, circuit_open=True)

        start = time.monotonic()
        result = await fetch_with_retry(client, method, url, semaphore=self.semaphore, **kwargs)
        elapsed = time.monotonic() - start

        self.metrics.requests += 1
        self.metrics.total_latency_seconds += elapsed
        self.metrics.retries += result.retried
        if result.rate_limited:
            self.metrics.rate_limited += 1

        succeeded = result.response is not None and result.response.status_code < 400
        if succeeded:
            self.metrics.successes += 1
            self.breaker.record_success()
        else:
            self.metrics.failures += 1
            if result.transient:
                self.breaker.record_transient_failure()
            # A permanent failure (bad key, 404) says nothing about provider
            # reachability, so it never trips the breaker — see its docstring.

        return result


def get_provider_client(
    name: str,
    *,
    max_concurrency: int = 5,
    failure_threshold: int = 5,
    cooldown_seconds: float = 60.0,
) -> ProviderClient:
    """Get-or-create the process-wide ProviderClient for `name`. Concurrency/
    breaker settings only take effect on first creation for that name (they
    come from Env, which doesn't change mid-process) — later calls with
    different values reuse the existing instance rather than resetting its
    accumulated metrics/breaker state.
    """
    existing = _provider_clients.get(name)
    if existing is not None:
        return existing
    created = ProviderClient(
        name, max_concurrency=max_concurrency, failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds
    )
    _provider_clients[name] = created
    return created


def get_all_provider_metrics() -> dict[str, dict[str, float | int]]:
    """Snapshot of every provider's cumulative metrics — fed into
    engines/discovery.run_discovery_cycle's structured per-cycle log so
    provider latency/success-rate/429-count/retry-count/cache-hit-ratio/
    circuit-breaker-skips are all visible without a separate metrics
    backend."""
    return {name: pc.metrics.as_dict() | {"circuit_open": pc.breaker.is_open} for name, pc in _provider_clients.items()}
