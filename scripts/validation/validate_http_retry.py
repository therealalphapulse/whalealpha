import asyncio
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "..", "src"))
import _stub_httpx  # noqa: F401 — registers the httpx stub before real import
import _stub_structlog  # noqa: F401 — registers the structlog stub before real import
import httpx

from whale_alpha.utils.http_retry import (  # the REAL, shipped module
    CircuitBreaker,
    TTLCache,
    _mask_url,
    fetch_with_retry,
    get_all_provider_metrics,
    get_provider_client,
)


class FakeResp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class ScriptedClient:
    """Returns queued responses/exceptions in order, one per .request() call."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs.get("params")))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def main():
    print("=" * 70)
    print("TASK 3 — utils/http_retry.py RUNTIME VALIDATION (real module)")
    print("=" * 70)

    # 1. 429 -> Retry-After honored -> success
    client = ScriptedClient([FakeResp(429, {"retry-after": "0"}), FakeResp(200)])
    t0 = time.monotonic()
    result = await fetch_with_retry(client, "GET", "https://api.example.com/x?api-key=SECRET123", max_retries=3, base_backoff_seconds=0.01)
    elapsed = time.monotonic() - t0
    assert result.response.status_code == 200
    assert result.rate_limited is True
    assert len(client.calls) == 2
    print(f"[PASS] 429 + Retry-After=0 -> retried once -> 200 OK  (2 requests made, {elapsed:.3f}s)")

    # 2. 429 exhausting retries -> transient, not permanent
    client2 = ScriptedClient([FakeResp(429), FakeResp(429), FakeResp(429)])
    result2 = await fetch_with_retry(client2, "GET", "https://api.example.com/y", max_retries=2, base_backoff_seconds=0.01)
    assert result2.response is None and result2.transient is True
    print(f"[PASS] 429 exhausting {result2.retried} retries -> transient=True (retry-queue eligible), not a permanent reject")

    # 3. permanent 404 -> no retry at all
    client3 = ScriptedClient([FakeResp(404)])
    result3 = await fetch_with_retry(client3, "GET", "https://api.example.com/z", max_retries=5, base_backoff_seconds=0.01)
    assert result3.response.status_code == 404 and result3.transient is False
    assert len(client3.calls) == 1
    print("[PASS] 404 -> zero retries attempted (permanent, would waste budget)")

    # 4. network error -> transient
    client4 = ScriptedClient([httpx.TransportError("boom"), httpx.TransportError("boom")])
    result4 = await fetch_with_retry(client4, "GET", "https://api.example.com/w", max_retries=1, base_backoff_seconds=0.01)
    assert result4.response is None and result4.transient is True
    print("[PASS] network error -> transient=True")

    # 5. URL masking never leaks the api-key query param
    masked = _mask_url("https://api.example.com/x?api-key=SECRET123")
    assert "SECRET123" not in masked
    print(f"[PASS] URL masking strips credentials before logging: {masked}")

    # 6. TTLCache expiry
    cache = TTLCache(ttl_seconds=0.05)
    cache.set("addr1", ["swap1"])
    assert cache.get("addr1") == ["swap1"]
    await asyncio.sleep(0.08)
    assert cache.get("addr1") is None
    print("[PASS] TTLCache: fresh hit, then expires after ttl_seconds")

    # 7. CircuitBreaker: opens after threshold, fails fast, half-opens after cooldown
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=0.05)
    for _ in range(3):
        assert breaker.allow_request()
        breaker.record_transient_failure()
    assert breaker.is_open is True
    assert breaker.allow_request() is False
    print("[PASS] CircuitBreaker: opened after 3 consecutive transient failures, now failing fast")
    await asyncio.sleep(0.08)
    assert breaker.allow_request() is True  # half-open trial
    breaker.record_success()
    assert breaker.is_open is False
    print("[PASS] CircuitBreaker: half-open trial allowed after cooldown, success closes it again")

    # 8. ProviderClient end-to-end: circuit trips after repeated failures,
    #    stops making real network calls, metrics reflect everything.
    flaky_client = ScriptedClient([FakeResp(500)] * 3 + [FakeResp(200)])
    pc = get_provider_client("demo_provider", max_concurrency=2, failure_threshold=3, cooldown_seconds=1.0)
    for i in range(3):
        r = await pc.get(flaky_client, "https://api.example.com/data", max_retries=0, base_backoff_seconds=0.01)
        assert r.response is None and r.transient is True
    assert pc.breaker.is_open is True
    calls_before = len(flaky_client.calls)
    r_skipped = await pc.get(flaky_client, "https://api.example.com/data", max_retries=0, base_backoff_seconds=0.01)
    assert r_skipped.circuit_open is True
    assert len(flaky_client.calls) == calls_before  # NO network call made
    print("[PASS] ProviderClient: circuit opened after 3x 500s, 4th call skipped network entirely (circuit_open=True)")
    print(f"       metrics: {pc.metrics.as_dict()}")

    # 9. get_provider_client registry + get_all_provider_metrics
    named = get_provider_client("jupiter_trending", max_concurrency=4, failure_threshold=5, cooldown_seconds=60)
    good_client = ScriptedClient([FakeResp(200)])
    await named.get(good_client, "https://api.jup.ag/tokens", max_retries=0, base_backoff_seconds=0.01)
    all_metrics = get_all_provider_metrics()
    assert "jupiter_trending" in all_metrics
    assert "demo_provider" in all_metrics
    print("[PASS] get_all_provider_metrics() (fed into run_discovery_cycle's structured log):")
    for name, m in all_metrics.items():
        print(f"         {name}: {m}")

    # 10. semaphore actually bounds concurrency
    max_concurrent = 0
    current = 0

    class TrackingClient:
        async def request(self, method, url, timeout=None, **kwargs):
            nonlocal max_concurrent, current
            current += 1
            max_concurrent = max(max_concurrent, current)
            await asyncio.sleep(0.02)
            current -= 1
            return FakeResp(200)

    pc2 = get_provider_client("concurrency_test", max_concurrency=2, failure_threshold=99, cooldown_seconds=1)
    tc = TrackingClient()
    await asyncio.gather(*(pc2.get(tc, "https://x", base_backoff_seconds=0.01) for _ in range(8)))
    assert max_concurrent <= 2
    print(f"[PASS] Semaphore bounded 8 concurrent requests to max_concurrency=2 (observed max={max_concurrent})")

    print()
    print("ALL 10 TASK-3 RUNTIME CHECKS PASSED against the real utils/http_retry.py module.")


asyncio.run(main())
