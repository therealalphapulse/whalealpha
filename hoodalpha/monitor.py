"""
hoodalpha/monitor.py

The core dip-tracking / rise-detection cycle. One price fetch per
watched token per cycle (piggybacked off WhaleAlpha's own polling via
hoodalpha/db.py::fetch_current_prices, falling back to
hoodalpha/marketdata.py only when that returns nothing), then:

  1. Update lowest_dip_price if a new low is seen.
  2. Arm 'dip_confirmed' once the dip is at least HOOD_MIN_DIP_PCT below
     entry_alert_price (so 1-2% noise never arms the rise check).
  3. Once armed, fire a HoodAlert when price is >= lowest_dip_price *
     (1 + HOOD_RISE_THRESHOLD_PCT / 100), then re-arm back to
     'watching' with lowest_dip_price reset to the current price -- so
     a later, genuinely new dip cycle can still fire its own alert.
     HoodAlert.uq_hood_alert_watch_dip is the actual dedup guard: this
     re-arm is a convenience, not the safety mechanism.

Mirrors domain/signals/signal_tracker.py::signal_lifecycle_loop's shape
(per-token isolated try/except, DB write per token) without importing
anything from it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from hoodalpha.db import hood_session, fetch_current_prices
from hoodalpha.models import HoodWatch, HoodAlert
from hoodalpha.settings import (
    HOOD_MIN_DIP_PCT,
    HOOD_RISE_THRESHOLD_PCT,
    HOOD_STALE_AFTER_HOURS,
    HOOD_FALLBACK_DEXSCREENER_ENABLED,
)
from hoodalpha.telegram import format_hood_alert, broadcast

logger = logging.getLogger("HoodAlpha.Monitor")


def _now():
    # Naive UTC, matching WhaleAlpha's own DateTime columns (TIMESTAMP
    # WITHOUT TIME ZONE) -- a tz-aware datetime cannot be bound to those
    # columns by asyncpg. HoodAlpha's own tables follow the same
    # convention for consistency, though they're a separate schema.
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _resolve_price(contract: str, chain: str, piggyback: dict) -> float | None:
    hit = piggyback.get((contract, chain))
    if hit and hit.get("current_price"):
        try:
            price = float(hit["current_price"])
            if price > 0:
                return price
        except (TypeError, ValueError):
            pass

    if not HOOD_FALLBACK_DEXSCREENER_ENABLED:
        return None

    from hoodalpha.marketdata import get_price  # local import: only touches the network when needed
    return await get_price(contract, chain)


async def run_cycle(bot) -> dict:
    """Runs one full monitor pass over every non-terminal HoodWatch row.
    Returns a small summary dict for logging."""
    async with hood_session() as session:
        result = await session.execute(
            select(HoodWatch).where(HoodWatch.status.in_(["watching", "dip_confirmed"]))
        )
        watches = result.scalars().all()

    if not watches:
        return {"checked": 0, "alerts": 0, "staled": 0}

    piggyback = await fetch_current_prices([(w.contract, w.chain) for w in watches])

    alerts_sent = 0
    staled = 0

    for watch in watches:
        try:
            price = await _resolve_price(watch.contract, watch.chain, piggyback)
            now = _now()

            if price is None:
                # No price this cycle. Only go stale after a long,
                # sustained absence -- a single missed poll (API hiccup,
                # momentarily thin liquidity) must never drop a token.
                if (
                    watch.last_checked_at
                    and (now - watch.last_checked_at).total_seconds() > HOOD_STALE_AFTER_HOURS * 3600
                ):
                    async with hood_session() as session:
                        await session.execute(
                            update(HoodWatch).where(HoodWatch.id == watch.id).values(status="stale")
                        )
                        await session.commit()
                    staled += 1
                continue

            values = {"last_price": price, "last_checked_at": now}

            new_low = price < watch.lowest_dip_price
            if new_low:
                values["lowest_dip_price"] = price
                values["lowest_dip_at"] = now

            effective_low = price if new_low else watch.lowest_dip_price
            dip_pct = (
                (watch.entry_alert_price - effective_low) / watch.entry_alert_price * 100
                if watch.entry_alert_price
                else 0.0
            )

            new_status = watch.status
            if watch.status == "watching" and dip_pct >= HOOD_MIN_DIP_PCT:
                new_status = "dip_confirmed"
                values["status"] = new_status

            async with hood_session() as session:
                await session.execute(
                    update(HoodWatch).where(HoodWatch.id == watch.id).values(**values)
                )
                await session.commit()

            armed = new_status == "dip_confirmed"
            if armed and effective_low > 0:
                rise_pct = (price - effective_low) / effective_low * 100
                if rise_pct >= HOOD_RISE_THRESHOLD_PCT:
                    fired = await _fire_alert(bot, watch, effective_low, price, rise_pct)
                    if fired:
                        alerts_sent += 1

        except Exception as e:
            # Isolated per-watch: one bad row must never block the rest
            # of the cycle, matching WhaleAlpha's own per-signal
            # isolation pattern.
            logger.error("Monitor error for watch id=%s contract=%s: %s", watch.id, watch.contract, e)

    return {"checked": len(watches), "alerts": alerts_sent, "staled": staled}


async def _fire_alert(bot, watch: HoodWatch, dip_price: float, alert_price: float, rise_pct: float) -> bool:
    """Inserts the HoodAlert dedup row FIRST (unique on watch_id+dip_price
    catches a duplicate fire before any Telegram send is attempted), then
    sends, then re-arms the watch for a future dip cycle. If the insert
    hits the unique constraint, another cycle/replica already alerted on
    this exact dip -- skip silently."""
    from sqlalchemy.exc import IntegrityError

    try:
        async with hood_session() as session:
            alert = HoodAlert(
                watch_id=watch.id, dip_price=dip_price, alert_price=alert_price, rise_pct=rise_pct
            )
            session.add(alert)
            await session.commit()
    except IntegrityError:
        return False  # already alerted for this dip_price -- duplicate prevented

    text = format_hood_alert(watch, alert_price, rise_pct)
    sent_count = await broadcast(bot, text)
    if sent_count == 0:
        logger.warning(
            "HoodAlert for watch id=%s recorded but delivered to 0 chats "
            "(check HOOD_TELEGRAM_CHAT_IDS / bot permissions)", watch.id
        )

    # Re-arm for a future, genuinely new dip cycle -- the dedup guard
    # above (not this re-arm) is what actually prevents duplicate
    # alerts, so re-arming is safe even if this step partially fails.
    async with hood_session() as session:
        await session.execute(
            update(HoodWatch)
            .where(HoodWatch.id == watch.id)
            .values(status="watching", lowest_dip_price=alert_price, lowest_dip_at=_now())
        )
        await session.commit()

    logger.info(
        "HoodAlert fired: %s/%s dip=%.8f -> now=%.8f (+%.1f%%), delivered to %d chat(s)",
        watch.contract[:10], watch.chain, dip_price, alert_price, rise_pct, sent_count,
    )
    return True
