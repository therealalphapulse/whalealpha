"""
Multi-provider RPC gateway for Solana with automatic failover, health monitoring,
and intelligent routing. Supports Helius, Alchemy, dRPC, and QuickNode with
priority-based scheduling and adaptive rate limiting.

Every module that talks to RPC should call through `multi_rpc_manager`
(via `request_json`, `get_cached` / `set_cached`) instead of opening its
own aiohttp session. That gives the whole bot, for free:

  1. One global outbound queue — no two modules can burst any provider at the
     same time, regardless of how many background loops are running.
  2. Global rate limiting — requests are throttled to a configurable
     requests/second ceiling *before* any provider has a chance to return
     HTTP 429, instead of firing immediately and reacting after the fact.
  3. Automatic failover — when a provider fails, the next provider in the
     priority list is tried automatically without changing any caller code.
  4. Provider health monitoring — circuit breaker tracks consecutive failures
     and temporarily removes unhealthy providers from rotation.
  5. Intelligent request routing — HIGH-priority requests (wallet balance,
     portfolio, real trading) are served ahead of LOW-priority work (scanning,
     analysis, background intelligence).
  6. A shared TTL cache — identical lookups within the cache lifetime are
     served from memory instead of re-hitting providers.
  7. Exponential backoff with adaptive throttling on 429 / timeout /
     transient network errors — never an immediate retry, never a retry storm.
  8. Starvation protection — background work makes forward progress even under
     sustained HIGH-priority load.
  9. Request deduplication — identical concurrent requests share a single
     upstream call when possible.
  10. Provider statistics — track success rates, latency, and provider switches
      for monitoring without changing public interfaces.

Every public method here fails soft: on an unrecoverable error (missing
key, retries exhausted, non-retryable HTTP error, all providers unavailable)
it returns None rather than raising, matching the "None = unknown, not zero"
convention every existing RPC-backed function in this codebase already relies on.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Any, Optional
import hashlib

import aiohttp

from config.settings import (
    HELIUS_API_KEY,
    ALCHEMY_API_KEY,
    DRPC_API_KEY,
    QUICKNODE_API_KEY,
    QUICKNODE_SOLANA_RPC,
    ANKR_API_KEY,
    ENABLE_HELIUS,
    ENABLE_ALCHEMY,
    ENABLE_DRPC,
    ENABLE_QUICKNODE,
    ENABLE_ANKR,
    RPC_PROVIDER_PRIORITY,
    MULTI_RPC_MAX_REQUESTS_PER_SECOND,
    MULTI_RPC_MAX_RETRIES,
    MULTI_RPC_CACHE_TTL_SECONDS,
    MULTI_RPC_HEALTH_CHECK_INTERVAL_SECONDS,
    MULTI_RPC_CIRCUIT_BREAKER_THRESHOLD,
    MULTI_RPC_PROVIDER_COOLDOWN_SECONDS,
    MULTI_RPC_TIMEOUT_SECONDS,
    MULTI_RPC_MAX_CONCURRENT_REQUESTS,
    MULTI_RPC_DEDUP_WINDOW_SECONDS,
)

logger = logging.getLogger("AlphaPulse.MultiRPCManager")

# Helius DAS ("Digital Asset Standard") / proprietary RPC methods. These are
# NOT part of standard Solana JSON-RPC and are only implemented by Helius —
# Alchemy, dRPC, and QuickNode will reject them (HTTP 4xx/"method not found").
# Routing these to non-Helius providers wastes retries/time and produces
# misleading "provider failed" noise for a request that was never going to
# succeed there. Standard methods (getAccountInfo, getBalance, getTransaction,
# sendTransaction, getSignaturesForAddress, getTokenSupply, etc.) are NOT in
# this set and continue to failover across every configured provider.
_HELIUS_ONLY_METHODS = {
    "getAsset",               # DAS single-asset metadata (services/solana_resolver.py)
    "getAssetBatch",
    "getAssetProof",
    "getAssetsByOwner",       # DAS portfolio lookup (wallet_portfolio.py, wallet_intelligence.py) — being migrated off, see those modules
    "getAssetsByGroup",
    "getAssetsByCreator",
    "getAssetsByAuthority",
    "searchAssets",
    "getProgramAccountsV2",   # Helius-proprietary cursor-paginated enhanced RPC
                               # method (domain/intelligence/holders.py primary
                               # holder-retrieval path). NOT standard Solana
                               # JSON-RPC — Alchemy/dRPC/QuickNode don't
                               # implement it, so routing it there would just
                               # burn a failed attempt on every request.
}

# Human-readable names for startup/failover log lines (internal keys stay
# lowercase since they're also used to build config dict lookups and URLs).
_PROVIDER_DISPLAY_NAMES = {
    "helius": "Helius",
    "alchemy": "Alchemy",
    "drpc": "dRPC",
    "quicknode": "QuickNode",
    # Added by the AlphaPulse Provider Integration Task (2026-08-19) as an
    # additional RPC/failover provider -- see RPC_PROVIDER_PRIORITY in
    # config/settings.py for where it sits in the failover order (last).
    "ankr": "Ankr",
}

# --- Priority levels (lower = served first) ---
PRIORITY_HIGH = 0     # user wallet balance / portfolio / trade validation
PRIORITY_NORMAL = 5   # on-demand user lookups (e.g. /token, /resolve)
PRIORITY_LOW = 10     # background: holder analysis, signal scanning,
                       # watchlist refresh, discovery, intelligence refresh

# --- Per-attempt outcomes for _dispatch_to_provider ---
#
# A plain bool used to collapse two very different situations into one
# "not success" signal: an actual provider FAILURE (429/5xx/timeout/error)
# and a provider that answered HTTP 200 with a syntactically valid but
# EMPTY result (e.g. a plan/add-on restriction on a heavy call like
# getProgramAccounts that silently returns `"result": []` instead of an
# error). Callers that opt in via `retry_on_empty_result` need to tell
# these apart: an empty result should NOT trip the circuit breaker (the
# provider didn't fail), but it also should not be trusted as final
# without at least trying the other eligible providers first.
_OUTCOME_SUCCESS = "success"            # non-empty (or emptiness not tracked) — stop rotation
_OUTCOME_EMPTY_SUCCESS = "empty_success"  # HTTP 200, valid, empty result — keep rotating
_OUTCOME_FAILURE = "failure"            # genuine failure — keep rotating, penalize health

_MIN_REQUEST_INTERVAL = (
    1.0 / MULTI_RPC_MAX_REQUESTS_PER_SECOND if MULTI_RPC_MAX_REQUESTS_PER_SECOND > 0 else 0.5
)
_BASE_BACKOFF_SECONDS = 1.5
_MAX_BACKOFF_SECONDS = 30.0
_DEFAULT_TIMEOUT_SECONDS = MULTI_RPC_TIMEOUT_SECONDS
_RATE_LIMIT_LOG_INTERVAL_SECONDS = 30.0
_MAX_RATE_MULTIPLIER = 8.0
_SUCCESSES_TO_EASE = 5
_STARVATION_MAX_WAIT_SECONDS = 20.0


class _CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, expires_at: float):
        self.value = value
        self.expires_at = expires_at


class _ProviderStats:
    """Track per-provider statistics for monitoring."""
    __slots__ = (
        "total_requests", "successful_requests", "failed_requests",
        "rate_limited_responses", "timeouts", "total_latency_ms",
        "provider_switches_to_this",
    )

    def __init__(self):
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.rate_limited_responses = 0
        self.timeouts = 0
        self.total_latency_ms = 0.0
        self.provider_switches_to_this = 0

    def average_latency_ms(self) -> float:
        if self.successful_requests == 0:
            return 0.0
        return self.total_latency_ms / self.successful_requests


class _ProviderHealth:
    """Track provider circuit-breaker state and a lightweight health score."""
    __slots__ = (
        "consecutive_failures", "circuit_broken_since", "last_recovery_attempt",
        "score",
    )

    # Score bounds and step sizes: degrades fast on failure (a rate-limited
    # or timing-out provider should drop out of "preferred" quickly), climbs
    # back slowly on success (a couple of good responses shouldn't instantly
    # erase a recent bad streak). Used only to ORDER otherwise-eligible
    # providers ("healthy providers should be selected first") — it never
    # overrides the circuit breaker, which is the hard on/off switch.
    _SCORE_MAX = 100.0
    _SCORE_MIN = 0.0
    _SCORE_SUCCESS_STEP = 5.0
    _SCORE_FAILURE_STEP = 20.0

    def __init__(self):
        self.consecutive_failures = 0
        self.circuit_broken_since: Optional[float] = None
        self.last_recovery_attempt: Optional[float] = None
        self.score: float = self._SCORE_MAX

    def is_circuit_broken(self) -> bool:
        if self.circuit_broken_since is None:
            return False
        cooldown = MULTI_RPC_PROVIDER_COOLDOWN_SECONDS or MULTI_RPC_HEALTH_CHECK_INTERVAL_SECONDS
        elapsed = time.monotonic() - self.circuit_broken_since
        return elapsed < cooldown

    def mark_broken(self) -> None:
        if self.circuit_broken_since is None:
            self.circuit_broken_since = time.monotonic()
            logger.warning(
                f"Provider circuit breaker activated (threshold={MULTI_RPC_CIRCUIT_BREAKER_THRESHOLD}, "
                f"cooldown={MULTI_RPC_PROVIDER_COOLDOWN_SECONDS or MULTI_RPC_HEALTH_CHECK_INTERVAL_SECONDS}s)"
            )

    def mark_healthy(self) -> None:
        if self.circuit_broken_since is not None:
            logger.info(f"Provider recovered from circuit break")
        self.consecutive_failures = 0
        self.circuit_broken_since = None
        self.score = min(self._SCORE_MAX, self.score + self._SCORE_SUCCESS_STEP)

    def mark_failed(self) -> None:
        """Record a failed attempt (429 / 5xx / timeout / network error)."""
        self.score = max(self._SCORE_MIN, self.score - self._SCORE_FAILURE_STEP)


class _Job:
    __slots__ = (
        "method", "url", "params", "json_body", "timeout",
        "priority", "attempt", "future", "context", "enqueued_at",
        "provider_name", "start_time_ms", "exclude_providers",
        "retry_on_empty_result", "best_empty_result", "best_empty_provider",
        "empty_providers_seen", "failed_providers_seen",
    )

    def __init__(
        self, method, url, params, json_body, timeout, priority, context,
        provider_name="", exclude_providers=None, retry_on_empty_result=False,
    ):
        self.method = method
        self.url = url
        self.params = params
        self.json_body = json_body
        self.timeout = timeout
        self.priority = priority
        self.attempt = 0
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.context = context
        self.enqueued_at = time.monotonic()
        self.provider_name = provider_name
        self.start_time_ms = 0.0
        # Optional per-call provider exclusion (e.g. Signal/Quote Alert
        # calls scoped off dRPC). None/empty means no exclusion — every
        # other caller (wallet, portfolio, etc.) is completely unaffected
        # and keeps the full RPC_PROVIDER_PRIORITY failover chain.
        self.exclude_providers = set(exclude_providers) if exclude_providers else None
        # When True, an HTTP-200/valid-JSON/no-error response whose
        # "result" is an empty list is treated as _OUTCOME_EMPTY_SUCCESS
        # instead of an immediate final success — see _dispatch. Used by
        # holder-account scans (getProgramAccounts) where an empty array
        # is ambiguous between "genuinely no holders yet" and "this
        # provider silently can't/won't run this scan". Default False
        # preserves the original behavior for every other caller.
        self.retry_on_empty_result = retry_on_empty_result
        self.best_empty_result: Any = None
        self.best_empty_provider: str = ""
        self.empty_providers_seen: list[str] = []
        self.failed_providers_seen: list[str] = []


class _DedupEntry:
    """Entry in the request deduplication cache."""
    __slots__ = ("future", "expires_at")

    def __init__(self, future: asyncio.Future, expires_at: float):
        self.future = future
        self.expires_at = expires_at


class MultiRPCManager:
    """Multi-provider RPC gateway with failover, health monitoring, and rate limiting."""

    def __init__(self):
        # Priority-bucketed FIFO scheduling
        self._buckets: dict[int, deque] = {}
        self._wakeup: asyncio.Queue = asyncio.Queue()
        self._cache: dict[str, _CacheEntry] = {}
        self._dedup_cache: dict[str, _DedupEntry] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._worker_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._last_dispatch = 0.0
        self._dispatch_lock = asyncio.Lock()

        # Aggregated rate-limit / failure logging
        self._rl_hits_since_log = 0
        self._rl_window_start = time.monotonic()

        # Adaptive backpressure
        self._rate_multiplier = 1.0
        self._consecutive_429s = 0
        self._consecutive_successes = 0

        # Concurrent request tracking
        self._concurrent_requests = 0
        self._concurrent_lock = asyncio.Lock()

        # Provider management
        self._providers: dict[str, dict] = {}
        self._provider_health: dict[str, _ProviderHealth] = {}
        self._provider_stats: dict[str, _ProviderStats] = {}
        self._current_provider_index = 0
        self._init_providers()

    def _init_providers(self) -> None:
        """Initialize configured providers in priority order."""
        provider_configs = {
            "helius": {
                "enabled": ENABLE_HELIUS,
                "api_key": HELIUS_API_KEY,
                "endpoint": "https://mainnet.helius-rpc.com/",
            },
            "alchemy": {
                "enabled": ENABLE_ALCHEMY,
                "api_key": ALCHEMY_API_KEY,
                "endpoint": "https://solana-mainnet.g.alchemy.com/v2/",
            },
            "drpc": {
                "enabled": ENABLE_DRPC,
                "api_key": DRPC_API_KEY,
                "endpoint": "https://solana-mainnet.rpc.drpc.org/",
            },
            "quicknode": {
                "enabled": ENABLE_QUICKNODE,
                # Prefer the full per-account URL (QUICKNODE_SOLANA_RPC) —
                # QuickNode issues a complete endpoint, not a bare key to
                # append to a shared host. QUICKNODE_API_KEY (legacy,
                # templated onto a generic host below) still works if that's
                # all that's configured, for backward compatibility.
                "api_key": QUICKNODE_SOLANA_RPC or QUICKNODE_API_KEY,
                "full_url": bool(QUICKNODE_SOLANA_RPC),
                "endpoint": QUICKNODE_SOLANA_RPC or "https://solana-mainnet.rpc.quicknode.io/",
            },
            # Added by the AlphaPulse Provider Integration Task (2026-08-19).
            # Additional RPC/failover provider only -- registered last in
            # RPC_PROVIDER_PRIORITY (config/settings.py), so it is only ever
            # tried once Helius, QuickNode, Alchemy, AND dRPC have all
            # failed or are circuit-broken for a given request. Ankr issues
            # a premium key appended as a URL path segment (not a query
            # param like Alchemy/Helius), handled in _build_provider_url.
            "ankr": {
                "enabled": ENABLE_ANKR,
                "api_key": ANKR_API_KEY,
                "endpoint": "https://rpc.ankr.com/solana/",
            },
        }

        # Use configured provider priority
        priority_list = RPC_PROVIDER_PRIORITY or list(provider_configs.keys())

        # --- Diagnostics: collected while we walk the priority list so the
        # startup report below reflects exactly what registration decided,
        # rather than re-deriving it a second time. ---
        skip_reasons: list[str] = []
        unknown_in_priority: list[str] = []

        for provider_name in priority_list:
            if provider_name not in provider_configs:
                logger.warning(f"Unknown RPC provider in priority list: {provider_name}")
                unknown_in_priority.append(provider_name)
                continue

            config = provider_configs[provider_name]
            if not config["enabled"]:
                logger.info(f"RPC provider disabled via settings: {provider_name}")
                skip_reasons.append(
                    f"{provider_name}: skipped — disabled via ENABLE_{provider_name.upper()}"
                )
                continue

            if not config["api_key"]:
                logger.info(f"RPC provider skipped (no API key/endpoint): {provider_name}")
                if provider_name == "quicknode":
                    skip_reasons.append(
                        "quicknode: skipped — neither QUICKNODE_SOLANA_RPC (preferred, full "
                        "endpoint URL) nor QUICKNODE_API_KEY (legacy) is set in the environment"
                    )
                else:
                    skip_reasons.append(
                        f"{provider_name}: skipped — {provider_name.upper()}_API_KEY is not set "
                        f"(or blank) in the environment"
                    )
                continue

            self._providers[provider_name] = config
            self._provider_health[provider_name] = _ProviderHealth()
            self._provider_stats[provider_name] = _ProviderStats()

        # Providers configured in provider_configs but never even mentioned in
        # the priority list (e.g. a custom RPC_PROVIDER_PRIORITY that dropped
        # one) — surfaced separately since they were never evaluated above.
        for provider_name in provider_configs:
            if provider_name not in priority_list:
                skip_reasons.append(
                    f"{provider_name}: skipped — not present in RPC_PROVIDER_PRIORITY "
                    f"({priority_list})"
                )

        if not self._providers:
            logger.error(
                "No RPC providers configured! Configure at least one of: "
                "HELIUS_API_KEY, ALCHEMY_API_KEY, DRPC_API_KEY, QUICKNODE_API_KEY, "
                "ANKR_API_KEY"
            )
        else:
            provider_names = ", ".join(self._providers.keys())
            logger.info(f"MultiRPC initialized with providers: {provider_names}")

        # --- Mandatory startup diagnostic report ---
        # This is intentionally always-on (not gated behind DEBUG) because a
        # misconfigured provider pool is a production-affecting condition
        # (silent loss of failover) that should be visible in every deploy's
        # boot logs, not just when someone happens to be looking.
        def _flag(enabled: bool) -> str:
            return "Enabled" if enabled else "Disabled"

        report_lines = ["Configured providers:", ""]
        report_lines.append(f"Helius: {_flag(ENABLE_HELIUS)}")
        report_lines.append(f"Alchemy: {_flag(ENABLE_ALCHEMY)}")
        report_lines.append(f"dRPC: {_flag(ENABLE_DRPC)}")
        report_lines.append(f"QuickNode: {_flag(ENABLE_QUICKNODE)}")
        report_lines.append(f"Ankr: {_flag(ENABLE_ANKR)}")
        report_lines.append("")
        report_lines.append("Provider priority:")
        report_lines.append(f"{priority_list}")
        report_lines.append("")
        report_lines.append("Final provider pool:")
        report_lines.append(f"{list(self._providers.keys()) or '[] (NO PROVIDERS REGISTERED)'}")
        report_lines.append("")
        if skip_reasons or unknown_in_priority:
            report_lines.append("Reason any skipped provider was skipped:")
            for reason in skip_reasons:
                report_lines.append(f"  - {reason}")
            for name in unknown_in_priority:
                report_lines.append(
                    f"  - {name}: skipped — unrecognized provider name in "
                    f"RPC_PROVIDER_PRIORITY (must be one of helius, alchemy, drpc, quicknode)"
                )
        else:
            report_lines.append("Reason any skipped provider was skipped:")
            report_lines.append("  - (none skipped — all providers in the priority list registered)")

        logger.info("\n" + "\n".join(report_lines))

    def _eligible_providers_for(self, job: "_Job") -> list[str]:
        """
        Providers allowed to serve this job, in registration/priority order.

        Almost all requests (standard Solana JSON-RPC: getAccountInfo,
        getBalance, getTransaction, sendTransaction, getSignaturesForAddress,
        getTokenSupply, etc.) can go to ANY registered provider — full
        Helius → Alchemy → dRPC → QuickNode failover applies.

        The exception is Helius DAS/proprietary methods (see
        _HELIUS_ONLY_METHODS): those are only implemented by Helius, so
        routing them to Alchemy/dRPC/QuickNode would just burn retries on a
        request that can never succeed there. For those, only Helius (if
        registered) is eligible.
        """
        all_providers = list(self._providers.keys())
        if job.exclude_providers:
            all_providers = [p for p in all_providers if p not in job.exclude_providers]

        method = None
        if isinstance(job.json_body, dict):
            method = job.json_body.get("method")

        if method in _HELIUS_ONLY_METHODS:
            return [p for p in all_providers if p == "helius"]

        # Health-score-based ordering: "healthy providers should be selected
        # first". Sort is stable, so providers with an equal (e.g. all
        # fully-healthy) score keep the configured RPC_PROVIDER_PRIORITY
        # order — a provider only moves down the list once it's actually
        # taken recent failures. This never overrides the circuit breaker
        # (a circuit-broken provider is still skipped by _get_next_provider
        # regardless of where it sorts here); it only affects the ORDER in
        # which otherwise-eligible providers are tried.
        return sorted(
            all_providers,
            key=lambda p: -(self._provider_health[p].score if p in self._provider_health else 0.0),
        )

    def _get_next_provider(self, eligible: list[str]) -> Optional[str]:
        """Get the next healthy provider, cycling only through `eligible`."""
        if not eligible:
            return None

        now = time.monotonic()

        # Try all eligible providers in order
        for _ in range(len(eligible)):
            provider_name = eligible[self._current_provider_index % len(eligible)]
            self._current_provider_index += 1

            health = self._provider_health.get(provider_name)
            if health and health.is_circuit_broken():
                # Check if we should attempt recovery
                if health.last_recovery_attempt is None or \
                   (now - health.last_recovery_attempt) >= MULTI_RPC_HEALTH_CHECK_INTERVAL_SECONDS:
                    health.last_recovery_attempt = now
                    logger.info(f"Attempting recovery for provider: {provider_name}")
                    return provider_name
                continue

            return provider_name

        return None

    def _build_provider_url(self, provider_name: str) -> str:
        """Build the full RPC URL for a provider."""
        config = self._providers.get(provider_name)
        if not config:
            return ""

        endpoint = config["endpoint"]
        api_key = config["api_key"]

        if provider_name == "helius":
            return f"{endpoint}?api-key={api_key}"
        elif provider_name == "alchemy":
            return f"{endpoint}{api_key}"
        elif provider_name == "drpc":
            # dRPC uses bearer token in header, not URL param
            return endpoint
        elif provider_name == "quicknode":
            if config.get("full_url"):
                # QUICKNODE_SOLANA_RPC is already the complete endpoint
                # (QuickNode issues per-account URLs, not a bare key to
                # template onto a shared host).
                return endpoint
            return f"{endpoint}{api_key}"
        elif provider_name == "ankr":
            # Ankr's premium Solana endpoint takes the API key as a URL path
            # segment: https://rpc.ankr.com/solana/{api_key} (not a query
            # param like Alchemy, and not a bearer header like dRPC).
            return f"{endpoint}{api_key}"

        return ""

    def _get_provider_headers(self, provider_name: str) -> dict:
        """Get HTTP headers for a provider (e.g., Authorization header for dRPC)."""
        if provider_name == "drpc":
            config = self._providers.get(provider_name)
            if config:
                return {"Authorization": f"Bearer {config['api_key']}"}
        return {}

    def _request_dedup_key(self, method: str, url: str, json_body: Optional[dict]) -> str:
        """Generate a deduplication key for a request."""
        # Use method + url + json body hash
        body_hash = ""
        if json_body:
            body_str = str(sorted(json_body.items()))
            body_hash = hashlib.md5(body_str.encode()).hexdigest()
        key_str = f"{method}:{url}:{body_hash}"
        return hashlib.md5(key_str.encode()).hexdigest()

    def _check_dedup_cache(self, dedup_key: str) -> Optional[asyncio.Future]:
        """Check if an identical request is already in flight."""
        entry = self._dedup_cache.get(dedup_key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._dedup_cache.pop(dedup_key, None)
            return None
        return entry.future

    def _store_dedup_request(self, dedup_key: str, future: asyncio.Future) -> None:
        """Store a request in the deduplication cache."""
        self._dedup_cache[dedup_key] = _DedupEntry(
            future,
            time.monotonic() + MULTI_RPC_DEDUP_WINDOW_SECONDS
        )

    # Backward compatibility: reuse existing cache interfaces
    def get_cached(self, key: str) -> Any:
        """Return a cached value if present and unexpired, else None."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._cache.pop(key, None)
            return None
        return entry.value

    def set_cached(self, key: str, value: Any, ttl: float) -> None:
        """Store a value under key for ttl seconds. No-op if ttl <= 0."""
        if ttl and ttl > 0:
            self._cache[key] = _CacheEntry(value, time.monotonic() + ttl)

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    async def close(self) -> None:
        """Optional graceful shutdown hook."""
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        """Periodically evict expired cache/dedup entries that are never
        re-looked-up (get_cached/get_dedup only evict lazily, on access),
        so long-running processes don't accumulate stale entries forever."""
        while True:
            await asyncio.sleep(60)
            try:
                now = time.monotonic()
                expired_cache = [k for k, v in self._cache.items() if v.expires_at < now]
                for k in expired_cache:
                    self._cache.pop(k, None)

                expired_dedup = [k for k, v in self._dedup_cache.items() if v.expires_at < now]
                for k in expired_dedup:
                    self._dedup_cache.pop(k, None)

                if expired_cache or expired_dedup:
                    logger.debug(
                        f"Cleanup: evicted {len(expired_cache)} cache entr(y/ies), "
                        f"{len(expired_dedup)} dedup entr(y/ies)"
                    )
            except Exception as e:
                logger.warning(f"MultiRPC cleanup loop error: {e}")

    def _enqueue(self, job: "_Job") -> None:
        """Add job to its priority bucket and wake the worker."""
        job.enqueued_at = time.monotonic()
        self._buckets.setdefault(job.priority, deque()).append(job)
        self._wakeup.put_nowait(None)

    def _pop_next_ready_job(self) -> Optional["_Job"]:
        """Pick the next job to dispatch."""
        if not any(self._buckets.values()):
            return None

        now = time.monotonic()
        oldest_starved: Optional["_Job"] = None
        oldest_starved_priority: Optional[int] = None

        for priority, bucket in self._buckets.items():
            if not bucket:
                continue
            head = bucket[0]
            if now - head.enqueued_at >= _STARVATION_MAX_WAIT_SECONDS:
                if oldest_starved is None or head.enqueued_at < oldest_starved.enqueued_at:
                    oldest_starved = head
                    oldest_starved_priority = priority

        if oldest_starved is not None:
            self._buckets[oldest_starved_priority].popleft()
            if oldest_starved_priority != PRIORITY_HIGH:
                logger.info(
                    f"Starvation protection: force-dispatching {oldest_starved.context} "
                    f"(priority={oldest_starved_priority}) after "
                    f"{now - oldest_starved.enqueued_at:.1f}s waiting."
                )
            return oldest_starved

        for priority in sorted(p for p, b in self._buckets.items() if b):
            return self._buckets[priority].popleft()

        return None

    async def _next_job(self) -> "_Job":
        while True:
            job = self._pop_next_ready_job()
            if job is not None:
                while not self._wakeup.empty():
                    try:
                        self._wakeup.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                return job
            await self._wakeup.get()

    async def _worker_loop(self) -> None:
        while True:
            job = await self._next_job()
            try:
                await self._dispatch(job)
            except Exception as e:
                logger.error(f"MultiRPC worker error ({job.context}): {e}")
                if not job.future.done():
                    job.future.set_result(None)

    async def _throttle(self) -> None:
        """Global min-interval gate."""
        async with self._dispatch_lock:
            effective_interval = _MIN_REQUEST_INTERVAL * self._rate_multiplier
            now = time.monotonic()
            wait = effective_interval - (now - self._last_dispatch)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_dispatch = time.monotonic()

    async def _acquire_concurrency_slot(self) -> None:
        """Acquire a concurrent request slot."""
        while True:
            async with self._concurrent_lock:
                if MULTI_RPC_MAX_CONCURRENT_REQUESTS <= 0 or \
                   self._concurrent_requests < MULTI_RPC_MAX_CONCURRENT_REQUESTS:
                    self._concurrent_requests += 1
                    return
            await asyncio.sleep(0.01)

    async def _release_concurrency_slot(self) -> None:
        """Release a concurrent request slot."""
        async with self._concurrent_lock:
            self._concurrent_requests = max(0, self._concurrent_requests - 1)

    async def _dispatch(self, job: "_Job") -> None:
        """
        Dispatch a job, trying providers in order until one succeeds.

        Failover semantics:
          - Within one "cycle", every eligible provider is tried in order
            with NO delay between them — a 429 / 5xx / timeout / circuit-open
            on Helius switches to the next eligible provider (e.g. QuickNode)
            immediately.
          - Only once an ENTIRE cycle has failed (every eligible provider
            was tried and none succeeded) do we back off before trying the
            full rotation again, up to MULTI_RPC_MAX_RETRIES extra cycles.

        This is deliberately a single, self-contained retry loop (no
        separate delayed re-enqueue task) so a job is never dispatched by
        two independent code paths at once.
        """
        await self._throttle()
        await self._acquire_concurrency_slot()
        slot_held = True

        try:
            if not self._providers:
                logger.error("No providers available")
                if not job.future.done():
                    job.future.set_result(None)
                return

            eligible = self._eligible_providers_for(job)
            if not eligible:
                # Capability mismatch: this job needs a Helius-only DAS
                # method and Helius isn't registered. Fail fast with a clear
                # reason instead of pointlessly cycling the other providers.
                method = job.json_body.get("method") if isinstance(job.json_body, dict) else None
                logger.warning(
                    f"No eligible provider for {job.context} "
                    f"(method={method!r} requires Helius, which is not registered/configured)"
                )
                if not job.future.done():
                    job.future.set_result(None)
                return

            total_cycles = MULTI_RPC_MAX_RETRIES + 1

            for cycle in range(total_cycles):
                attempted_this_cycle = 0

                for _ in range(len(eligible)):
                    provider_name = self._get_next_provider(eligible)
                    if provider_name is None:
                        # Every eligible provider is currently circuit-broken
                        # and not yet due for a recovery probe — nothing left
                        # to try this cycle.
                        break

                    attempted_this_cycle += 1
                    job.provider_name = provider_name
                    logger.info(
                        f"Trying provider: {_PROVIDER_DISPLAY_NAMES.get(provider_name, provider_name)}"
                    )
                    outcome = await self._dispatch_to_provider(job, provider_name)
                    if outcome == _OUTCOME_SUCCESS:
                        return

                    if outcome == _OUTCOME_EMPTY_SUCCESS:
                        # The provider is healthy and answered — it just
                        # returned zero rows. Do NOT treat this as a
                        # failure (no circuit-breaker penalty), but don't
                        # trust it as final either: keep rotating through
                        # the remaining eligible providers this cycle so a
                        # single provider silently unable to run this scan
                        # can't masquerade as "this token has no holders".
                        job.empty_providers_seen.append(provider_name)
                        continue

                    # Failure: update health/circuit-breaker, then continue
                    # the inner loop immediately — this IS the "switch to
                    # the next provider automatically" behavior.
                    job.failed_providers_seen.append(provider_name)
                    health = self._provider_health.get(provider_name)
                    if health:
                        health.mark_failed()
                        health.consecutive_failures += 1
                        if health.consecutive_failures >= MULTI_RPC_CIRCUIT_BREAKER_THRESHOLD:
                            health.mark_broken()

                if job.future.done():
                    # A prior iteration already resolved it via an
                    # unexpected/parse-error branch in _dispatch_to_provider.
                    return

                if job.retry_on_empty_result and job.best_empty_result is not None:
                    # We've now tried every eligible provider we could reach
                    # this cycle. None returned real data, but at least one
                    # gave a clean HTTP-200 empty response. An empty result
                    # is a statement about chain state (or a plan
                    # restriction), not a transient fault — re-running the
                    # exact same rotation after a backoff won't change it,
                    # so we settle on it now instead of burning
                    # MULTI_RPC_MAX_RETRIES more cycles and adding latency
                    # to time-sensitive signal scoring.
                    cross_checked = len(job.empty_providers_seen)
                    total_tried = cross_checked + len(job.failed_providers_seen)
                    logger.info(
                        f"[HolderDiag] {job.context}: empty result cross-validated "
                        f"across {cross_checked}/{total_tried} reachable eligible "
                        f"provider(s) this cycle ({', '.join(job.empty_providers_seen)}) — "
                        + (
                            f"{len(job.failed_providers_seen)} other provider(s) "
                            f"({', '.join(job.failed_providers_seen)}) hard-failed rather "
                            f"than returning empty. "
                            if job.failed_providers_seen else ""
                        )
                        + "treating as a legitimate empty result, not a provider fault."
                    )
                    if total_tried == 1:
                        logger.info(
                            f"[HolderDiag] {job.context}: only one eligible provider could "
                            f"be reached this cycle — empty result has limited cross-"
                            f"validation; if this keeps happening for non-early tokens, "
                            f"check that provider's getProgramAccounts support/plan."
                        )
                    if not job.future.done():
                        job.future.set_result(job.best_empty_result)
                    return

                is_last_cycle = cycle == total_cycles - 1
                if is_last_cycle:
                    break

                if attempted_this_cycle == 0:
                    # Every eligible provider is circuit-broken; there is
                    # nothing rotation would gain from an immediate re-try,
                    # so use the standard health-check interval as the wait.
                    delay = MULTI_RPC_HEALTH_CHECK_INTERVAL_SECONDS
                else:
                    delay = min(_BASE_BACKOFF_SECONDS * (2 ** cycle), _MAX_BACKOFF_SECONDS)

                logger.info(
                    f"All eligible providers failed this pass ({job.context}); "
                    f"backing off {delay:.1f}s before retrying the rotation "
                    f"(cycle {cycle + 2}/{total_cycles})"
                )
                # Release the concurrency slot while backing off so a slow
                # provider recovery doesn't tie up a request slot other jobs
                # could be using.
                await self._release_concurrency_slot()
                slot_held = False
                await asyncio.sleep(delay)
                await self._acquire_concurrency_slot()
                slot_held = True

            # All cycles exhausted
            if job.retry_on_empty_result and job.best_empty_result is not None:
                # Safety net: this shouldn't normally be reached (the
                # cross-validated-empty check after each cycle above
                # resolves first), but if it ever is, a cross-checked
                # empty result is still strictly more informative than
                # None ("unknown") — prefer it.
                logger.info(
                    f"[HolderDiag] {job.context}: resolving with cross-validated "
                    f"empty result after exhausting all {total_cycles} rotation(s)"
                )
                if not job.future.done():
                    job.future.set_result(job.best_empty_result)
                return

            logger.warning(
                f"Request gave up after {total_cycles} rotation(s) through "
                f"{len(eligible)} eligible provider(s): {job.context}"
            )
            if not job.future.done():
                job.future.set_result(None)

        finally:
            if slot_held:
                await self._release_concurrency_slot()

    async def _dispatch_to_provider(self, job: "_Job", provider_name: str) -> str:
        """
        Attempt dispatch to a specific provider.

        Returns one of _OUTCOME_SUCCESS / _OUTCOME_EMPTY_SUCCESS /
        _OUTCOME_FAILURE — see the constants' docstrings above. Every
        branch that used to `job.future.set_result(...)` and `return True`
        for a "we're done, this is final" case now returns
        _OUTCOME_SUCCESS instead; every `return False` becomes
        _OUTCOME_FAILURE. The only new branch is the empty-result check
        just before the old unconditional success, gated on
        job.retry_on_empty_result so every non-holder caller is
        byte-for-byte unaffected.
        """
        session = await self._get_session()
        url = self._build_provider_url(provider_name)
        headers = self._get_provider_headers(provider_name)
        timeout = aiohttp.ClientTimeout(total=job.timeout or _DEFAULT_TIMEOUT_SECONDS)

        job.start_time_ms = time.monotonic() * 1000

        try:
            kwargs: dict = {"params": job.params, "timeout": timeout, "headers": headers}
            if job.method == "POST":
                kwargs["json"] = job.json_body

            caller = session.post if job.method == "POST" else session.get

            async with caller(url, **kwargs) as resp:
                elapsed_ms = (time.monotonic() * 1000) - job.start_time_ms

                if resp.status == 200:
                    logger.info(
                        f"[HolderDiag] {provider_name}: HTTP 200 received for "
                        f"{job.context} in {elapsed_ms:.0f}ms — parsing response"
                    )
                    try:
                        data = await resp.json(content_type=None)
                    except Exception as e:
                        # A 200 with a body that isn't valid JSON is a provider
                        # failure, not a usable result. Previously this fell
                        # through to job.future.set_result(None) + `return True`,
                        # which told the caller "done, final answer is None" and
                        # permanently stopped the failover rotation before the
                        # remaining eligible providers were ever tried.
                        logger.warning(
                            f"[HolderDiag] {provider_name}: JSON parse FAILED for "
                            f"{job.context}: {e} — treating as provider failure, "
                            f"continuing failover"
                        )
                        return _OUTCOME_FAILURE

                    # HTTP 200 only confirms the transport succeeded. Per the
                    # JSON-RPC 2.0 spec, an application-level failure (method
                    # not supported on this plan, response too large, etc.) is
                    # still returned as HTTP 200 with a top-level "error"
                    # object. Previously this was accepted as a final,
                    # successful result and rotation stopped here — meaning a
                    # provider that couldn't fulfill getProgramAccounts (a
                    # commonly plan-restricted call) silently killed the whole
                    # request instead of failing over, even though later
                    # providers (Alchemy, dRPC) were never given a chance. This
                    # is the actual implementation of the "fails over to the
                    # next provider exactly like any other getProgramAccounts
                    # call" behavior already documented in services/holders.py.
                    if isinstance(data, dict) and data.get("error"):
                        logger.warning(
                            f"[HolderDiag] {provider_name}: JSON-RPC error for "
                            f"{job.context}: {data['error']} — treating as "
                            f"provider failure, continuing failover"
                        )
                        return _OUTCOME_FAILURE

                    result_len = len(data.get("result") or []) if isinstance(data, dict) else None

                    # An HTTP-200, error-free response whose "result" is an
                    # empty list is genuinely ambiguous for a scan-style call
                    # like getProgramAccounts: it could mean "this token
                    # really has zero holder accounts right now" (expected
                    # for brand-new Pump.fun mints) or "this provider's
                    # plan silently can't/won't run this scan and returns
                    # an empty array instead of an error". Opted-in callers
                    # (retry_on_empty_result=True) get the benefit of the
                    # doubt withheld until the rest of the eligible
                    # providers have had a chance to disagree — see
                    # _dispatch's cross-validation step. Every other caller
                    # keeps the original "any error-free 200 is final"
                    # behavior untouched.
                    if (
                        job.retry_on_empty_result
                        and isinstance(data, dict)
                        and isinstance(data.get("result"), list)
                        and result_len == 0
                    ):
                        self._note_success(provider_name, elapsed_ms)
                        logger.info(
                            f"[HolderDiag] {provider_name}: HTTP 200, valid response, "
                            f"but 0 result entries for {job.context} — provider is "
                            f"healthy but this is a suspicious empty response for a "
                            f"scan call; deferring to cross-validation against the "
                            f"remaining eligible provider(s) instead of accepting it "
                            f"as final immediately."
                        )
                        if job.best_empty_result is None:
                            job.best_empty_result = data
                            job.best_empty_provider = provider_name
                        return _OUTCOME_EMPTY_SUCCESS

                    self._note_success(provider_name, elapsed_ms)
                    logger.info(
                        f"[HolderDiag] {provider_name}: request succeeded for "
                        f"{job.context}"
                        + (f" ({result_len} result entries)" if result_len is not None else "")
                        + " — returning to caller, rotation stops here."
                    )
                    if not job.future.done():
                        job.future.set_result(data)
                    return _OUTCOME_SUCCESS

                if resp.status == 429:
                    self._note_rate_limit(provider_name)
                    return _OUTCOME_FAILURE

                if resp.status in (500, 502, 503, 504):
                    logger.warning(f"{provider_name} HTTP {resp.status} for {job.context}")
                    return _OUTCOME_FAILURE

                body_text = await resp.text()

                # Provider Resilience fix (2026-08-28): every remaining
                # status code used to fall through to `job.future.set_
                # result(None)` + `return _OUTCOME_SUCCESS` — i.e. treated
                # as a FINAL, successful answer of "no data". For a 401/
                # 402/403 (bad key, plan restriction, out-of-credits — the
                # exact failure mode Ankr's premium endpoint hits, see
                # config/settings.py ANKR_API_KEY / RPC_PROVIDER_PRIORITY)
                # that was doubly wrong: it never counted toward this
                # provider's consecutive-failure/circuit-breaker tracking
                # (so a provider stuck returning 403 on every call was
                # never taken out of rotation and kept being hit on every
                # request), AND it stopped the whole job right there
                # instead of failing over to the next eligible provider —
                # even when Helius/QuickNode/Alchemy/dRPC still had a turn
                # left in this same rotation. Any other unexpected status
                # (400/404/418/...) has the same two problems and gets the
                # same fix: it is a genuine per-attempt failure, so it
                # penalizes this provider's health/circuit-breaker state
                # (via the _OUTCOME_FAILURE handling in _dispatch) and lets
                # the next eligible provider be tried, instead of silently
                # ending the request. This does not change the final
                # outcome for a caller when every eligible provider is
                # unhealthy — the job still ends in None, just after a
                # real failover attempt instead of skipping it, and the
                # circuit breaker now actually reflects what happened.
                logger.warning(f"{provider_name} HTTP {resp.status} for {job.context}: {body_text[:200]}")
                return _OUTCOME_FAILURE

        except asyncio.TimeoutError:
            stats = self._provider_stats.get(provider_name)
            if stats:
                stats.timeouts += 1
            logger.warning(f"{provider_name} timeout for {job.context}")
            return _OUTCOME_FAILURE

        except aiohttp.ClientError as e:
            logger.warning(f"{provider_name} network error for {job.context}: {e}")
            return _OUTCOME_FAILURE

        except Exception as e:
            logger.error(f"{provider_name} unexpected error for {job.context}: {e}")
            if not job.future.done():
                job.future.set_result(None)
            return _OUTCOME_SUCCESS

    def _note_rate_limit(self, provider_name: str) -> None:
        """Track rate-limit response."""
        stats = self._provider_stats.get(provider_name)
        if stats:
            stats.rate_limited_responses += 1

        now = time.monotonic()
        if now - self._rl_window_start > _RATE_LIMIT_LOG_INTERVAL_SECONDS:
            if self._rl_hits_since_log:
                logger.warning(
                    f"Rate-limited (HTTP 429) {self._rl_hits_since_log} request(s) in "
                    f"~{int(_RATE_LIMIT_LOG_INTERVAL_SECONDS)}s — auto-throttling "
                    f"(rate multiplier now {self._rate_multiplier:.2f}x) and retrying with backoff."
                )
            self._rl_window_start = now
            self._rl_hits_since_log = 0
        self._rl_hits_since_log += 1

        self._consecutive_successes = 0
        self._consecutive_429s += 1
        self._rate_multiplier = min(self._rate_multiplier * 1.5, _MAX_RATE_MULTIPLIER)

    def _note_success(self, provider_name: str, elapsed_ms: float) -> None:
        """Track successful response."""
        stats = self._provider_stats.get(provider_name)
        if stats:
            stats.successful_requests += 1
            stats.total_latency_ms += elapsed_ms

        health = self._provider_health.get(provider_name)
        if health:
            health.mark_healthy()

        self._consecutive_429s = 0
        self._consecutive_successes += 1
        if self._rate_multiplier > 1.0 and self._consecutive_successes >= _SUCCESSES_TO_EASE:
            self._consecutive_successes = 0
            self._rate_multiplier = max(1.0, self._rate_multiplier * 0.8)

    def queue_depths(self) -> dict[int, int]:
        """Current number of queued (not-yet-dispatched) jobs per priority level."""
        return {priority: len(bucket) for priority, bucket in self._buckets.items() if bucket}

    def provider_stats(self) -> dict[str, dict]:
        """Get provider statistics for monitoring."""
        stats_dict = {}
        for provider_name, stats in self._provider_stats.items():
            health = self._provider_health.get(provider_name)
            stats_dict[provider_name] = {
                "total_requests": stats.total_requests,
                "successful_requests": stats.successful_requests,
                "failed_requests": stats.failed_requests,
                "rate_limited_responses": stats.rate_limited_responses,
                "timeouts": stats.timeouts,
                "average_latency_ms": stats.average_latency_ms(),
                "success_rate_pct": (
                    (stats.successful_requests / stats.total_requests * 100)
                    if stats.total_requests > 0 else 0.0
                ),
                "circuit_broken": health.is_circuit_broken() if health else False,
            }
        return stats_dict

    # --- Public API (backward compatible) ---

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        priority: int = PRIORITY_LOW,
        cache_key: str | None = None,
        cache_ttl: float = MULTI_RPC_CACHE_TTL_SECONDS,
        timeout: int = _DEFAULT_TIMEOUT_SECONDS,
        context: str = "",
        exclude_providers: list[str] | None = None,
        retry_on_empty_result: bool = False,
    ) -> Any:
        """
        Queue + rate-limit + retry a single RPC call with automatic failover,
        with an optional shared cache lookup/store.

        exclude_providers: optional list of provider keys ("helius",
        "quicknode", "alchemy", "drpc") to skip for THIS call only. Does
        not affect RPC_PROVIDER_PRIORITY or any other in-flight/future job
        — e.g. Signal/Quote Alert calls can scope out "drpc" while wallet
        and portfolio calls keep the full failover chain untouched.

        retry_on_empty_result: when True, an HTTP-200/error-free response
        whose "result" is an empty list is NOT accepted as final the
        moment it's seen. Instead the remaining eligible providers are
        tried too; only once every reachable eligible provider has either
        failed or agreed on "empty" is the empty result returned. Default
        False preserves the original "first error-free 200 wins" behavior
        for every existing caller. Intended for scan-style calls (e.g.
        getProgramAccounts) where a provider-side plan/add-on restriction
        can silently return an empty array instead of an error.

        Returns the parsed JSON body (dict or list) on success, or None
        if the request could not be completed. Never raises.
        """
        if cache_key:
            cached = self.get_cached(cache_key)
            if cached is not None:
                return cached

        # Check deduplication cache
        dedup_key = self._request_dedup_key(method, url, json_body)
        existing_future = self._check_dedup_cache(dedup_key)
        if existing_future is not None:
            try:
                return await existing_future
            except Exception:
                pass

        self._ensure_worker()

        job = _Job(
            method.upper(), url, params, json_body, timeout, priority,
            context or url, exclude_providers=exclude_providers,
            retry_on_empty_result=retry_on_empty_result,
        )
        future = job.future
        self._store_dedup_request(dedup_key, future)
        self._enqueue(job)

        # Track request
        for stats in self._provider_stats.values():
            stats.total_requests += 1

        result = await future

        if result is not None and cache_key:
            self.set_cached(cache_key, result, cache_ttl)

        return result


# Process-wide singleton. Every RPC-calling module imports this same
# instance so ALL RPC traffic funnels through one queue/limiter/cache.
multi_rpc_manager = MultiRPCManager()

# Backward compatibility alias
helius_manager = multi_rpc_manager
