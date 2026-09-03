"""
Daily quota governor — Blueprint Section 3.4.

Keeps the 100-150/day target honest without ever touching the hard
rejects in conviction_scorer.hard_reject_reasons(): the only lever this
module moves is the conviction-score cutoff used to decide whether an
already-hard-gate-cleared candidate is "good enough today", and it only
moves within [DYNAMIC_FLOOR, DEFAULT_CUTOFF].

Adaptation note: the blueprint's "rank all qualifying candidates and
send only the top 150" assumes a batch/end-of-day scoring pass. This
bot alerts in real time as candidates clear the pipeline, so the
practical equivalent implemented here is a hard daily send cap
(DAILY_MAX) — once reached, no further alerts go out until the next
UTC day, regardless of how strong a later candidate scores. Sending
fewer than DAILY_MIN on a quiet day is treated as correct behavior,
per Blueprint 2.4, not something this module tries to "fix".

Storage: reuses the existing generic SystemFlag key-value table so no
new migration/table is required.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, func

from infra.db.session import async_session
from models.system_flag import SystemFlag
from models.pump_alerted_token import PumpAlertedToken

logger = logging.getLogger("AlphaPulse.QuotaGovernor")

DAILY_MIN = 100
DAILY_MAX = 150

DEFAULT_CUTOFF = 80.0
DYNAMIC_FLOOR = 70.0
ADJUST_STEP = 2.0
LOOKBACK_DAYS = 5

# Signal Engine re-evaluation: these two numbers are also the exact
# boundaries of the top two Signal Tier bands in scoring.TIER_LABELS —
# DEFAULT_CUTOFF (80) is the floor of "WATCHLIST" and DYNAMIC_FLOOR (70)
# is the floor of "MARGINAL". That's intentional, not a coincidence to
# preserve: the quota governor's whole job is choosing where, within
# that already-tiered range, today's live send bar sits — it should
# never be able to drift outside the band the tier system calls
# meaningfully sendable. If either constant changes, scoring.TIER_LABELS
# needs to change with it.

_HISTORY_KEY = "conviction_qualify_history"      # JSON {date: count}
_CUTOFF_KEY = "conviction_score_cutoff"           # float, current cutoff
_LAST_ADJUST_KEY = "conviction_cutoff_last_adjusted"  # date string


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _get_flag(session, key: str) -> str | None:
    res = await session.execute(select(SystemFlag).where(SystemFlag.key == key))
    row = res.scalar_one_or_none()
    return row.value if row else None


async def _set_flag(session, key: str, value: str) -> None:
    res = await session.execute(select(SystemFlag).where(SystemFlag.key == key))
    row = res.scalar_one_or_none()
    if row:
        row.value = value
    else:
        session.add(SystemFlag(key=key, value=value))


async def get_current_cutoff() -> float:
    """Current conviction-score cutoff. Defaults to 80.0 until/unless
    the rolling-average logic below has lowered it."""
    async with async_session() as session:
        raw = await _get_flag(session, _CUTOFF_KEY)
    try:
        return float(raw) if raw else DEFAULT_CUTOFF
    except (TypeError, ValueError):
        return DEFAULT_CUTOFF


async def record_qualifying_candidate() -> None:
    """
    Call once per candidate that has already cleared every hard gate and
    the conviction-scorer's absolute floor (scoring.HARD_FLOOR_CUTOFF,
    i.e. `pump["eligible"]`) in a scan cycle — "genuinely qualifying" per
    Blueprint 3.1 — regardless of whether it ends up sent (dedup / daily
    cap / the current dynamic cutoff may still block the actual alert).
    This is what the rolling-average adjustment in maybe_adjust_cutoff()
    reads from.

    IMPORTANT: this must be gated on the scorer's fixed *floor*
    (HARD_FLOOR_CUTOFF, 65), not on DEFAULT_CUTOFF (80) or the current
    dynamic cutoff. DEFAULT_CUTOFF is also the top of the cutoff's own
    adjustable range, and real candidates only rarely reach it in
    practice — gating this counter on that same value made the counter
    (and therefore the entire "lower the cutoff when qualifying supply
    is thin" mechanism) circular: the daily history could never
    accumulate entries, maybe_adjust_cutoff() could never see evidence
    the cutoff was too strict, and the cutoff stayed pinned at
    DEFAULT_CUTOFF forever, silently rejecting every eligible candidate
    (see analyze_candidate()'s `final_score < dynamic_cutoff` check).
    Counting at the real eligibility floor instead gives the adjustment
    logic an honest signal of how much qualifying supply actually exists
    each day, while the cutoff itself is still never allowed to move
    outside [DYNAMIC_FLOOR, DEFAULT_CUTOFF] — filtering strictness at
    the actual send gate is unchanged, only the input this mechanism
    reacts to is fixed.
    """
    today = _today()
    async with async_session() as session:
        raw = await _get_flag(session, _HISTORY_KEY)
        try:
            history = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            history = {}

        history[today] = history.get(today, 0) + 1

        # keep it small — only need the last couple weeks
        if len(history) > 14:
            for old_date in sorted(history.keys())[: len(history) - 14]:
                history.pop(old_date, None)

        await _set_flag(session, _HISTORY_KEY, json.dumps(history))
        await session.commit()


async def maybe_adjust_cutoff() -> float:
    """
    Runs the Blueprint 3.4 rolling-average rule: if the last
    LOOKBACK_DAYS days each came in under DAILY_MIN qualifying
    candidates, step the cutoff down by ADJUST_STEP (never below
    DYNAMIC_FLOOR). If the last LOOKBACK_DAYS each cleared DAILY_MIN
    comfortably, step back up toward DEFAULT_CUTOFF. Only evaluated
    once per UTC day. Returns the (possibly updated) cutoff.
    """
    today = _today()

    async with async_session() as session:
        last_adjusted = await _get_flag(session, _LAST_ADJUST_KEY)
        if last_adjusted == today:
            raw_cutoff = await _get_flag(session, _CUTOFF_KEY)
            try:
                return float(raw_cutoff) if raw_cutoff else DEFAULT_CUTOFF
            except (TypeError, ValueError):
                return DEFAULT_CUTOFF

        raw_history = await _get_flag(session, _HISTORY_KEY)
        try:
            history = json.loads(raw_history) if raw_history else {}
        except (TypeError, ValueError):
            history = {}

        raw_cutoff = await _get_flag(session, _CUTOFF_KEY)
        try:
            cutoff = float(raw_cutoff) if raw_cutoff else DEFAULT_CUTOFF
        except (TypeError, ValueError):
            cutoff = DEFAULT_CUTOFF

        recent_dates = sorted(history.keys())[-LOOKBACK_DAYS:]
        # Evaluate against whatever history actually exists (up to
        # LOOKBACK_DAYS), rather than requiring a full LOOKBACK_DAYS
        # window of entries before the very first adjustment. Requiring
        # a full window meant a fresh deploy (or any gap that cleared
        # history) had to sit at the strictest possible DEFAULT_CUTOFF
        # for LOOKBACK_DAYS days no matter how little qualifying supply
        # showed up in that time — a multi-day guaranteed-zero-alerts
        # cold start. One day of real data is enough to start the same
        # gradual ADJUST_STEP-at-a-time correction the rolling window
        # already does; it still converges to the same steady state
        # once LOOKBACK_DAYS of history exists.
        if recent_dates:
            recent_counts = [history[d] for d in recent_dates]

            if all(c < DAILY_MIN for c in recent_counts) and cutoff > DYNAMIC_FLOOR:
                cutoff = max(DYNAMIC_FLOOR, cutoff - ADJUST_STEP)
                logger.info(f"Quota governor: lowering conviction cutoff to {cutoff}")
            elif all(c >= DAILY_MIN for c in recent_counts) and cutoff < DEFAULT_CUTOFF:
                cutoff = min(DEFAULT_CUTOFF, cutoff + ADJUST_STEP)
                logger.info(f"Quota governor: raising conviction cutoff back to {cutoff}")

        await _set_flag(session, _CUTOFF_KEY, str(cutoff))
        await _set_flag(session, _LAST_ADJUST_KEY, today)
        await session.commit()

    return cutoff


async def get_daily_alert_count() -> int:
    """How many alerts have actually been sent today (UTC)."""
    start_of_day = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    async with async_session() as session:
        res = await session.execute(
            select(func.count(PumpAlertedToken.id)).where(
                PumpAlertedToken.alerted_at >= start_of_day
            )
        )
        return res.scalar() or 0


async def has_quota_remaining() -> bool:
    """Hard daily cap (Blueprint DAILY_MAX) — never send past this,
    however strong a later candidate scores."""
    count = await get_daily_alert_count()
    return count < DAILY_MAX
