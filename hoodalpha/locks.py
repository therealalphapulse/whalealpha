"""
hoodalpha/locks.py

Deliberate copy of infra/locks.py's Redis leader-election algorithm --
not an import of it. Two reasons:

  1. Zero Python import coupling to WhaleAlpha's code, per the
     "completely separate and independent" requirement -- a change to
     infra/locks.py (even a bugfix) cannot silently change HoodAlpha's
     behavior without HoodAlpha's own code being edited too.
  2. Its own key prefix (HOOD_LOCK_PREFIX = "hoodalpha:") guarantees
     HoodAlpha's lease keys can never collide with WhaleAlpha's
     ("loop:alert_engine", "loop:pump_radar", etc.) even though both
     read the same Redis instance.

Same safe-failure-mode as the original: a lost/unavailable lock means a
skipped cycle, never a duplicate run.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from hoodalpha.settings import REDIS_URL, HOOD_LOCK_PREFIX

logger = logging.getLogger("HoodAlpha.Locks")

_in_memory_locks: dict[str, tuple[float, str]] = {}
_in_memory_guard = asyncio.Lock()


def _key(name: str) -> str:
    return f"{HOOD_LOCK_PREFIX}lock:{name}"


async def try_acquire_lock(key: str, ttl_seconds: int = 60) -> str | None:
    token = uuid.uuid4().hex

    if REDIS_URL:
        try:
            import redis.asyncio as redis

            client = redis.from_url(REDIS_URL, decode_responses=True)
            acquired = await client.set(_key(key), token, nx=True, ex=ttl_seconds)
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
            # Fail closed, same rationale as WhaleAlpha's infra/locks.py.
            return None

    async with _in_memory_guard:
        now = time.monotonic()
        existing = _in_memory_locks.get(key)
        if existing and existing[0] > now:
            return None
        _in_memory_locks[key] = (now + ttl_seconds, token)
        return token


async def renew_lock(key: str, token: str, ttl_seconds: int = 60) -> bool:
    if REDIS_URL:
        try:
            import redis.asyncio as redis

            client = redis.from_url(REDIS_URL, decode_responses=True)
            script = """
                if redis.call('get', KEYS[1]) == ARGV[1] then
                    return redis.call('expire', KEYS[1], ARGV[2])
                else
                    return 0
                end
            """
            result = await client.eval(script, 1, _key(key), token, ttl_seconds)
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
    if REDIS_URL:
        try:
            import redis.asyncio as redis

            client = redis.from_url(REDIS_URL, decode_responses=True)
            script = """
                if redis.call('get', KEYS[1]) == ARGV[1] then
                    return redis.call('del', KEYS[1])
                else
                    return 0
                end
            """
            await client.eval(script, 1, _key(key), token)
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


async def run_as_leader(
    key: str,
    loop_coro_factory,
    *,
    lease_seconds: int = 90,
    renew_interval_seconds: int = 30,
    retry_after_seconds: int = 30,
) -> None:
    """Long-lived leader election for a HoodAlpha background loop. Exactly
    one replica owns the named lease at a time; identical shape to
    WhaleAlpha's infra/locks.py::run_as_leader (see that module for the
    detailed rationale), copied rather than imported."""
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
                        "Lost leadership lease for '%s'; stopping the loop "
                        "to prevent duplicate execution.", key
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
