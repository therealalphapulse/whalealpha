import asyncio
import html
import json
import logging
import math
import time
from datetime import datetime, timezone

from sqlalchemy import select, text, update, delete, func as sa_func

from infra.db.session import async_session, engine
from models.signal_token import SignalToken
from models.signal_event import SignalEvent, Milestone
from models.system_flag import SystemFlag
from providers.marketdata.dexscreener import get_token_card_info

logger = logging.getLogger("AlphaPulse.SignalTracker")

# --- Quote Alert / 24h trading-cycle gate ---
# Quote (milestone) alerts should only fire for a signal that is still
# genuinely trading right now — not for an old, abandoned token whose
# market has effectively gone quiet but whose SignalToken row is still
# marked "active" (this codebase has no separate expiry job, so a signal
# stays "active" indefinitely once created). A near-zero 24h volume is the
# clearest available signal that a token's trading cycle has gone dead;
# gating the alert (not the underlying lifecycle price/ATH tracking) on
# this keeps users from getting a "gain" quote alert on a token nobody is
# actually trading anymore.
MIN_24H_VOLUME_FOR_QUOTE_ALERT = 500.0


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else "N/A"


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return default


def format_usd(value) -> str:
    try:
        num = float(value)
        if num >= 1_000_000_000:
            return f"${num / 1_000_000_000:.2f}B"
        elif num >= 1_000_000:
            return f"${num / 1_000_000:.2f}M"
        elif num >= 1_000:
            return f"${num / 1_000:.2f}K"
        else:
            return f"${num:,.2f}"
    except Exception:
        return "N/A"


def format_x(value) -> str:
    try:
        return f"{float(value):.2f}x"
    except Exception:
        return "N/A"


async def migrate_signal_schema():
    statements = [
        "ALTER TABLE signal_tokens ADD COLUMN IF NOT EXISTS twitter_url VARCHAR",
        "ALTER TABLE signal_tokens ADD COLUMN IF NOT EXISTS telegram_url VARCHAR",
        "ALTER TABLE signal_tokens ADD COLUMN IF NOT EXISTS message_ids_json TEXT DEFAULT '{}'",
        "ALTER TABLE signal_tokens ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT",
        "ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS milestone_value DOUBLE PRECISION",
        "ALTER TABLE signal_tokens ADD COLUMN IF NOT EXISTS highest_alerted_multiple DOUBLE PRECISION DEFAULT 1.0",
        # Production incident fix (see SIGNAL_ENGINE_REEVALUATION.md /
        # PRODUCTION_AUDIT_REPORT.md history): status=="active" is set
        # the instant a SignalToken row is created, before the Signal
        # Alert card has actually been delivered to any subscriber. Both
        # auto-buy paths (paper, in domain/signals/pump_radar.py, and
        # real money, in domain/trading/real/real_automation_engine.py)
        # now additionally require alert_delivered == True, which this
        # column tracks. Only ever set True by
        # mark_signal_alert_delivered() below, called from
        # pump_radar_loop after the subscriber-delivery loop confirms at
        # least one successful send. Defaults to FALSE so no historical
        # row is retroactively treated as alerted.
        "ALTER TABLE signal_tokens ADD COLUMN IF NOT EXISTS alert_delivered BOOLEAN DEFAULT FALSE",
        "ALTER TABLE signal_tokens ADD COLUMN IF NOT EXISTS alert_delivered_at TIMESTAMP",
        # Downside counterpart to highest_alerted_multiple: gates DUMP/SL
        # quote alerts the same way highest_alerted_multiple gates upside
        # milestone alerts (only alert on a genuinely NEW low, never
        # re-fire for a dip that's already been reported). Added to wire
        # up the DUMP milestone, which existed in the enum/schema but was
        # never actually triggered anywhere in the lifecycle loop.
        "ALTER TABLE signal_tokens ADD COLUMN IF NOT EXISTS lowest_alerted_multiple DOUBLE PRECISION DEFAULT 1.0",
        # Auto-buy "signal source" setting (New / Redelivered / Both) --
        # see models/signal_token.py and redeliver_undelivered_signal_alerts()
        # in domain/signals/pump_radar.py.
        "ALTER TABLE signal_tokens ADD COLUMN IF NOT EXISTS was_redelivered BOOLEAN DEFAULT FALSE",
        # First Milestone Snapshot support -- see models/signal_token.py
        # and send_milestone_alert() below.
        "ALTER TABLE signal_tokens ADD COLUMN IF NOT EXISTS first_milestone_message_ids_json TEXT DEFAULT '{}'",
        # NOTE: models/signal_event.py's SAEnum(Milestone) uses SQLAlchemy's
        # default mapping, which sends each member's NAME (e.g. "PCT_25",
        # "SIX_X"), not its .value. A prior patch briefly switched this to
        # values_callable (sending .value strings like "25pct" instead),
        # which broke every milestone insert against this live enum type
        # and was reverted. The .value-style entries below are harmless
        # leftovers from that attempt — kept so no data already written
        # under them becomes unreadable, but nothing writes them anymore.
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'ENTRY'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'PCT_25'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'PCT_50'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'TWO_X'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'THREE_X'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'FOUR_X'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'FIVE_X'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'SIX_X'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'TEN_X'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'MULTI_X'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'DUMP'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'ARCHIVE'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'entry'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS '25pct'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS '50pct'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS '2x'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS '3x'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS '4x'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS '5x'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS '6x'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS '10x'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'multi_x'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'dump'",
        "ALTER TYPE milestone ADD VALUE IF NOT EXISTS 'archive'",
    ]

    # New enum values must be committed before they can be used, and
    # ALTER TYPE ... ADD VALUE cannot run inside the same multi-statement
    # transaction as other DDL on some Postgres versions, so each
    # statement gets its own connection/transaction.
    for s in statements:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(s))
        except Exception as e:
            logger.warning(f"Signal schema migration statement skipped ({s}): {e}")

    logger.info("✅ Signal Schema Migration Complete")


async def reset_signal_history_once():
    """
    One-time reset of signal tracking history (SignalToken + SignalEvent
    rows only). Does NOT touch PaperPortfolio, PaperTrade, PaperSettings,
    or PaperPnlEvent — user portfolio balances, positions, and trade
    history are fully preserved. Guarded by a SystemFlag so this only
    ever runs once, even across restarts/redeploys.
    """
    flag_key = "signal_history_reset_v1"

    async with async_session() as session:
        result = await session.execute(select(SystemFlag).where(SystemFlag.key == flag_key))
        if result.scalar_one_or_none():
            return  # already ran, nothing to do

        await session.execute(delete(SignalEvent))
        await session.execute(delete(SignalToken))
        session.add(SystemFlag(key=flag_key, value="done"))
        await session.commit()

    logger.info("✅ One-time signal history reset complete (portfolio data preserved)")


async def _get_flag_int(key: str, default: int = 0) -> int:
    async with async_session() as session:
        result = await session.execute(select(SystemFlag).where(SystemFlag.key == key))
        flag = result.scalar_one_or_none()
        try:
            return int(flag.value) if flag and flag.value else default
        except (ValueError, TypeError):
            return default


async def _set_flag_int(key: str, value: int) -> None:
    async with async_session() as session:
        result = await session.execute(select(SystemFlag).where(SystemFlag.key == key))
        flag = result.scalar_one_or_none()
        if flag:
            flag.value = str(value)
        else:
            session.add(SystemFlag(key=key, value=str(value)))
        await session.commit()


async def broadcast_to_all_subscribers(bot, text_msg: str):
    """
    Shared send-to-everyone helper used by every scheduled/threshold
    broadcast (every-10, every-15, all-time, 24h). Keeps the subscriber
    fetch + per-chat send/error-handling in one place instead of repeated
    per report type.
    """
    try:
        # Local import to avoid a circular import at module load time
        # (pump_radar.py imports from this module too).
        from domain.signals.pump_radar import get_pump_subscribers
        subscribers = await get_pump_subscribers()
    except Exception as e:
        logger.error(f"Broadcast subscriber fetch failed: {e}")
        return

    for chat_id in subscribers:
        try:
            await bot.send_message(chat_id, text_msg)
        except Exception as e:
            logger.warning(f"Broadcast send failed for {chat_id}: {e}")


async def build_last_n_signals_report(n: int, ascending: bool = True) -> str | None:
    """
    Shared builder for the every-N-signals summary. `ascending=True`
    matches the existing every-10 report (weakest-to-strongest); the new
    every-15 report (item 5) uses ascending=False for a "top performers
    first" framing, per its own spec wording.
    """
    async with async_session() as session:
        res = await session.execute(
            select(SignalToken).order_by(SignalToken.signaled_at.desc()).limit(n)
        )
        recent = res.scalars().all()

    if not recent:
        return None

    scored = []
    for s in recent:
        mult = s.ath_multiple or 1.0
        pct = (mult - 1) * 100
        scored.append((pct, s.symbol))

    scored.sort(key=lambda x: x[0], reverse=not ascending)

    title = "Signal Summary" if ascending else "Performance Report"
    text_msg = f"📢 <b>{title} — Last {len(scored)} Alerts</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    for pct, symbol in scored:
        emoji = "🟢" if pct >= 0 else "🔴"
        sign = "+" if pct >= 0 else ""
        text_msg += f"{emoji} ${_esc(symbol)}: {sign}{pct:.0f}%\n"
    text_msg += "\n⚡ Powered by AlphaPulse"
    return text_msg


async def bump_signal_count_and_maybe_broadcast(bot):
    """
    Increments the persistent total-signals counter.
      - Every 10th signal: existing weakest-to-strongest summary of the
        last 10 (unchanged behavior).
      - Every 15th signal (item 5): a top-performers-first performance
        report of the last 15. Independent trigger on the same counter —
        both can fire in the same cycle (e.g. at count 30).
    """
    count = await _get_flag_int("total_signal_count", 0) + 1
    await _set_flag_int("total_signal_count", count)

    if count % 10 == 0:
        report = await build_last_n_signals_report(10, ascending=True)
        if report:
            await broadcast_to_all_subscribers(bot, report + f"\n📊 Total Signals Sent: {count}")

    if count % 15 == 0:
        report = await build_last_n_signals_report(15, ascending=False)
        if report:
            await broadcast_to_all_subscribers(bot, report)


async def build_24h_trending_report(limit: int = 10) -> str | None:
    """
    Item 4: leaderboard scoped to only the current 24h window, resetting
    at 00:00 UTC (i.e. signals created since midnight today).
    """
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

    async with async_session() as session:
        res = await session.execute(
            select(SignalToken)
            .where(SignalToken.signaled_at >= start_of_day, SignalToken.ath_multiple >= 1.25)
            .order_by(SignalToken.ath_multiple.desc())
            .limit(limit)
        )
        signals = res.scalars().all()

    if not signals:
        return None

    text_msg = "📅 <b>AlphaPulse 24-Hour Trending</b>\n<i>Best signals today</i>\n\n"
    for i, s in enumerate(signals):
        mult = s.ath_multiple or 1.0
        medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"{i + 1}️⃣"
        text_msg += f"{medal} ${html.escape(s.symbol or 'Unknown')} • {mult:.1f}X\n"
    text_msg += "\n⚡ Powered by AlphaPulse"
    return text_msg


async def build_alltime_trending_broadcast(limit: int = 10) -> str | None:
    """
    Item 3: permanent leaderboard of the best AlphaPulse signals ever
    recorded, for the twice-daily scheduled broadcast. (Separate from
    get_cached_alltime_trending, which backs the on-demand /alltime
    command and has its own short cache — kept independent so this
    scheduled version always reflects the current data at send time.)
    """
    async with async_session() as session:
        res = await session.execute(
            select(SignalToken)
            .where(SignalToken.ath_multiple >= 1.25)
            .order_by(SignalToken.ath_multiple.desc())
            .limit(limit)
        )
        signals = res.scalars().all()

    if not signals:
        return None

    text_msg = "🏆 <b>Top AlphaPulse Trending</b>\n<i>Best signals ever recorded</i>\n\n"
    for i, s in enumerate(signals):
        mult = s.ath_multiple or 1.0
        medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"{i + 1}️⃣"
        text_msg += f"{medal} ${html.escape(s.symbol or 'Unknown')} • {mult:.1f}X\n"
    text_msg += "\n⚡ Powered by AlphaPulse"
    return text_msg


# Fixed UTC hour slots for the scheduled broadcasts. All-time: exactly 2x/
# day. 24h trending: exactly 7x/day, spread across the day and starting
# an hour after the 00:00 window reset so that slot isn't broadcasting an
# empty just-reset leaderboard.
ALLTIME_BROADCAST_HOURS_UTC = [9, 21]
TRENDING_24H_BROADCAST_HOURS_UTC = [1, 4, 7, 10, 13, 17, 21]


async def scheduled_broadcast_loop(bot, interval_seconds: int = 120):
    """
    Fires items 3 and 4 on their fixed daily schedules. Each hour slot is
    guarded by a SystemFlag keyed to that exact UTC date+hour, so
    re-checking every couple minutes never double-sends even if this
    loop's own timing drifts slightly.
    """
    logger.info("🗓️ Scheduled Trending Broadcasts Active")

    while True:
        try:
            now = datetime.now(timezone.utc)
            date_str = now.strftime("%Y-%m-%d")
            hour = now.hour

            if hour in ALLTIME_BROADCAST_HOURS_UTC:
                flag_key = f"broadcast_alltime_{date_str}_{hour}"
                if not await _get_flag_int(flag_key, 0):
                    report = await build_alltime_trending_broadcast()
                    if report:
                        await broadcast_to_all_subscribers(bot, report)
                    await _set_flag_int(flag_key, 1)

            if hour in TRENDING_24H_BROADCAST_HOURS_UTC:
                flag_key = f"broadcast_24h_{date_str}_{hour}"
                if not await _get_flag_int(flag_key, 0):
                    report = await build_24h_trending_report()
                    if report:
                        await broadcast_to_all_subscribers(bot, report)
                    await _set_flag_int(flag_key, 1)

        except Exception as e:
            logger.error(f"Scheduled broadcast loop error: {e}")

        await asyncio.sleep(interval_seconds)


async def update_signal_message_ids(contract: str, msg_ids_dict: dict):
    async with async_session() as session:
        await session.execute(
            update(SignalToken)
            .where(SignalToken.contract == contract)
            .values(message_ids_json=json.dumps(msg_ids_dict))
        )
        await session.commit()

    logger.info(f"Saved message ids for {contract[:8]}... -> {msg_ids_dict}")


async def update_first_milestone_message_ids(contract: str, msg_ids_dict: dict):
    """Persists the per-chat First Milestone Snapshot (root) message ids --
    see send_milestone_alert() below. Mirrors update_signal_message_ids()
    above, against the separate first_milestone_message_ids_json column."""
    async with async_session() as session:
        await session.execute(
            update(SignalToken)
            .where(SignalToken.contract == contract)
            .values(first_milestone_message_ids_json=json.dumps(msg_ids_dict))
        )
        await session.commit()

    logger.info(f"Saved first-milestone snapshot message ids for {contract[:8]}... -> {msg_ids_dict}")


async def mark_signal_alert_delivered(contract: str) -> None:
    """
    Production incident fix -- see migrate_signal_schema() above and
    domain/signals/pump_radar.py::pump_radar_loop. Called ONLY after the
    subscriber-delivery loop for a Signal Alert has confirmed at least
    one successful send. Both auto-buy paths (paper auto-buy here in
    pump_radar.py, and the real-money automation engine's independent
    poll) now require alert_delivered == True in addition to
    status == "active" before treating a signal as buy-eligible -- a
    SignalToken row existing only proves it passed qualification, not
    that anyone was ever alerted. Never call this before delivery is
    attempted, and never call it if every subscriber send failed.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        await session.execute(
            update(SignalToken)
            .where(SignalToken.contract == contract)
            .values(alert_delivered=True, alert_delivered_at=now)
        )
        await session.commit()

    logger.info(f"Signal Alert delivery confirmed for {contract[:8]}...")


async def get_previous_signal_for_contract(contract: str) -> SignalToken | None:
    """
    Read-only lookup: was `contract` already detected/alerted by
    AlphaPulse's own scanning pipeline?

    Used by app_platform/commands/auto_scan.py so a user pasting an
    independent CA they found elsewhere gets told "AlphaPulse already
    called this" and can be shown/quoted the original alert, instead of
    just a fresh DexScreener card as if it were never seen before. This
    does not participate in the signal lifecycle (no writes, no status
    changes) — it only reads the existing row if one exists.
    """
    async with async_session() as session:
        res = await session.execute(
            select(SignalToken).where(SignalToken.contract == contract)
        )
        return res.scalar_one_or_none()


async def create_signal_from_candidate(candidate: dict) -> bool:
    contract = candidate.get("contract", "")
    data = candidate.get("data") or {}
    pump = candidate.get("pump") or {}

    # Pump.fun-Only Signal Policy — defensive re-verification at the
    # Signal Engine's actual point of entry. Every upstream caller
    # (services/pump_radar.py) already filters to Pump.fun-origin mints
    # before reaching here; this is a second, independent check so a
    # non-Pump.fun contract can never enter the Signal Engine even if a
    # future caller forgets to filter upstream. A Pump.fun mint address
    # ends with the literal suffix "pump".
    if not contract or not contract.lower().endswith("pump"):
        logger.info(f"Signal rejected — not a verified Pump.fun origin: {contract[:8] if contract else 'N/A'}...")
        return False

    async with async_session() as session:
        existing_result = await session.execute(
            select(SignalToken).where(SignalToken.contract == contract)
        )
        existing = existing_result.scalar_one_or_none()

        if existing:
            return False

        mc = _to_float(data.get("market_cap")) or _to_float(data.get("fdv"))
        price = _to_float(data.get("price"))
        liq = _to_float(data.get("liquidity"))

        signal = SignalToken(
            contract=contract,
            name=data.get("name"),
            symbol=data.get("symbol"),
            twitter_url=data.get("twitter_url"),
            telegram_url=data.get("telegram_url"),
            entry_price=price,
            entry_market_cap=mc,
            entry_liquidity=liq,
            entry_score=_to_float(pump.get("score") or pump.get("probability")),
            # Signal Engine re-evaluation (Validation deliverable): this
            # column already existed and signal_calibration.py already
            # reads it, but nothing actually wrote to it — every real
            # signal was silently skipped by analyze_score_calibration()
            # (sample_size stayed 0 forever), which meant there was no
            # way to ever measure precision/false-positive rate or which
            # score components actually predict outcomes. Persist the
            # exact breakdown dict score_candidate() already computed for
            # this candidate, unmodified, so calibration works against
            # real history going forward.
            entry_breakdown_json=json.dumps(pump.get("breakdown")) if pump.get("breakdown") else None,
            current_price=price,
            current_market_cap=mc,
            current_liquidity=liq,
            ath_price=price,
            ath_market_cap=mc,
            current_multiple=1.0,
            ath_multiple=1.0,
            pair_url=data.get("pair_url"),
            image_url=data.get("image_url"),
            status="active",
            message_ids_json="{}",
            first_milestone_message_ids_json="{}",
        )

        session.add(signal)
        await session.commit()

    logger.info(f"Signal created for {contract[:8]}... symbol={data.get('symbol')}")
    return True


def _format_gain_label(gain: float) -> str:
    """
    Formats a raw gain multiple into a display label. Below 2X shows as a
    percentage (+37%); at or above 2X shows as an X-multiple, using one
    decimal only when the value isn't a clean whole number (5X, but 5.2X).
    """
    if gain < 2.0:
        pct = (gain - 1) * 100
        return f"+{pct:.0f}%"
    if abs(gain - round(gain)) < 0.05:
        return f"{round(gain)}X"
    return f"{gain:.1f}X"


def _milestone_enum(label: str) -> Milestone:
    mapping = {
        "+25%": Milestone.PCT_25,
        "+50%": Milestone.PCT_50,
        "2X": Milestone.TWO_X,
        "3X": Milestone.THREE_X,
        "4X": Milestone.FOUR_X,
        "5X": Milestone.FIVE_X,
        "6X": Milestone.SIX_X,
        "10X": Milestone.TEN_X,
        "DUMP": Milestone.DUMP,
    }
    # Any ladder rung without its own DB enum value (7X, 8X, 9X, 11X, and
    # every integer beyond — the ladder continues indefinitely, so it can
    # never have a dedicated Postgres enum value for every X) falls back to
    # the existing MULTI_X bucket. This only affects the coarse
    # `milestone_type` category column; the exact rung ("7X", "11X", ...) is
    # still preserved verbatim in SignalEvent.status, which is what actually
    # drives per-milestone dedup and the alert text.
    return mapping.get(label, Milestone.MULTI_X)


# Root-cause fix for missed/inconsistent Quote Alert milestones:
#
# The lifecycle loop polls each active signal's price periodically and used
# to alert on whatever the *current* live gain happened to be at poll time
# (via _format_gain_label), gated only by "gain > last_alerted". Two bugs
# followed from that:
#
#   1. A price that jumped several ladder rungs between two polls (very
#      common for volatile tokens on a 90s poll interval) only ever
#      produced ONE alert, for the current gain — every intermediate rung
#      (e.g. +25%, +50%, 2X, 3X on the way to a 5X print) was silently
#      skipped and never delivered.
#   2. The alert label was derived from the raw instantaneous gain rather
#      than the ladder rung actually being reported, so it almost never
#      landed on a clean milestone ("2.37X" instead of "2X"), which also
#      meant it fell through to the generic MULTI_X bucket instead of the
#      correct named milestone.
#
# _milestones_crossed() replaces that with an explicit, pure function over
# the fixed ladder (+25% -> +50% -> 2X -> 3X -> ... -> NX indefinitely) that
# returns every rung in (last_alerted, gain], in order, so the caller can
# fire one alert per rung — never zero (missed) and never more than once
# per rung (spam/duplicate), regardless of how far the price jumped in a
# single poll.
_MAX_MILESTONES_PER_POLL = 500  # safety cap: guards against a runaway loop
# / alert flood if bad market-cap data ever produced an absurd "gain"
# value. Never reached by real trading activity — the ladder itself is
# still uncapped/indefinite per rung.


def _milestones_crossed(last_alerted: float, gain: float) -> list:
    """
    Returns every Quote Alert ladder rung in (last_alerted, gain], ascending,
    as (threshold, label) pairs: (1.25, "+25%"), (1.5, "+50%"),
    (2.0, "2X"), (3.0, "3X"), ... continuing for every integer X.
    """
    crossed = []

    if last_alerted < 1.25 <= gain:
        crossed.append((1.25, "+25%"))
    if last_alerted < 1.50 <= gain:
        crossed.append((1.50, "+50%"))

    next_x = max(2, math.floor(last_alerted) + 1)
    while next_x <= gain and len(crossed) < _MAX_MILESTONES_PER_POLL:
        crossed.append((float(next_x), f"{next_x}X"))
        next_x += 1

    return crossed


async def signal_lifecycle_loop(bot, interval_seconds=90):
    await migrate_signal_schema()
    logger.info("📡 Signal Lifecycle Tracker with Replies Active")

    while True:
        try:
            async with async_session() as session:
                res = await session.execute(
                    select(SignalToken).where(SignalToken.status == "active")
                )
                signals = res.scalars().all()

            for s in signals:
                try:
                    data = await get_token_card_info(s.contract)
                    if not data:
                        continue

                    cur_mc = _to_float(data.get("market_cap")) or _to_float(data.get("fdv"))
                    if not s.entry_market_cap or s.entry_market_cap <= 0 or cur_mc <= 0:
                        continue

                    gain = cur_mc / s.entry_market_cap

                    # Quote Alerts only fire for tokens currently
                    # performing in their 24h trading cycle — a token with
                    # no real 24h volume is functionally an old/inactive
                    # signal even if its DB status still says "active", and
                    # should not generate a fresh alert. This does NOT
                    # affect price/ATH tracking below, only whether an
                    # alert is sent.
                    vol_24h = _to_float(data.get("volume_24h"))
                    is_currently_trading = vol_24h >= MIN_24H_VOLUME_FOR_QUOTE_ALERT

                    # ATH-based gate (replaces the old discrete-threshold
                    # dedup): only alert when price makes a genuinely NEW
                    # high beyond the last ATH that was actually alerted.
                    # A dump-then-recovery back to (or below) a previously
                    # alerted ATH never re-fires — only a fresh high above
                    # it does, at whatever exact multiple it reaches.
                    # Quote Alerts only ever report positive % gains — this
                    # is the sole trigger condition in the lifecycle loop.
                    last_alerted = s.highest_alerted_multiple or 1.0

                    # Root-cause fix (signal/milestone invariant audit,
                    # 2026-08-22): a milestone (Quote Alert) must never be
                    # sent for a signal whose own initial Signal Alert card
                    # was never actually delivered to anyone.
                    # SignalToken.status is set to "active" at creation
                    # time in pump_radar_loop *before* any delivery is
                    # attempted, and stays "active" regardless of delivery
                    # outcome -- so status alone was never sufficient to
                    # gate milestones on. message_ids_json is the one field
                    # that is only ever populated once send_pump_card()
                    # actually succeeds for at least one subscriber (see
                    # update_signal_message_ids() call sites), so it is the
                    # correct signal of "was the initial alert delivered".
                    # pump_radar_loop's redeliver_undelivered_signal_alerts()
                    # is what actively closes this gap by retrying delivery
                    # every cycle; this is the defensive backstop that
                    # guarantees the invariant even in the window before
                    # that retry catches up (or if it never can, because the
                    # candidate has since stopped qualifying). Price/ATH
                    # tracking above this point is intentionally NOT gated
                    # -- only the decision to fire an alert is.
                    has_confirmed_alert_delivery = bool(
                        s.message_ids_json and s.message_ids_json not in ("{}", "")
                    )
                    if not has_confirmed_alert_delivery and is_currently_trading and gain >= 1.25:
                        logger.info(
                            "[SignalMilestoneGate] Proceeding with milestone alert for "
                            "%s (gain=%.2fx) — initial Signal Alert not yet confirmed "
                            "delivered, but a qualifying milestone must not be "
                            "suppressed by missing/false delivery confirmation",
                            s.contract[:8], gain,
                        )
                    crossed_milestones = (
                        _milestones_crossed(last_alerted, gain)
                        if is_currently_trading
                        else []
                    )

                    # NOTE: downside ("Token is down" / DUMP) quote alerts
                    # have been intentionally removed. Users should not
                    # receive downside price alerts — only positive Quote
                    # Alerts, milestone alerts, and normal signal lifecycle
                    # tracking (below) are sent.

                    async with async_session() as session2:
                        values = {
                            "current_market_cap": cur_mc,
                            "current_multiple": gain,
                        }
                        if gain > (s.ath_multiple or 1.0):
                            values["ath_multiple"] = gain
                            values["ath_market_cap"] = cur_mc
                        # NOTE: `highest_alerted_multiple` is deliberately NOT
                        # bumped here. It is the field that gates whether a
                        # milestone is ever re-considered (`gain > last_alerted`
                        # above) — bumping it before the alert is actually
                        # confirmed delivered meant a crash/exception between
                        # this commit and send_milestone_alert() finishing
                        # would permanently suppress that milestone forever
                        # (gain would never again exceed an already-bumped
                        # value). It is now only persisted inside
                        # send_milestone_alert(), atomically with the
                        # SignalEvent row that is the actual dedup guard, so a
                        # failed/interrupted send is retried on the next
                        # lifecycle pass instead of silently lost.
                        await session2.execute(
                            update(SignalToken)
                            .where(SignalToken.id == s.id)
                            .values(**values)
                        )
                        await session2.commit()

                    # Fire one alert per crossed rung, in ascending order.
                    # The final (highest) rung uses the actual live gain/mc
                    # (identical to the old single-alert behavior for the
                    # common no-jump case). Any earlier rung swept up in the
                    # same jump — for which no real polled price exists —
                    # is reported at its own exact threshold, so the alert
                    # text reflects that rung rather than the token's later,
                    # higher price.
                    for idx, (threshold, m_label) in enumerate(crossed_milestones):
                        is_final = idx == len(crossed_milestones) - 1
                        step_gain = gain if is_final else threshold
                        step_mc = cur_mc if is_final else s.entry_market_cap * threshold
                        await send_milestone_alert(bot, s, m_label, step_mc, step_gain)

                except Exception as e:
                    # Isolated per-signal: one bad signal (e.g. a milestone
                    # type that doesn't exist in the DB enum yet) must never
                    # block replies/updates for every other signal.
                    logger.error(f"Signal lifecycle error for signal {s.id} ({s.contract}): {e}")

                await asyncio.sleep(1.0)

        except Exception as e:
            logger.error(f"Signal Lifecycle Error: {e}")

        await asyncio.sleep(interval_seconds)


def _build_first_milestone_snapshot_text(signal, label, cur_mc, gain) -> str:
    """First Milestone Snapshot (item 1): a complete data snapshot sent in
    place of the normal minimal milestone message, ONLY for a chat that
    never received this token's original Signal Alert, and ONLY the first
    time a tracked signal reaches a milestone. Explicitly NOT presented as
    a new/fresh signal -- labelled as a Snapshot of an existing tracked
    signal reporting the milestone it just reached, using only data
    already recorded on the signal (no new lookups, no changed milestone
    calculation/threshold)."""
    pct_gain = (gain - 1) * 100
    entry_price = signal.entry_price
    cur_price = signal.current_price or signal.entry_price

    lines = [
        f"🚀 <b>First Milestone Snapshot — ${_esc(signal.symbol)}</b>",
        "<i>(Existing tracked Alpha Pulse signal — not a new call)</i>\n",
        f"📛 Name: <b>{_esc(signal.name or signal.symbol)}</b>",
        f"🏷️ Symbol: <b>${_esc(signal.symbol)}</b>",
        f"🔗 CA: <code>{_esc(signal.contract)}</code>\n",
        f"💰 Original Entry MC: <b>{format_usd(signal.entry_market_cap)}</b>",
        f"💎 Current MC: <b>{format_usd(cur_mc)}</b>",
        f"💵 Entry Price: <b>${entry_price:.8f}</b>" if entry_price else "💵 Entry Price: <b>N/A</b>",
        f"💵 Current Price: <b>${cur_price:.8f}</b>" if cur_price else "💵 Current Price: <b>N/A</b>",
        f"🔥 Gain from original entry: <b>+{pct_gain:.0f}%</b>",
        f"📊 Current Multiple: <b>{format_x(gain)}</b>",
    ]
    if signal.ath_multiple:
        lines.append(f"🏔️ ATH: <b>{format_x(signal.ath_multiple)}</b> ({format_usd(signal.ath_market_cap)})")
    snapshot_bits = []
    if signal.total_holders is not None:
        snapshot_bits.append(f"👥 Holders: <b>{signal.total_holders}</b>")
    if signal.top_holder_pct is not None:
        snapshot_bits.append(f"🥇 Top Holder: <b>{signal.top_holder_pct:.1f}%</b>")
    if signal.dev_holding_pct is not None:
        snapshot_bits.append(f"👤 Dev Holding: <b>{signal.dev_holding_pct:.1f}%</b>")
    if signal.bundle_pct is not None:
        snapshot_bits.append(f"📦 Bundle: <b>{signal.bundle_pct:.1f}%</b>")
    if snapshot_bits:
        lines.append("")
        lines.extend(snapshot_bits)

    lines.append("")
    lines.append(f"🎯 Milestone Reached: <b>{label}</b>")
    lines.append("\n⚡ Powered by AlphaPulse")
    return "\n".join(lines)


async def send_milestone_alert(bot, signal, label, cur_mc, gain):
    from models.pump_subscription import PumpAlertSubscription
    # Reuse the exact same PUMP_ALERT_CHANNEL_IDS parser used for Signal
    # Alerts (domain/signals/pump_radar.py::get_pump_subscribers) instead
    # of a second, duplicated inline parser. The duplicate only accepted
    # numeric chat IDs (bare `int(item)`, silently dropping anything
    # starting with "@" via `except ValueError: pass`), so a public
    # channel handle like "@therealalphapulse" configured in
    # PUMP_ALERT_CHANNEL_IDS would receive Signal Alerts but never Quote
    # Milestone alerts. Both alert types now resolve channel recipients
    # through the one shared function.
    from domain.signals.pump_radar import _load_channel_ids

    async with async_session() as session:
        check = await session.execute(
            select(SignalEvent).where(
                SignalEvent.signal_id == signal.id,
                SignalEvent.status == label
            )
        )
        if check.scalar_one_or_none():
            return

        # First Milestone Snapshot / First Milestone Auto-Buy (item 1/2/3):
        # determine, atomically with the dedup check above and the
        # SignalEvent insert below (same transaction), whether this is the
        # very first milestone ever recorded for this signal. Because this
        # whole block is one transaction and signal_lifecycle_loop awaits
        # send_milestone_alert() serially per signal, this is True at most
        # once per signal, for the earliest milestone it ever crosses --
        # every later milestone on the same signal sees prior_events_count
        # > 0 and is_first_milestone False. Does NOT change which
        # milestones fire or their thresholds -- only how this one is
        # reported/whether it can trigger First Milestone Auto-Buy.
        prior_events_count = (
            await session.execute(
                select(sa_func.count(SignalEvent.id)).where(SignalEvent.signal_id == signal.id)
            )
        ).scalar() or 0
        is_first_milestone = prior_events_count == 0

        session.add(
            SignalEvent(
                signal_id=signal.id,
                status=label,
                milestone_type=_milestone_enum(label),
                milestone_value=gain,
            )
        )
        # Bumped in the same transaction as the SignalEvent row above —
        # this (not the earlier lifecycle-loop update) is the durable
        # record that this milestone has been claimed. If the process
        # dies or send_message calls below fail after this commit, the
        # milestone is at worst not re-delivered on a subsequent partial
        # failure of THIS attempt; it can no longer be silently lost by a
        # crash that happens before delivery is even attempted, which was
        # possible when this field was bumped earlier in the pipeline.
        await session.execute(
            update(SignalToken)
            .where(SignalToken.id == signal.id)
            .values(highest_alerted_multiple=gain)
        )
        await session.commit()

    pct_gain = (gain - 1) * 100

    text_msg = (
        f"🚀 <b>${signal.symbol}</b> reached <b>{label}</b>\n"
        f"🔥 From initial alert: <b>+{pct_gain:.0f}%</b>\n\n"
        f"💰 Entry: <b>{format_usd(signal.entry_market_cap)}</b>\n"
        f"💎 Current: <b>{format_usd(cur_mc)}</b>\n"
        f"📊 Multiple: <b>{format_x(gain)}</b>"
    )

    # First Milestone Snapshot text -- only ever needed/built when this is
    # the signal's first milestone; a plain-text ladder update otherwise
    # (unchanged from existing behavior).
    snapshot_text = _build_first_milestone_snapshot_text(signal, label, cur_mc, gain) if is_first_milestone else None

    msg_ids = json.loads(signal.message_ids_json or "{}")
    first_ms_ids = json.loads(getattr(signal, "first_milestone_message_ids_json", None) or "{}")
    updated_first_ms_ids = dict(first_ms_ids)

    async with async_session() as session:
        res = await session.execute(
            select(PumpAlertSubscription.user_id).where(PumpAlertSubscription.enabled == True)
        )
        db_users = res.scalars().all()

    channel_ids = _load_channel_ids()

    all_chats = list(dict.fromkeys(list(db_users) + channel_ids))

    for chat_id in all_chats:
        try:
            reply_id = msg_ids.get(str(chat_id))
            if reply_id:
                # Original Signal Alert WAS delivered to this chat --
                # existing behavior, unchanged: reply to it with the
                # normal minimal milestone update regardless of which
                # milestone this is.
                await bot.send_message(chat_id=chat_id, text=text_msg, reply_to_message_id=reply_id)
                continue

            # Original Signal Alert was NOT delivered to this chat.
            root_id = updated_first_ms_ids.get(str(chat_id))
            if is_first_milestone and not root_id:
                # First milestone for this signal, and this chat has no
                # First Milestone Snapshot root yet -- send the full
                # Snapshot (not a reply) and record it as the root for
                # every later milestone on this same token/chat. Never
                # creates a second root for the same token/chat: once
                # root_id exists it always takes the elif branch below.
                sent = await bot.send_message(chat_id=chat_id, text=snapshot_text)
                updated_first_ms_ids[str(chat_id)] = sent.message_id
            elif root_id:
                # A later milestone (2nd, 3rd, ...) for a chat whose root
                # is the First Milestone Snapshot -- quote/reply to it,
                # same minimal milestone text as the normal path above.
                await bot.send_message(chat_id=chat_id, text=text_msg, reply_to_message_id=root_id)
            else:
                # No original alert AND no First Milestone Snapshot root
                # exists for this chat (e.g. a subscriber that joined
                # after this signal's first milestone already fired) --
                # existing fallback behavior: send plain, no reply.
                await bot.send_message(chat_id=chat_id, text=text_msg)
        except Exception as e:
            logger.warning(f"Milestone reply failed for {chat_id}: {e}")

    if is_first_milestone and updated_first_ms_ids != first_ms_ids:
        await update_first_milestone_message_ids(signal.contract, updated_first_ms_ids)

    if is_first_milestone:
        # First Milestone as an Auto-Buy source (item 2/3): only ever
        # attempted here, at the signal's one-and-only first-milestone
        # moment -- never for any later milestone on this signal. Uses the
        # existing automation settings/eligibility/execution pipeline
        # unchanged; wallets that haven't selected First Milestone as an
        # auto-buy source are unaffected.
        try:
            from domain.trading.real import real_automation_engine
            await real_automation_engine.run_first_milestone_auto_buy(bot, signal)
        except Exception as e:
            logger.error(f"First Milestone auto-buy dispatch failed for {signal.contract[:8]}...: {e}")


async def build_top_signals_card(limit: int = 5) -> str:
    """
    Top Performing Signals summary card. Only includes tokens that hit at
    least the first profit milestone (+25%). Sorted by highest ROI (ATH multiple).
    """
    async with async_session() as session:
        res = await session.execute(
            select(SignalToken)
            .where(SignalToken.ath_multiple >= 1.25)
            .order_by(SignalToken.ath_multiple.desc())
            .limit(limit)
        )
        signals = res.scalars().all()

    if not signals:
        return "📭 No profitable signals yet."

    numbers = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    text_msg = "🏆 <b>Top Performing Signals</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"

    total_roi_pct = 0.0

    for i, s in enumerate(signals):
        mult = s.ath_multiple or 1.0
        roi_pct = (mult - 1) * 100
        total_roi_pct += roi_pct

        num = numbers[i] if i < len(numbers) else f"{i + 1}."
        display = f"{mult:.1f}X" if mult >= 2.0 else f"+{roi_pct:.0f}%"

        text_msg += f"{num} ${_esc(s.symbol)} – {display}\n"

    avg_roi = total_roi_pct / len(signals) if signals else 0.0

    text_msg += (
        f"\n📊 Avg ROI: <b>+{avg_roi:.0f}%</b>\n\n"
        "⚡ Powered by AlphaPulse"
    )

    return text_msg


async def build_active_signals_report(limit: int = 10) -> str:
    async with async_session() as session:
        res = await session.execute(
            select(SignalToken)
            .where(SignalToken.status == "active")
            .order_by(SignalToken.signaled_at.desc())
            .limit(limit)
        )
        signals = res.scalars().all()

    if not signals:
        return "📭 <b>No Active Signals</b>\n\nUse /pump_alerts_on to enable radar."

    text_msg = "📡 <b>Active Signals</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, s in enumerate(signals, 1):
        text_msg += (
            f"<b>{i}. {_esc(s.name or 'Unknown')} (${_esc(s.symbol)})</b>\n"
            f"   Entry MC: <b>{format_usd(s.entry_market_cap)}</b>\n"
            f"   Current: <b>{format_x(s.current_multiple or 1)}</b>\n"
            f"   Best: <b>{format_x(s.ath_multiple or 1)}</b>\n\n"
        )
    return text_msg + "━━━━━━━━━━━━━━━━━━━━━\n⚡ AlphaPulse"


async def build_winners_report(limit: int = 15) -> str:
    async with async_session() as session:
        res = await session.execute(
            select(SignalToken)
            .where(SignalToken.ath_multiple >= 1.5)
            .order_by(SignalToken.ath_multiple.desc())
            .limit(limit)
        )
        signals = res.scalars().all()

    if not signals:
        return "📭 No winners yet."

    text_msg = "🏆 <b>Top Early Trending</b>\n\n"

    for i, s in enumerate(signals):
        tag = " [TG]" if s.telegram_url else " [X]" if s.twitter_url else ""
        mult = s.ath_multiple or 1.0

        if i == 0:
            text_msg += f"🥇 {html.escape(s.name or 'Unknown')} | ${s.symbol} • {mult:.1f}X{tag}\n\n"
        elif i == 1:
            text_msg += f"🥈 {html.escape(s.name or 'Unknown')} | ${s.symbol} • {mult:.1f}X{tag}\n\n"
        elif i == 2:
            text_msg += f"🥉 {html.escape(s.name or 'Unknown')} | ${s.symbol} • {mult:.1f}X{tag}\n\n"
        else:
            text_msg += f"{i+1}️⃣ ${s.symbol} • {mult:.1f}X{tag}\n"

    return text_msg + "\n<i>Updated list of previous called tokens.</i>"


_alltime_cache = {"data": None, "ts": 0.0}
_ALLTIME_CACHE_TTL_SECONDS = 300  # 5 minutes


async def get_cached_alltime_trending(limit: int = 20) -> str:
    """
    All-time trending signals report (broader than /winners' recency-biased
    list) — sorted by highest-ever multiple across the bot's full history.
    Cached in memory for a few minutes since this scans the full signal
    table and doesn't need to be recomputed on every single call.
    """
    now = time.time()

    if _alltime_cache["data"] and (now - _alltime_cache["ts"]) < _ALLTIME_CACHE_TTL_SECONDS:
        return _alltime_cache["data"]

    async with async_session() as session:
        res = await session.execute(
            select(SignalToken)
            .where(SignalToken.ath_multiple >= 1.25)
            .order_by(SignalToken.ath_multiple.desc())
            .limit(limit)
        )
        signals = res.scalars().all()

    if not signals:
        report = "📭 No all-time trending signals yet."
    else:
        text_msg = "🌟 <b>All-Time Trending</b>\n<i>Best signals since launch</i>\n\n"
        for i, s in enumerate(signals):
            tag = " [TG]" if s.telegram_url else " [X]" if s.twitter_url else ""
            mult = s.ath_multiple or 1.0
            medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"{i + 1}️⃣"
            text_msg += f"{medal} {html.escape(s.name or 'Unknown')} | ${s.symbol} • {mult:.1f}X{tag}\n"
        report = text_msg + "\n<i>Cached — refreshes every 5 minutes.</i>"

    _alltime_cache["data"] = report
    _alltime_cache["ts"] = now
    return report


async def build_signal_status_report() -> str:
    async with async_session() as session:
        total = (await session.execute(select(sa_func.count(SignalToken.id)))).scalar() or 0
        active = (await session.execute(select(sa_func.count(SignalToken.id)).where(SignalToken.status == "active"))).scalar() or 0
        milestones = (await session.execute(select(sa_func.count(SignalEvent.id)))).scalar() or 0

    return (
        "📊 <b>Signal Tracker Status</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Total Signals: <b>{total}</b>\n"
        f"Active Tracking: <b>{active}</b>\n"
        f"Milestones Sent: <b>{milestones}</b>\n\n"
        "Reply/Tag System: Active ✅\n"
        "⚡ Powered by AlphaPulse"
    )
