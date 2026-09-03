"""
Fail-closed re-validation queue — Production Validation Policy.

A token must never be promoted to a signal when a MANDATORY validation
check could not be completed (API failure, HTTP 429 rate limit, network
or RPC error, timeout, or any other unknown/unverifiable state). Missing
or unavailable security/data is never treated as neutral or passing.

Instead of discarding such a candidate outright, services/pump_radar.py
rejects it for the current scan cycle and hands its contract address to
this module, which tracks it so it is automatically folded back into a
later scan cycle and given a full, fresh pass through every mandatory
check again — never a shortcut or partial re-check. A candidate only
ever becomes a signal once every mandatory validation has completed
successfully on its own merits.

This module intentionally contains no scoring, security, or hard-reject
logic of its own — it only tracks which contracts are owed a retry, and
for how long, so it cannot change or weaken any existing validation
decision elsewhere in the pipeline.
"""

import logging
import time

try:
    from config.settings import HELIUS_HOLDER_CACHE_TTL_SECONDS
except ImportError:
    HELIUS_HOLDER_CACHE_TTL_SECONDS = 420.0

logger = logging.getLogger("AlphaPulse.ValidationQueue")

# A contract stays eligible for automatic re-validation for up to this
# long. This is not a decision to "pass" or "reject" the token itself —
# a contract that still cannot be validated after this long simply stops
# being retried, which leaves it exactly where it already was: never
# promoted to a signal.
MAX_PENDING_AGE_SECONDS = 6 * 3600  # 6 hours

# Minimum time a contract must wait after a failed attempt before it's
# eligible to be folded back into another scan cycle.
#
# Profiling (see Helius consumer audit) showed re-validation was the
# single biggest amplifier of Helius rate-limit storms: a contract that
# failed holder-analysis due to Helius unavailability was previously
# re-queued and retried on literally the very next Pump Radar cycle
# (~45s later) — while Helius was still recovering from the same
# rate-limit event that caused the original failure — and each retry
# attempt is itself uncached (holder data only caches on success) and
# can burn up to HELIUS_MAX_RETRIES+1 HTTP calls on its own. That is a
# direct feedback loop: our own retries were extending the outage we
# were reacting to.
#
# 2x HELIUS_HOLDER_CACHE_TTL_SECONDS — enough breathing room for the
# shared Helius rate limiter's adaptive backoff to ease back down (see
# helius_request_manager.py's `_SUCCESSES_TO_EASE`), while remaining
# trivially small next to the 6-hour expiry above, so no candidate is
# ever dropped or delayed enough to matter for detection — it's simply
# not re-hammered every cycle.
#
# Derived from the actual TTL setting rather than a hardcoded number so
# the two can't silently drift apart again (this constant previously
# hardcoded 180s next to a comment claiming "2x a 90s TTL" while the
# real configured TTL was 420s — a >2x discrepancy from what the
# comment described).
REVALIDATION_COOLDOWN_SECONDS = int(HELIUS_HOLDER_CACHE_TTL_SECONDS * 2)

# Safety cap so a prolonged provider outage can't grow this in-memory
# queue without bound. Oldest entries are evicted first.
MAX_QUEUE_SIZE = 500

_pending: dict[str, dict] = {}


def schedule_revalidation(contract: str, reason: str) -> None:
    """
    Record that `contract` was rejected this cycle solely because a
    mandatory check's data was unavailable, and should be re-attempted
    from scratch on a later scan cycle. Safe to call repeatedly for the
    same contract — updates the existing entry in place.
    """
    if not contract:
        return

    now = time.time()
    entry = _pending.get(contract)
    if entry:
        entry["attempts"] += 1
        entry["last_reason"] = reason
        entry["last_attempt_at"] = now
    else:
        if len(_pending) >= MAX_QUEUE_SIZE:
            _evict_oldest()
        _pending[contract] = {
            "first_seen_at": now,
            "last_attempt_at": now,
            "attempts": 1,
            "last_reason": reason,
        }

    logger.info(
        f"Queued {contract[:8]} for re-validation ({reason}); "
        f"attempt {_pending[contract]['attempts']}"
    )


def clear_revalidation(contract: str) -> None:
    """
    Remove a contract from the queue. Call this once the mandatory
    checks that previously could not be completed for it have actually
    been completed (whether the candidate goes on to pass or to be
    hard-rejected for a genuine, verified reason) — so it is not retried
    forever for a data gap that no longer exists.
    """
    _pending.pop(contract, None)


def get_pending_contracts(limit: int | None = None) -> list[str]:
    """
    Contracts currently owed a re-validation pass. Expired entries
    (older than MAX_PENDING_AGE_SECONDS) are dropped first, then entries
    still within REVALIDATION_COOLDOWN_SECONDS of their last attempt are
    skipped for this call — see that constant's docstring: retrying a
    contract every single cycle while the very API it needs is still
    recovering just re-triggers the same rate-limit event that caused
    the original failure. The contract stays in the queue and is simply
    reconsidered on a later call once its cooldown has elapsed; it is
    never dropped or double-counted by this filtering.

    If `limit` is given, returns at most that many contracts, ordered by
    least-recently-attempted first (fair rotation). Without a limit, every
    scan cycle was folding the ENTIRE pending backlog on top of that
    cycle's brand-new candidates — with a shared, finite downstream API
    budget (Helius), that backlog only ever grows (nothing can ever clear
    faster than new rejections arrive), so every subsequent cycle got
    strictly heavier than the last with no way to ever catch up. Capping
    per cycle keeps total per-cycle demand bounded and gives every pending
    contract a fair, periodic shot at re-validation instead of an
    ever-growing queue drowning out fresh candidates indefinitely.
    """
    _expire_stale()
    now = time.time()
    eligible = [
        (c, e) for c, e in _pending.items()
        if now - e["last_attempt_at"] >= REVALIDATION_COOLDOWN_SECONDS
    ]
    contracts = sorted(eligible, key=lambda kv: kv[1]["last_attempt_at"])
    ordered = [c for c, _ in contracts]
    if limit is not None:
        return ordered[:limit]
    return ordered


def pending_count() -> int:
    return len(_pending)


def _expire_stale() -> None:
    now = time.time()
    expired = [
        c for c, e in _pending.items()
        if now - e["first_seen_at"] > MAX_PENDING_AGE_SECONDS
    ]
    for c in expired:
        logger.info(
            f"Dropping {c[:8]} from re-validation queue "
            f"(still unverifiable after {MAX_PENDING_AGE_SECONDS // 3600}h)"
        )
        _pending.pop(c, None)


def _evict_oldest() -> None:
    if not _pending:
        return
    oldest_contract = min(_pending.items(), key=lambda kv: kv[1]["first_seen_at"])[0]
    _pending.pop(oldest_contract, None)
