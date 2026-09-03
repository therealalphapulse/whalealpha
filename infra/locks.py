"""
infra/locks.py

NEW in v4 (Bible §5 State Management + §11 Background Job Architecture).

The audit found `main.py` starting ~10 `asyncio.create_task` background
loops that assume exactly one process is ever running. That assumption
holds in v3 (it can only run as one process — see the audit's scalability
finding on long-polling + MemoryStorage) but becomes actively dangerous
in v4 once the Signal/Trading and Intelligence worker pools can have more
than one replica: without a lock, every replica would run every job on
its own schedule, which for `real_dca_engine`/`real_exit_engine`/etc.
means double-executing real trades — a correctness bug with real money,
not just an efficiency loss.

`try_acquire_lock` returns an owner token on success or None when another
holder currently owns the lock. The safe failure mode is a missed cycle,
never a duplicate run.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

logger = logging.getLogger("AlphaPulse.Locks")

_in_memory_locks: dict[str, tuple[float, str]] = {}  # key -> (expires_at, owner_token)
_in_memory_guard = asyncio.Lock()


async def try_acquire_lock(key: str, ttl_seconds: int = 60) -> str | None:
    """
    Attempts to acquire a named lock for `ttl_seconds`. Returns an owner
    token on success, or None if another holder currently has it.

    Uses Redis SET NX EX when REDIS_URL is configured (safe across
    multiple replicas/processes); falls back to an in-process lock
    otherwise.
    """
    import os

    redis_url = os.getenv("REDIS_URL", "").strip()
    token = uuid.uuid4().hex

    if redis_url:
        try:
            import redis.asyncio as redis

            client = redis.from_url(redis_url, decode_responses=True)
            acquired = await client.set(f"lock:{key}", token, nx=True, ex=ttl_seconds)
            await client.aclose()
            return token if acquired else None
        except ImportError:
            logger.warning(
                "REDIS_URL is set but the 'redis' package is not installed; "
                "falling back to the in-memory lock, which is NOT safe "
                "across multiple replicas."
            )
        except Exception as e:
            logger.warning("Redis lock acquisition failed for '%s': %s", key, e)
            # Fail closed: do not execute a real-money background job without
            # a functioning distributed lock when Redis is configured.
            return None

    async with _in_memory_guard:
        now = time.monotonic()
        existing = _in_memory_locks.get(key)
        if existing and existing[0] > now:
            return None
        _in_memory_locks[key] = (now + ttl_seconds, token)
        return token


async def renew_lock(key: str, token: str, ttl_seconds: int = 60) -> bool:
    """
    Extends a lock only when the caller still owns it.

    The previous leader-renewal implementation called try_acquire_lock()
    again. Because acquisition correctly uses SET NX, that call could never
    renew an already-held Redis lock. The lease therefore expired after
    90 seconds while the original loop continued running, allowing another
    replica to become leader and potentially double-execute real-money jobs.

    Redis uses an atomic compare-and-expire Lua script so a stale owner can
    never extend another replica's lease.
    """
    import os

    redis_url = os.getenv("REDIS_URL", "").strip()

    if redis_url:
        try:
            import redis.asyncio as redis

            client = redis.from_url(redis_url, decode_responses=True)
            script = """
                if redis.call('get', KEYS[1]) == ARGV[1] then
                    return redis.call('expire', KEYS[1], ARGV[2])
                else
                    return 0
                end
            """
            result = await client.eval(script, 1, f"lock:{key}", token, ttl_seconds)
            await client.aclose()
            return bool(result)
        except ImportError:
            pass
        except Exception as e:
            logger.warning("Redis lock renewal failed for '%s': %s", key, e)
            return False

    async with _in_memory_guard:
        existing = _in_memory_locks.get(key)
        if not existing or existing[1] != token:
            return False
        _in_memory_locks[key] = (time.monotonic() + ttl_seconds, token)
        return True


async def release_lock(key: str, token: str) -> None:
    """Releases a lock only if the caller still owns it."""
    import os

    redis_url = os.getenv("REDIS_URL", "").strip()

    if redis_url:
        try:
            import redis.asyncio as redis

            client = redis.from_url(redis_url, decode_responses=True)
            script = """
                if redis.call('get', KEYS[1]) == ARGV[1] then
                    return redis.call('del', KEYS[1])
                else
                    return 0
                end
            """
            await client.eval(script, 1, f"lock:{key}", token)
            await client.aclose()
            return
        except ImportError:
            pass
        except Exception as e:
            logger.warning("Redis lock release failed for '%s': %s", key, e)
            return

    async with _in_memory_guard:
        existing = _in_memory_locks.get(key)
        if existing and existing[1] == token:
            _in_memory_locks.pop(key, None)


class singleton_job:
    """
    Async context manager wrapping try_acquire_lock/release_lock for the
    common "run this background job cycle only if no other replica is
    already running it" pattern used throughout workers/.
    """

    def __init__(self, key: str, ttl_seconds: int = 60):
        self.key = key
        self.ttl_seconds = ttl_seconds
        self._token: str | None = None

    async def __aenter__(self) -> bool:
        self._token = await try_acquire_lock(self.key, self.ttl_seconds)
        return self._token is not None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._token is not None:
            await release_lock(self.key, self._token)
        return False


async def run_as_leader(
    key: str,
    loop_coro_factory,
    *,
    lease_seconds: int = 90,
    renew_interval_seconds: int = 30,
    retry_after_seconds: int = 30,
) -> None:
    """
    Long-lived leader election for AlphaPulse background loops.

    Exactly one replica owns the named lease at a time. The existing loop
    function remains unchanged; only its ownership is coordinated here.
    """
    while True:
        token = await try_acquire_lock(key, ttl_seconds=lease_seconds)
        if token is None:
            logger.info(
                "Leadership for '%s' held by another replica; retrying in %ss",
                key, retry_after_seconds,
            )
            await asyncio.sleep(retry_after_seconds)
            continue

        logger.info("This replica is now leader for '%s'", key)

        lost_leadership = asyncio.Event()

        async def _renew():
            while not lost_leadership.is_set():
                await asyncio.sleep(renew_interval_seconds)
                renewed = await renew_lock(key, token, ttl_seconds=lease_seconds)
                if not renewed:
                    logger.error(
                        "Lost leadership lease for '%s'; the underlying loop "
                        "will be stopped to prevent duplicate execution.", key
                    )
                    lost_leadership.set()
                    return

        renewal_task = asyncio.create_task(_renew())
        loop_task = asyncio.create_task(loop_coro_factory())

        try:
            done, _ = await asyncio.wait(
                {loop_task, renewal_task}, return_when=asyncio.FIRST_COMPLETED
            )

            if renewal_task in done and lost_leadership.is_set() and not loop_task.done():
                loop_task.cancel()
                try:
                    await loop_task
                except asyncio.CancelledError:
                    pass
            else:
                await loop_task
        except asyncio.CancelledError:
            loop_task.cancel()
            renewal_task.cancel()
            await asyncio.gather(loop_task, renewal_task, return_exceptions=True)
            await release_lock(key, token)
            raise
        except Exception:
            logger.exception("Loop '%s' crashed while leader; retrying leadership", key)
        finally:
            if not renewal_task.done():
                renewal_task.cancel()
            if not loop_task.done():
                loop_task.cancel()
            await asyncio.gather(loop_task, renewal_task, return_exceptions=True)
            await release_lock(key, token)

        await asyncio.sleep(retry_after_seconds)
