"""
King Token Profile -- proven multi-milestone-winner pattern extraction.

A "king token" is a signal whose SignalEvent history (domain.signals.
signal_tracker.send_milestone_alert) shows it didn't just pop once, but
kept clearing the Quote Alert milestone ladder -- +25%, +50%, +75%,
2X, 3X, ... -- the way $QUEST has. This module reads that history
(which already exists; nothing new is tracked) and turns it into a
small, bounded "winning entry profile": the average on-chain
conviction-score breakdown (domain.signals.scoring.score_candidate()'s
own breakdown, snapshotted at entry time in
SignalToken.entry_breakdown_json) across every token that has proven
itself a repeat winner.

This is the same shape of insight domain/signals/ai_calibration_engine.py
already computes in shadow mode (component_correlations against
ath_multiple) -- the difference is this module turns it into a small,
live scoring input (domain.signals.scoring._king_pattern_bonus), the
same way verified smart_money/whale holdings already do, instead of
leaving it purely advisory. It never overrides or bypasses a single
hard gate: a candidate that fails hard_reject_reasons() never reaches
this bonus regardless of how closely it resembles the King profile,
and the profile is only ever trusted once at least
KING_PATTERN_MIN_SAMPLE_SIZE (see scoring.py) proven winners back it --
a single lucky token is not treated as a repeatable pattern.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select, func

from infra.db.session import async_session
from models.signal_token import SignalToken
from models.signal_event import SignalEvent, Milestone

logger = logging.getLogger("AlphaPulse.KingTokenProfile")

# A signal needs at least this many genuine positive-milestone
# SignalEvents (e.g. +25%, +50%, 2X already reaches this) to count as
# a "king token" -- repeatedly proven, not a single lucky print.
KING_MIN_MILESTONE_EVENTS = 3

# Milestones that do NOT count as evidence of a repeat winner: ENTRY is
# not a gain at all, ARCHIVE is bookkeeping, DUMP is a loss.
_NON_WIN_MILESTONES = (Milestone.ENTRY, Milestone.ARCHIVE, Milestone.DUMP)

# The four numeric conviction-scorer sub-scores that
# entry_breakdown_json always carries (see scoring.score_candidate()) --
# the only fields this profile averages. Bonus/multiplier fields are
# deliberately excluded: those already reflect narrative hype or
# earlier smart-money bonuses, not the repeatable on-chain shape this
# profile is meant to capture.
BREAKDOWN_COMPONENT_KEYS = (
    "liquidity_lp_integrity",
    "holder_distribution",
    "momentum_quality",
    "wallet_deployer_behavior",
)

_PROFILE_CACHE_TTL_SECONDS = 3600  # refresh at most once an hour
_cache: dict | None = None
_cache_built_at: float = 0.0


def _numeric_breakdown_fields(breakdown: dict) -> dict[str, float]:
    out = {}
    for key in BREAKDOWN_COMPONENT_KEYS:
        value = breakdown.get(key)
        if isinstance(value, (int, float)):
            out[key] = float(value)
    return out


async def compute_king_token_profile(session=None) -> dict:
    """
    Builds the King Token profile from every signal whose SignalEvent
    history shows >= KING_MIN_MILESTONE_EVENTS genuine positive
    milestones. Purely additive/advisory data extraction: never
    mutates SignalToken/SignalEvent, and never itself decides a live
    candidate's fate -- see scoring._king_pattern_bonus() for the
    (small, capped) way this profile is actually used.
    """
    own_session = session is None
    if own_session:
        session = async_session()
    try:
        res = await session.execute(
            select(SignalEvent.signal_id, func.count(SignalEvent.id))
            .where(SignalEvent.milestone_type.notin_(list(_NON_WIN_MILESTONES)))
            .group_by(SignalEvent.signal_id)
            .having(func.count(SignalEvent.id) >= KING_MIN_MILESTONE_EVENTS)
        )
        king_signal_ids = [row[0] for row in res.all()]

        if not king_signal_ids:
            return {
                "sample_size": 0,
                "centroid": {},
                "king_signal_ids": [],
                "king_symbols": [],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

        res2 = await session.execute(
            select(SignalToken).where(SignalToken.id.in_(king_signal_ids))
        )
        king_signals = list(res2.scalars().all())
    finally:
        if own_session:
            await session.close()

    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for s in king_signals:
        raw = getattr(s, "entry_breakdown_json", None)
        if not raw:
            continue
        try:
            breakdown = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        for key, value in _numeric_breakdown_fields(breakdown).items():
            sums[key] = sums.get(key, 0.0) + value
            counts[key] = counts.get(key, 0) + 1

    centroid = {key: round(sums[key] / counts[key], 2) for key in sums if counts[key] > 0}

    return {
        # sample_size counts every proven king token, even ones missing
        # an entry_breakdown_json snapshot (older rows) -- the centroid
        # itself is only ever averaged over rows that actually have one.
        "sample_size": len(king_signals),
        "centroid": centroid,
        "king_signal_ids": [s.id for s in king_signals],
        "king_symbols": sorted({s.symbol for s in king_signals if s.symbol}),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_cached_king_profile(*, force_refresh: bool = False) -> dict:
    """In-process TTL cache so the DB aggregation above doesn't run on
    every single candidate evaluated across every scan cycle -- refreshed
    at most once per _PROFILE_CACHE_TTL_SECONDS. Provider/DB failure here
    must never break candidate scoring, so a refresh failure logs and
    falls back to the last good cache (or an empty, zero-bonus profile
    if there isn't one yet)."""
    global _cache, _cache_built_at
    now = time.monotonic()
    if force_refresh or _cache is None or (now - _cache_built_at) > _PROFILE_CACHE_TTL_SECONDS:
        try:
            _cache = await compute_king_token_profile()
            _cache_built_at = now
        except Exception as e:
            logger.error(f"King token profile refresh failed: {e}")
            if _cache is None:
                _cache = {
                    "sample_size": 0,
                    "centroid": {},
                    "king_signal_ids": [],
                    "king_symbols": [],
                    "generated_at": None,
                }
    return _cache


__all__ = [
    "KING_MIN_MILESTONE_EVENTS",
    "BREAKDOWN_COMPONENT_KEYS",
    "compute_king_token_profile",
    "get_cached_king_profile",
]
