"""Re-pump re-delivery for already-alerted Pump.fun signals.

Problem
-------
Once a token has a SignalToken row, every discovery path treats it as
"already alerted": create_signal_from_candidate() returns False for an
existing contract, so a token that pumped, dumped, and later began a
genuinely new pump was silently ignored forever -- its milestone ladder
was stuck on the old entry price / old highest_alerted_multiple.

What this module does
---------------------
Called once per active signal from the signal lifecycle loops (which
already poll live market data every cycle), it:

  1. Tracks a confirmed post-dump low ("trough") for the signal.
  2. When the re-delivery condition below is met, immediately sends the
     token as a NEW FULL alert card titled REDELIVERED to every Signal
     Alert subscriber.
  3. Only once that card was delivered to at least one chat, resets the
     signal's milestone tracking as a fresh cycle, anchored to the exact
     price / market cap / timestamp of the re-delivery. The previous
     cycle is archived in signal_tokens.prior_cycles_json.

Re-delivery condition (all must hold; every threshold is env-tunable)
---------------------------------------------------------------------
  * the signal's original alert was actually delivered
  * PUMPED   : cycle peak market cap >= REPUMP_MIN_PRIOR_PUMP_X  x entry (1.5)
  * DUMPED   : a confirmed trough <= REPUMP_DUMP_RETRACE_RATIO x peak    (0.5)
               (confirmed = two consecutive polls inside the dump zone,
               so a single bad provider tick can never fake a dump)
  * RE-PUMP  : current market cap >= REPUMP_REBOUND_MULTIPLE x trough    (2.0)
  * LIVE     : 24h volume >= REPUMP_MIN_VOLUME_24H (5000) and
               liquidity >= REPUMP_MIN_LIQUIDITY (3000)
  * SPACING  : >= REPUMP_MIN_GAP_MINUTES (30) since the cycle began, and
               fewer than REPUMP_MAX_REDELIVERIES (3) re-deliveries so far
  * Solana Pump.fun signals only (same policy as the signal engine)

Deliberately NOT touched
------------------------
alert_delivered / alert_delivered_at / signaled_at / was_redelivered
(consumed by the real-money and Auto-Trade signal pickup), PumpAlertedToken
.alerted_at (the daily alert quota), and auto-buy: a re-delivery is an
informational alert + tracking reset only; it never triggers a buy.
"""

from __future__ import annotations

import html
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("WhaleAlpha.RepumpRedelivery")

MAX_ARCHIVED_CYCLES = 20


# ── configuration ────────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class RepumpConfig:
    enabled: bool = True
    min_prior_pump: float = 1.5
    dump_retrace_ratio: float = 0.5
    rebound_multiple: float = 2.0
    min_volume_24h: float = 5000.0
    min_liquidity: float = 3000.0
    min_gap_minutes: float = 30.0
    max_redeliveries: int = 3


def load_config() -> RepumpConfig:
    return RepumpConfig(
        enabled=_env_bool("REPUMP_REDELIVERY_ENABLED", True),
        min_prior_pump=_env_float("REPUMP_MIN_PRIOR_PUMP_X", 1.5),
        dump_retrace_ratio=_env_float("REPUMP_DUMP_RETRACE_RATIO", 0.5),
        rebound_multiple=_env_float("REPUMP_REBOUND_MULTIPLE", 2.0),
        min_volume_24h=_env_float("REPUMP_MIN_VOLUME_24H", 5000.0),
        min_liquidity=_env_float("REPUMP_MIN_LIQUIDITY", 3000.0),
        min_gap_minutes=_env_float("REPUMP_MIN_GAP_MINUTES", 30.0),
        max_redeliveries=int(_env_float("REPUMP_MAX_REDELIVERIES", 3)),
    )


# ── pure decision logic (no I/O; unit-tested) ────────────────────────────

def update_trough(
    prev_mc: float | None,
    cur_mc: float,
    peak_mc: float,
    trough_mc: float | None,
    cfg: RepumpConfig,
) -> float | None:
    """Returns the signal's updated post-dump low.

    * cur >= peak          -> a new high invalidates any earlier dump -> None
    * two consecutive polls inside the dump zone (<= peak * retrace) ->
      the trough is the HIGHER of the two readings (conservative), then
      only ever ratchets down. A single-poll dip never creates a trough.
    * otherwise unchanged.
    """
    if peak_mc <= 0 or cur_mc <= 0:
        return trough_mc
    if cur_mc >= peak_mc:
        return None
    dump_line = peak_mc * cfg.dump_retrace_ratio
    if prev_mc and 0 < prev_mc <= dump_line and cur_mc <= dump_line:
        candidate = max(prev_mc, cur_mc)
        return candidate if trough_mc is None else min(trough_mc, candidate)
    return trough_mc


@dataclass(frozen=True)
class RepumpDecision:
    trigger: bool
    reason: str
    prior_peak_multiple: float = 0.0
    dump_pct: float = 0.0
    rebound_multiple: float = 0.0


def evaluate_repump(
    *,
    entry_mc: float,
    peak_mc: float,
    trough_mc: float | None,
    cur_mc: float,
    volume_24h: float,
    liquidity: float,
    now: datetime,
    reference_time: datetime | None,
    redelivery_count: int,
    cfg: RepumpConfig,
) -> RepumpDecision:
    if not cfg.enabled:
        return RepumpDecision(False, "disabled")
    if redelivery_count >= cfg.max_redeliveries:
        return RepumpDecision(False, "max_redeliveries")
    if entry_mc <= 0 or peak_mc <= 0 or cur_mc <= 0:
        return RepumpDecision(False, "bad_market_cap")
    if not trough_mc or trough_mc <= 0:
        return RepumpDecision(False, "no_confirmed_dump")

    prior_peak_x = peak_mc / entry_mc
    dump_pct = (1 - trough_mc / peak_mc) * 100
    rebound_x = cur_mc / trough_mc

    if prior_peak_x < cfg.min_prior_pump:
        return RepumpDecision(False, "no_prior_pump", prior_peak_x, dump_pct, rebound_x)
    if trough_mc > peak_mc * cfg.dump_retrace_ratio:
        return RepumpDecision(False, "dump_too_shallow", prior_peak_x, dump_pct, rebound_x)
    if rebound_x < cfg.rebound_multiple:
        return RepumpDecision(False, "rebound_too_small", prior_peak_x, dump_pct, rebound_x)
    if volume_24h < cfg.min_volume_24h:
        return RepumpDecision(False, "low_volume", prior_peak_x, dump_pct, rebound_x)
    if liquidity < cfg.min_liquidity:
        return RepumpDecision(False, "low_liquidity", prior_peak_x, dump_pct, rebound_x)
    if reference_time is not None and (now - reference_time) < timedelta(minutes=cfg.min_gap_minutes):
        return RepumpDecision(False, "min_gap", prior_peak_x, dump_pct, rebound_x)

    return RepumpDecision(True, "repump", prior_peak_x, dump_pct, rebound_x)


def append_cycle_archive(prior_json: str | None, record: dict, keep: int = MAX_ARCHIVED_CYCLES) -> str:
    try:
        cycles = json.loads(prior_json) if prior_json else []
        if not isinstance(cycles, list):
            cycles = []
    except (TypeError, ValueError):
        cycles = []
    cycles.append(record)
    return json.dumps(cycles[-keep:])


def _f(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return default


def _iso(dt) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


# ── lifecycle hook ───────────────────────────────────────────────────────

async def maybe_redeliver_repump(bot, signal, data: dict, current_mc: float) -> bool:
    """Called once per active signal per lifecycle pass, AFTER the loop has
    persisted this poll's current/ATH values.

    Returns True only when a REDELIVERED card reached at least one chat AND
    the signal's tracking was reset. The caller must then skip milestone
    processing for this pass (the ladder was just re-anchored).
    Never raises into the lifecycle loop's own per-signal error handling
    beyond what the caller already catches.
    """
    cfg = load_config()
    if not cfg.enabled:
        return False

    contract = getattr(signal, "contract", "") or ""
    chain = (getattr(signal, "chain", None) or "solana").lower()
    if chain != "solana" or not contract.lower().endswith("pump"):
        return False

    msg_ids_json = getattr(signal, "message_ids_json", None)
    already_alerted = bool(getattr(signal, "alert_delivered", False)) or bool(
        msg_ids_json and msg_ids_json not in ("{}", "")
    )
    if not already_alerted:
        return False

    entry_mc = _f(getattr(signal, "entry_market_cap", None))
    if entry_mc <= 0 or current_mc <= 0:
        return False

    prev_mc = _f(getattr(signal, "current_market_cap", None)) or None
    prior_peak_mc = max(_f(getattr(signal, "ath_market_cap", None)), entry_mc)
    stored_trough = getattr(signal, "repump_trough_market_cap", None)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    reference_time = (
        getattr(signal, "cycle_started_at", None)
        or getattr(signal, "signaled_at", None)
    )

    decision = evaluate_repump(
        entry_mc=entry_mc,
        peak_mc=prior_peak_mc,
        trough_mc=stored_trough,
        cur_mc=current_mc,
        volume_24h=_f(data.get("volume_24h")),
        liquidity=_f(data.get("liquidity")),
        now=now,
        reference_time=reference_time,
        redelivery_count=int(getattr(signal, "repump_redelivery_count", 0) or 0),
        cfg=cfg,
    )

    from sqlalchemy import update, func as sa_func
    from infra.db.session import async_session
    from models.signal_token import SignalToken

    if not decision.trigger:
        new_trough = update_trough(prev_mc, current_mc, prior_peak_mc, stored_trough, cfg)
        if new_trough != stored_trough:
            async with async_session() as session:
                await session.execute(
                    update(SignalToken)
                    .where(SignalToken.id == signal.id)
                    .values(repump_trough_market_cap=new_trough)
                )
                await session.commit()
        return False

    price = _f(data.get("price"))
    if price <= 0:
        logger.warning("[Repump] %s qualifies but live price is missing -- retrying next pass", contract[:8])
        return False

    from domain.signals.pump_radar import get_pump_subscribers, send_pump_card
    from domain.signals.signal_tracker import format_usd

    subscribers = await get_pump_subscribers()
    if not subscribers:
        return False

    # Full card from the signal's persisted entry snapshot + LIVE market
    # data -- same construction the existing undelivered-signal redelivery
    # uses, so it is a complete card, not a milestone one-liner.
    old_cycle = int(getattr(signal, "tracking_cycle", None) or 1)
    rebound_pct = (decision.rebound_multiple - 1) * 100
    candidate = {
        "contract": contract,
        "data": dict(data),
        "pump": {
            "score": int(_f(getattr(signal, "entry_score", None))),
            "tier": "Signal Alert (REDELIVERED)",
            "reasons": [
                f"Re-pump +{rebound_pct:.0f}% off post-dump low",
                f"Prior peak {decision.prior_peak_multiple:.1f}x, dumped -{decision.dump_pct:.0f}%",
            ],
            "breakdown": json.loads(signal.entry_breakdown_json) if getattr(signal, "entry_breakdown_json", None) else {},
        },
        "holder_analysis": {
            "total_holders": getattr(signal, "total_holders", None),
            "top_holder_pct": getattr(signal, "top_holder_pct", None),
            "top10_pct": getattr(signal, "top10_holder_pct", None),
            "dev_holding_pct": getattr(signal, "dev_holding_pct", None),
            "bundle_wallet_count": getattr(signal, "bundle_wallet_count", None),
            "bundle_pct": getattr(signal, "bundle_pct", None),
        },
    }
    title = "🔁 <b>REDELIVERED</b> — Re-pump detected"
    extra_block = [
        (
            f"📈 Peak <b>{decision.prior_peak_multiple:.2f}x</b> → low "
            f"<b>-{decision.dump_pct:.0f}%</b> → now <b>{decision.rebound_multiple:.2f}x</b> off the low"
        ),
        f"♻️ Tracking reset at <b>{html.escape(format_usd(current_mc))}</b> (cycle {old_cycle + 1})",
    ]

    import asyncio

    msg_ids: dict[str, int] = {}
    failures = 0
    for chat_id in subscribers:
        try:
            msg = await send_pump_card(bot, chat_id, candidate, title=title, extra_block=extra_block)
            if msg and hasattr(msg, "message_id"):
                msg_ids[str(chat_id)] = msg.message_id
        except Exception as exc:
            failures += 1
            logger.warning("[Repump] send failed for chat %s / %s: %s", chat_id, contract[:8], exc)
        await asyncio.sleep(0.1)

    if not msg_ids:
        logger.warning(
            "[Repump] %s: 0/%d delivered (%d failed) -- tracking NOT reset, retrying next pass",
            contract[:8], len(subscribers), failures,
        )
        return False

    # Delivery confirmed -> reset milestone tracking from this exact moment.
    archive = append_cycle_archive(
        getattr(signal, "prior_cycles_json", None),
        {
            "cycle": old_cycle,
            "started_at": _iso(reference_time),
            "ended_at": now.isoformat(),
            "entry_price": getattr(signal, "entry_price", None),
            "entry_market_cap": entry_mc,
            "peak_market_cap": prior_peak_mc,
            "peak_multiple": round(decision.prior_peak_multiple, 4),
            "trough_market_cap": stored_trough,
            "highest_alerted_multiple": getattr(signal, "highest_alerted_multiple", None),
            "message_ids": json.loads(msg_ids_json) if msg_ids_json else {},
        },
    )
    liquidity = _f(data.get("liquidity"))

    async with async_session() as session:
        result = await session.execute(
            update(SignalToken)
            .where(
                SignalToken.id == signal.id,
                sa_func.coalesce(SignalToken.tracking_cycle, 1) == old_cycle,
            )
            .values(
                entry_price=price,
                entry_market_cap=current_mc,
                entry_liquidity=liquidity,
                current_price=price,
                current_market_cap=current_mc,
                current_liquidity=liquidity,
                ath_price=price,
                ath_market_cap=current_mc,
                current_multiple=1.0,
                ath_multiple=1.0,
                highest_alerted_multiple=1.0,
                lowest_alerted_multiple=1.0,
                message_ids_json=json.dumps(msg_ids),
                tracking_cycle=old_cycle + 1,
                cycle_started_at=sa_func.now(),
                last_repump_redelivery_at=sa_func.now(),
                repump_trough_market_cap=None,
                repump_redelivery_count=int(getattr(signal, "repump_redelivery_count", 0) or 0) + 1,
                prior_cycles_json=archive,
            )
        )
        await session.commit()
        reset_rows = result.rowcount or 0

    if not reset_rows:
        logger.warning("[Repump] %s: cycle already advanced elsewhere; reset skipped", contract[:8])
        return False

    # Keep the duplicate-signal gate's own bookkeeping on the new scale.
    # alerted_at (daily quota) is intentionally NOT touched.
    try:
        from config.settings import SIGNAL_COOLDOWN_HOURS
        from models.pump_alerted_token import PumpAlertedToken

        async with async_session() as session:
            await session.execute(
                update(PumpAlertedToken)
                .where(PumpAlertedToken.contract == contract)
                .values(
                    times_alerted=sa_func.coalesce(PumpAlertedToken.times_alerted, 1) + 1,
                    last_alert_ath_multiple=1.0,
                    cooldown_expires_at=now + timedelta(hours=SIGNAL_COOLDOWN_HOURS),
                )
            )
            await session.commit()
    except Exception as exc:
        logger.warning("[Repump] %s: pump_alerted_tokens bookkeeping skipped (non-fatal): %s", contract[:8], exc)

    logger.info(
        "[Repump] %s REDELIVERED to %d/%d chat(s): peak %.2fx -> low -%.0f%% -> %.2fx off low; "
        "tracking reset at mc=%.0f price=%s (cycle %d)",
        contract[:8], len(msg_ids), len(subscribers), decision.prior_peak_multiple,
        decision.dump_pct, decision.rebound_multiple, current_mc, price, old_cycle + 1,
    )
    return True
