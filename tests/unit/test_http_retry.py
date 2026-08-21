"""Unit tests for utils/http_retry.py — the shared 429/5xx/network retry
helper and TTL cache used by the wallet-history retry queue (see
engines/discovery.evaluate_candidates and
integrations/wallet_discovery_source.fetch_wallet_swap_history).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from whale_alpha.utils.http_retry import (
    CircuitBreaker,
    ProviderClient,
    TTLCache,
    fetch_with_retry,
    get_all_provider_metrics,
    get_provider_client,
    mask_headers_for_log,
)


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class _FakeClient:
    """Stands in for httpx.AsyncClient — returns/raises whatever's queued,
    one item per call to `.request(...)`, and records how many times it was
    called so tests can assert on retry counts.
    """

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def request(self, method, url, timeout=None, **kwargs):  # noqa: ANN001
        self.calls += 1
        item = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
async def test_succeeds_immediately_on_200():
    client = _FakeClient([_FakeResponse(200)])
    result = await fetch_with_retry(
        client, "GET", "https://example.com", max_retries=3, base_backoff_seconds=0
    )
    assert result.response is not None
    assert result.response.status_code == 200
    assert result.transient is False
    assert client.calls == 1


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds_honoring_retry_after():
    client = _FakeClient([_FakeResponse(429, {"retry-after": "0"}), _FakeResponse(200)])
    result = await fetch_with_retry(
        client, "GET", "https://example.com", max_retries=3, base_backoff_seconds=0
    )
    assert result.response is not None
    assert result.response.status_code == 200
    assert client.calls == 2


@pytest.mark.asyncio
async def test_429_exhausting_retries_is_transient_not_permanent():
    client = _FakeClient([_FakeResponse(429), _FakeResponse(429), _FakeResponse(429)])
    result = await fetch_with_retry(
        client, "GET", "https://example.com", max_retries=2, base_backoff_seconds=0
    )
    assert result.response is None
    assert result.transient is True
    assert result.rate_limited is True
    assert client.calls == 3  # initial attempt + 2 retries


@pytest.mark.asyncio
async def test_permanent_4xx_is_not_retried():
    client = _FakeClient([_FakeResponse(404)])
    result = await fetch_with_retry(
        client, "GET", "https://example.com", max_retries=3, base_backoff_seconds=0
    )
    assert result.response is not None
    assert result.response.status_code == 404
    assert result.transient is False
    assert client.calls == 1  # no retry attempted at all


@pytest.mark.asyncio
async def test_network_error_is_transient():
    client = _FakeClient([httpx.ConnectError("boom"), httpx.ConnectError("boom")])
    result = await fetch_with_retry(
        client, "GET", "https://example.com", max_retries=1, base_backoff_seconds=0
    )
    assert result.response is None
    assert result.transient is True
    assert client.calls == 2


@pytest.mark.asyncio
async def test_semaphore_bounds_concurrency():
    max_concurrent = 0
    current = 0
    semaphore = asyncio.Semaphore(2)

    class _TrackingClient:
        async def request(self, method, url, timeout=None, **kwargs):  # noqa: ANN001
            nonlocal max_concurrent, current
            current += 1
            max_concurrent = max(max_concurrent, current)
            await asyncio.sleep(0.01)
            current -= 1
            return _FakeResponse(200)

    client = _TrackingClient()
    await asyncio.gather(
        *(
            fetch_with_retry(
                client, "GET", "https://example.com", semaphore=semaphore, base_backoff_seconds=0
            )
            for _ in range(6)
        )
    )
    assert max_concurrent <= 2


def test_ttl_cache_expires_entries():
    cache: TTLCache[str] = TTLCache(ttl_seconds=0.05)
    cache.set("addr", "value")
    assert cache.get("addr") == "value"


@pytest.mark.asyncio
async def test_ttl_cache_expires_after_ttl():
    cache: TTLCache[str] = TTLCache(ttl_seconds=0.01)
    cache.set("addr", "value")
    await asyncio.sleep(0.03)
    assert cache.get("addr") is None


# --------------------------------------------------------------------------
# CircuitBreaker — trips after repeated transient failures, fails fast
# during cooldown, allows a half-open trial after it, closes again on
# success. Used by ProviderClient (see below) so a provider that's clearly
# down stops burning retry budget on every candidate in a batch.
# --------------------------------------------------------------------------


def test_circuit_breaker_starts_closed():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    assert breaker.allow_request() is True
    assert breaker.is_open is False


def test_circuit_breaker_opens_after_consecutive_transient_failures():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    for _ in range(3):
        breaker.record_transient_failure()
    assert breaker.is_open is True
    assert breaker.allow_request() is False


def test_circuit_breaker_success_resets_the_failure_count():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    breaker.record_transient_failure()
    breaker.record_transient_failure()
    breaker.record_success()
    breaker.record_transient_failure()
    breaker.record_transient_failure()
    # 2 consecutive failures after the reset — still under threshold of 3.
    assert breaker.is_open is False


@pytest.mark.asyncio
async def test_circuit_breaker_half_opens_after_cooldown():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.02)
    breaker.record_transient_failure()
    assert breaker.allow_request() is False
    await asyncio.sleep(0.04)
    assert breaker.allow_request() is True  # half-open trial permitted
    breaker.record_success()
    assert breaker.is_open is False


# --------------------------------------------------------------------------
# ProviderClient / get_provider_client — the recommended entry point for
# every discovery-source provider (Jupiter, Birdeye, DexScreener, pump.fun,
# LaunchLab, Raydium, Meteora): binds semaphore + circuit breaker + metrics.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_client_trips_breaker_and_skips_network_calls():
    client = _FakeClient([_FakeResponse(500)] * 10)
    provider = ProviderClient(
        "test_provider_trip", max_concurrency=2, failure_threshold=3, cooldown_seconds=60
    )

    for _ in range(3):
        result = await provider.get(client, "https://example.com", max_retries=0, base_backoff_seconds=0)
        assert result.response is None and result.transient is True

    assert provider.breaker.is_open is True
    calls_before = client.calls
    skipped = await provider.get(client, "https://example.com", max_retries=0, base_backoff_seconds=0)
    assert skipped.circuit_open is True
    assert client.calls == calls_before  # no network call made once the breaker is open


@pytest.mark.asyncio
async def test_provider_client_tracks_metrics():
    client = _FakeClient([_FakeResponse(200), _FakeResponse(429), _FakeResponse(200)])
    provider = ProviderClient(
        "test_provider_metrics", max_concurrency=2, failure_threshold=99, cooldown_seconds=60
    )

    await provider.get(client, "https://example.com", max_retries=0, base_backoff_seconds=0)
    await provider.get(client, "https://example.com", max_retries=0, base_backoff_seconds=0)

    assert provider.metrics.requests == 2
    assert provider.metrics.successes == 1
    assert provider.metrics.failures == 1
    assert provider.metrics.rate_limited == 1


def test_get_provider_client_is_a_process_wide_singleton_per_name():
    a = get_provider_client("shared_provider_test", max_concurrency=3)
    b = get_provider_client("shared_provider_test", max_concurrency=99)  # ignored, already created
    assert a is b


@pytest.mark.asyncio
async def test_get_all_provider_metrics_includes_registered_providers():
    provider = get_provider_client("metrics_snapshot_test", max_concurrency=2)
    client = _FakeClient([_FakeResponse(200)])
    await provider.get(client, "https://example.com", max_retries=0, base_backoff_seconds=0)

    snapshot = get_all_provider_metrics()
    assert "metrics_snapshot_test" in snapshot
    assert snapshot["metrics_snapshot_test"]["requests"] >= 1
    assert "circuit_open" in snapshot["metrics_snapshot_test"]


# --------------------------------------------------------------------------
# Security: never log API keys or bearer tokens (query params or headers).
# --------------------------------------------------------------------------


def test_mask_url_strips_query_string_credentials():
    from whale_alpha.utils.http_retry import _mask_url

    masked = _mask_url("https://api.example.com/v0/x?api-key=SECRET123&limit=10")
    assert "SECRET123" not in masked
    assert masked == "https://api.example.com/v0/x"


def test_mask_headers_for_log_redacts_authorization_and_api_key():
    masked = mask_headers_for_log(
        {"Authorization": "Bearer SECRET", "X-API-KEY": "abc123", "Accept": "application/json"}
    )
    assert masked["Authorization"] == "[REDACTED]"
    assert masked["X-API-KEY"] == "[REDACTED]"
    assert masked["Accept"] == "application/json"


def test_mask_headers_for_log_handles_none():
    assert mask_headers_for_log(None) == {}


@pytest.mark.asyncio
async def test_server_530_is_not_retried_across_every_cycle():
    client = _FakeClient([_FakeResponse(530)] * 10)
    provider = ProviderClient("pumpfun_530", max_concurrency=1, failure_threshold=1, cooldown_seconds=60)
    result = await provider.get(client, "https://pump.fun", max_retries=1, base_backoff_seconds=0)
    assert result.response is None and result.transient is True
    assert client.calls == 2
    skipped = await provider.get(client, "https://pump.fun", max_retries=1, base_backoff_seconds=0)
    assert skipped.circuit_open is True
    assert client.calls == 2
