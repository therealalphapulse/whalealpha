"""Unit tests for utils/http_retry.py — the shared 429/5xx/network retry
helper and TTL cache used by the wallet-history retry queue (see
engines/discovery.evaluate_candidates and
integrations/wallet_discovery_source.fetch_wallet_swap_history).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from whale_alpha.utils.http_retry import TTLCache, fetch_with_retry


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
    result = await fetch_with_retry(client, "GET", "https://example.com", max_retries=3, base_backoff_seconds=0)
    assert result.response is not None
    assert result.response.status_code == 200
    assert result.transient is False
    assert client.calls == 1


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds_honoring_retry_after():
    client = _FakeClient([_FakeResponse(429, {"retry-after": "0"}), _FakeResponse(200)])
    result = await fetch_with_retry(client, "GET", "https://example.com", max_retries=3, base_backoff_seconds=0)
    assert result.response is not None
    assert result.response.status_code == 200
    assert client.calls == 2


@pytest.mark.asyncio
async def test_429_exhausting_retries_is_transient_not_permanent():
    client = _FakeClient([_FakeResponse(429), _FakeResponse(429), _FakeResponse(429)])
    result = await fetch_with_retry(client, "GET", "https://example.com", max_retries=2, base_backoff_seconds=0)
    assert result.response is None
    assert result.transient is True
    assert result.rate_limited is True
    assert client.calls == 3  # initial attempt + 2 retries


@pytest.mark.asyncio
async def test_permanent_4xx_is_not_retried():
    client = _FakeClient([_FakeResponse(404)])
    result = await fetch_with_retry(client, "GET", "https://example.com", max_retries=3, base_backoff_seconds=0)
    assert result.response is not None
    assert result.response.status_code == 404
    assert result.transient is False
    assert client.calls == 1  # no retry attempted at all


@pytest.mark.asyncio
async def test_network_error_is_transient():
    client = _FakeClient([httpx.ConnectError("boom"), httpx.ConnectError("boom")])
    result = await fetch_with_retry(client, "GET", "https://example.com", max_retries=1, base_backoff_seconds=0)
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
            fetch_with_retry(client, "GET", "https://example.com", semaphore=semaphore, base_backoff_seconds=0)
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
