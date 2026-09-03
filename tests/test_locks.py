"""
tests/test_locks.py

Part of the v4.0 test-suite bootstrap. Covers infra/locks.py — the
correctness-critical primitive behind workers/ not double-executing real
trades across multiple replicas (Bible §5/§11). Runs entirely on the
in-memory fallback backend, no Redis required, so it runs anywhere,
including this sandbox.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra.locks import try_acquire_lock, release_lock, singleton_job, run_as_leader  # noqa: E402


async def test_second_acquire_blocked_while_first_holds_lock():
    t1 = await try_acquire_lock("k1", ttl_seconds=5)
    t2 = await try_acquire_lock("k1", ttl_seconds=5)
    assert t1 is not None
    assert t2 is None
    await release_lock("k1", t1)


async def test_acquire_succeeds_again_after_release():
    t1 = await try_acquire_lock("k2", ttl_seconds=5)
    await release_lock("k2", t1)
    t2 = await try_acquire_lock("k2", ttl_seconds=5)
    assert t2 is not None
    await release_lock("k2", t2)


async def test_release_with_wrong_token_is_a_noop():
    t1 = await try_acquire_lock("k3", ttl_seconds=5)
    await release_lock("k3", "not-the-real-token")
    # lock should still be held by t1's token
    t2 = await try_acquire_lock("k3", ttl_seconds=5)
    assert t2 is None
    await release_lock("k3", t1)


async def test_singleton_job_context_manager():
    async with singleton_job("k4", ttl_seconds=5) as acquired:
        assert acquired is True
        async with singleton_job("k4", ttl_seconds=5) as acquired2:
            assert acquired2 is False


async def test_leader_election_blocks_second_replica():
    async def forever():
        await asyncio.sleep(1000)

    task = asyncio.create_task(
        run_as_leader("k5", lambda: forever(), lease_seconds=2,
                      renew_interval_seconds=1, retry_after_seconds=1)
    )
    await asyncio.sleep(0.1)
    blocked = await try_acquire_lock("k5", ttl_seconds=2)
    assert blocked is None
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_leader_election_recovers_from_crash():
    calls = {"n": 0}

    async def crashes_once_then_hangs():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        await asyncio.sleep(1000)

    task = asyncio.create_task(
        run_as_leader("k6", lambda: crashes_once_then_hangs(), lease_seconds=1,
                      renew_interval_seconds=1, retry_after_seconds=0.1)
    )
    await asyncio.sleep(0.4)
    assert calls["n"] >= 2, "must retry leadership after the wrapped loop crashes"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    tests = [
        test_second_acquire_blocked_while_first_holds_lock,
        test_acquire_succeeds_again_after_release,
        test_release_with_wrong_token_is_a_noop,
        test_singleton_job_context_manager,
        test_leader_election_blocks_second_replica,
        test_leader_election_recovers_from_crash,
    ]

    async def run_all():
        passed = 0
        for t in tests:
            await t()
            passed += 1
            print(f"PASS  {t.__name__}")
        print(f"\n{passed}/{len(tests)} tests passed")

    asyncio.run(run_all())
