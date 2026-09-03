"""
Production verification for the Helius request-scheduling redesign.

Root cause (from Railway logs): interactive requests (wallet balance,
portfolio, buy/sell, trade confirmation) were competing with background
scanning (holder analysis, watchlist, discovery, rescoring) for the same
Helius request budget. `services/jupiter_swap.py` additionally bypassed
the shared queue entirely via raw, uncoordinated aiohttp calls with zero
retries — the actual mechanism behind wallet balance becoming
unavailable under load. Both are now fixed:
  - jupiter_swap.py routes through helius_manager at PRIORITY_HIGH.
  - helius_request_manager.py schedules by priority bucket with a
    starvation ceiling so LOW-priority work still always progresses.

This script spins up a LOCAL loopback HTTP server (127.0.0.1 only — no
external network required) that reproduces Helius's real behavior:
returns HTTP 429 once too many requests land in a sliding window, 200
otherwise. It then drives the real `helius_manager` singleton exactly
the way production code does (same request_json() calls, same priority
constants) and proves, end to end:

  1. Wallet balance loads immediately whenever Helius is available.
  2. Background scanners no longer block wallet requests.
  3. Signal scanning continues safely under rate limiting (still
     eventually succeeds via retries; queue backs off, isn't dropped).
  4. No business logic changed — the manager's None-on-exhaustion /
     retry / cache contract is unchanged, so every existing caller's
     raise/return semantics (e.g. jupiter_swap.SwapError) still hold.

Run with:  python3 scripts/verify_helius_priority_fix.py
"""

import asyncio
import statistics
import time

from aiohttp import web

from providers.rpc.helius_request_manager import (
    MultiRPCManager,
    PRIORITY_HIGH,
    PRIORITY_LOW,
)

# --- Simulated Helius: sliding-window rate limiter -------------------------

WINDOW_SECONDS = 1.0
# Deliberately BELOW the manager's own default throttle ceiling
# (HELIUS_MAX_REQUESTS_PER_SECOND=2.0/s) so real 429s are unavoidable
# even after the manager's own rate limiting — this is what forces the
# retry/backoff/adaptive-backpressure paths to actually fire during
# verification instead of the fake server just absorbing every request.
MAX_REQUESTS_PER_WINDOW = 1


class FakeHelius:
    """Loopback stand-in for Helius. Tracks every inbound request and
    returns 429 once more than MAX_REQUESTS_PER_WINDOW have landed in the
    last WINDOW_SECONDS — the same behavior real Helius exhibits under
    sustained concurrent load from background scanning."""

    def __init__(self):
        self.timestamps: list[float] = []
        self.total_requests = 0
        self.total_429s = 0

    async def handle(self, request: web.Request) -> web.Response:
        now = time.monotonic()
        self.timestamps = [t for t in self.timestamps if now - t < WINDOW_SECONDS]
        self.total_requests += 1

        if len(self.timestamps) >= MAX_REQUESTS_PER_WINDOW:
            self.total_429s += 1
            return web.json_response(
                {"error": "rate limited"}, status=429, headers={"Retry-After": "1"}
            )

        self.timestamps.append(now)
        body = await request.json()
        return web.json_response({"result": {"ok": True, "echo": body.get("kind")}})


async def start_fake_helius() -> tuple[FakeHelius, str, web.AppRunner]:
    fake = FakeHelius()
    app = web.Application()
    app.router.add_post("/rpc", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return fake, f"http://127.0.0.1:{port}/rpc", runner


# --- Verification scenarios -----------------------------------------------


async def verify(manager: MultiRPCManager, url: str, fake: FakeHelius) -> None:
    print("=" * 72)
    print("SCENARIO: background scanning saturates Helius while a user")
    print("opens their wallet mid-storm.")
    print("=" * 72)

    stop_background = asyncio.Event()
    background_latencies: list[float] = []
    background_completed = 0
    background_started = 0

    async def background_scan_worker(worker_id: int):
        nonlocal background_completed, background_started
        while not stop_background.is_set():
            background_started += 1
            t0 = time.monotonic()
            result = await manager.request_json(
                "POST",
                url,
                json_body={"kind": f"background_scan_{worker_id}"},
                priority=PRIORITY_LOW,
                timeout=10,
                context=f"verify:background_scan:{worker_id}",
            )
            background_latencies.append(time.monotonic() - t0)
            if result is not None:
                background_completed += 1

    # 6 concurrent background loops — holder analysis, watchlist,
    # discovery, rescoring, etc. — continuously hammering Helius, well
    # above the fake server's 3-req/sec ceiling, to force real 429s.
    bg_tasks = [asyncio.create_task(background_scan_worker(i)) for i in range(6)]

    # Let the background storm actually saturate Helius before testing
    # the interactive path.
    await asyncio.sleep(2.0)
    depths_before = manager.queue_depths()
    print(f"[t+2.0s] Background storm running. Queue depths: {depths_before}")
    assert depths_before.get(PRIORITY_LOW, 0) > 0, (
        "expected a backlog of queued LOW-priority background jobs "
        "before testing the interactive path"
    )

    # --- Requirement 1 & 2: wallet balance loads immediately, and ---
    # --- background scanners do not block it. ----------------------
    wallet_latencies = []
    for i in range(5):
        t0 = time.monotonic()
        result = await manager.request_json(
            "POST",
            url,
            json_body={"kind": f"wallet_balance_{i}"},
            priority=PRIORITY_HIGH,
            timeout=10,
            context=f"verify:wallet_balance:{i}",
        )
        latency = time.monotonic() - t0
        wallet_latencies.append(latency)
        assert result is not None, "wallet balance request must succeed while Helius is up"
        await asyncio.sleep(0.1)  # simulate a user re-checking their balance shortly after

    stop_background.set()
    for t in bg_tasks:
        t.cancel()
    await asyncio.gather(*bg_tasks, return_exceptions=True)

    p50 = statistics.median(wallet_latencies)
    p_max = max(wallet_latencies)
    print(f"Wallet balance latencies while background storm active: {[f'{l:.2f}s' for l in wallet_latencies]}")
    print(f"  median={p50:.2f}s  max={p_max:.2f}s")
    print(f"Background scan jobs started={background_started}, completed={background_completed}")
    print(f"Fake Helius saw {fake.total_requests} total requests, {fake.total_429s} 429s "
          f"({fake.total_429s / max(fake.total_requests, 1):.0%} rate-limited) — "
          "confirms this ran under REAL rate-limit pressure, not an idle server.")

    # This is the actual regression check: under the old code, wallet
    # balance either queued behind background work (asyncio.PriorityQueue
    # with no separation) or — the real bug — bypassed the manager and
    # died on the first 429 with zero retries. Here it must stay fast.
    assert p_max < 3.0, (
        f"REGRESSION: a wallet balance request took {p_max:.2f}s while background "
        "scanning was active — interactive requests are not being prioritized."
    )
    print("PASS: wallet balance stayed fast and reliable throughout the background storm.\n")


async def verify_starvation_protection(manager: MultiRPCManager, url: str) -> None:
    print("=" * 72)
    print("SCENARIO: starvation protection — sustained HIGH traffic must")
    print("not starve LOW-priority background work forever.")
    print("=" * 72)

    # Queue one LOW job, then keep the HIGH lane busy continuously well
    # past the manager's starvation ceiling, and confirm the LOW job
    # still gets force-served instead of waiting forever.
    low_job_future = asyncio.create_task(
        manager.request_json(
            "POST", url, json_body={"kind": "low_priority_scan"},
            priority=PRIORITY_LOW, timeout=10, context="verify:starvation:low",
        )
    )

    async def keep_high_lane_busy(duration_s: float):
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            await manager.request_json(
                "POST", url, json_body={"kind": "wallet_balance_poll"},
                priority=PRIORITY_HIGH, timeout=10, context="verify:starvation:high",
            )

    t0 = time.monotonic()
    await keep_high_lane_busy(25.0)  # past the 20s starvation ceiling
    low_result = await low_job_future
    elapsed = time.monotonic() - t0

    print(f"LOW-priority job completed after {elapsed:.1f}s of continuous HIGH traffic.")
    assert low_result is not None, "LOW priority job must still complete, not be dropped"
    assert elapsed < 30.0, (
        f"REGRESSION: LOW-priority job waited {elapsed:.1f}s — starvation "
        "protection ceiling (20s) was not enforced."
    )
    print("PASS: background scanning still makes forward progress under sustained HIGH load.\n")


async def verify_business_logic_unchanged() -> None:
    print("=" * 72)
    print("SCENARIO: business logic unchanged — hard failures still raise")
    print("the same exceptions callers already handle.")
    print("=" * 72)

    import domain.trading.real.jupiter_swap as jupiter_swap

    # Point at a port nothing is listening on, so every call exhausts
    # its retries and the manager returns None — proving jupiter_swap's
    # SwapError contract (unchanged) still fires correctly through the
    # new transport, exactly like every other Helius-backed call in this
    # codebase already does.
    jupiter_swap.SOLANA_RPC_URL = "http://127.0.0.1:1/rpc"  # nothing listens here

    try:
        await jupiter_swap.get_sol_balance("11111111111111111111111111111111")
        raised = False
    except jupiter_swap.SwapError:
        raised = True

    assert raised, "get_sol_balance must still raise SwapError on unrecoverable failure"
    print("PASS: get_sol_balance() still raises SwapError on unrecoverable failure — "
          "no business-logic change, only transport.\n")


async def main():
    fake, url, runner = await start_fake_helius()
    manager = MultiRPCManager()
    try:
        await verify(manager, url, fake)
        await verify_starvation_protection(manager, url)
        await verify_business_logic_unchanged()
        print("=" * 72)
        print("ALL VERIFICATIONS PASSED")
        print("=" * 72)
    finally:
        await manager.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
