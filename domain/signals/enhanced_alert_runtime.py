"""Runtime improvements for Signal/Quote Alerts.

Important policy: this module does NOT change PumpRadar's token-selection
filters, hard rejects, score calculation, holder requirements, or quota.
It improves candidate discovery by adding an older-token reactivation source,
then sends every candidate through the existing PumpRadar qualification
pipeline unchanged. It also improves how qualified candidates are delivered
and how profit milestones are quoted after a signal exists.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from infra.db.session import async_session
from models.signal_token import SignalToken
from providers.marketdata.dexscreener import get_token_card_info
from providers.marketdata._resilience import get_json

from domain.signals.pump_radar import (
    scan_for_pump_candidates,
    analyze_candidate,
    _is_pump_fun,
    get_pump_subscribers,
    has_quota_remaining,
    maybe_adjust_cutoff,
    was_already_alerted,
    create_signal_from_candidate,
    update_signal_message_ids,
    bump_signal_count_and_maybe_broadcast,
    auto_buy_for_new_signal,
    send_pump_card,
    mark_alerted,
)
from domain.signals.scoring import passes_mc_liquidity_gate
from domain.signals.signal_tracker import (
    migrate_signal_schema,
    send_milestone_alert,
    mark_signal_alert_delivered,
    _to_float,
)

logger = logging.getLogger("AlphaPulse.EnhancedAlerts")

# This is a delivery/throughput setting, NOT a token-selection filter.
# Existing scoring, hard rejects, confidence gates, and quota remain intact.
SIGNALS_PER_CYCLE = 10

# Quote alerts are milestone based rather than "every new ATH" based.
# This prevents +26%, +27%, +28%... spam while preserving useful profit
# notifications. The existing +25/+50/2X/3X... milestone vocabulary is kept.
QUOTE_MILESTONES = (
    (1.25, "+25%"),
    (1.50, "+50%"),
    (2.00, "2X"),
    (3.00, "3X"),
    (4.00, "4X"),
    (5.00, "5X"),
    (6.00, "6X"),
    (10.00, "10X"),
)
MIN_24H_VOLUME_FOR_QUOTE_ALERT = 500.0

# Reactivation discovery is deliberately broad and separate from the
# qualification pipeline. It only finds older Pump.fun tokens showing a
# meaningful change in market activity. Every discovered contract is then
# passed to analyze_candidate(), which applies the existing MC/liquidity,
# volume, security, holder, hard-gate, score, verified-risk, confidence and
# quota logic unchanged.
REACTIVATION_MIN_AGE_HOURS = 48
REACTIVATION_MIN_VOLUME_1H = 1500.0
REACTIVATION_MIN_ACCELERATION = 1.25
REACTIVATION_MIN_BUY_RATIO = 0.50
REACTIVATION_SOURCE_LIMIT = 40
REACTIVATION_QUALIFIED_LIMIT = 15


def _next_quote_milestone(gain: float, last_alerted: float) -> tuple[float, str] | None:
    """Return the highest newly crossed milestone, if any."""
    crossed = [item for item in QUOTE_MILESTONES if gain >= item[0] and item[0] > last_alerted]
    return max(crossed, key=lambda item: item[0]) if crossed else None


def _parse_pool_created_at(value: str | None) -> float | None:
    if not value:
        return None
    try:
        created = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return created.timestamp()
    except (TypeError, ValueError):
        return None


async def fetch_reactivation_candidates(limit: int = REACTIVATION_QUALIFIED_LIMIT) -> list[str]:
    """Discover older Pump.fun tokens whose market activity is accelerating.

    Sources are GeckoTerminal's Solana top-pool and 1h-trending views. This
    is a discovery layer only. It does not replace or weaken any existing
    AlphaPulse qualification gate.

    A token enters the reactivation shortlist when it is at least 48 hours
    old and shows enough 1h activity to be meaningful, with either volume
    acceleration, buy pressure, or positive short-term price momentum. The
    final decision remains entirely inside analyze_candidate().

    Before returning the shortlist, the same existing cheap MC/liquidity
    gate used by PumpRadar is applied as a discovery prefilter. This is NOT
    a new filter and does not alter its thresholds; it simply prevents the
    reactivation source from spending its expensive analysis budget on
    obviously out-of-universe tokens (for example a $9.8M token when the
    existing signal universe ceiling is $1.5M). analyze_candidate() still
    runs the authoritative gate again.
    """
    urls = (
        "https://api.geckoterminal.com/api/v2/networks/solana/pools"
        "?include=base_token&page=1&sort=h24_volume_usd_desc",
        "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
        "?include=base_token&page=1&duration=1h",
    )

    ranked: dict[str, float] = {}
    seen = set()

    for url in urls:
        try:
            payload = await get_json(url, cache_ttl_seconds=30, timeout_seconds=10)
            if not payload:
                continue

            token_map: dict[str, str] = {}
            for item in payload.get("included", []) or []:
                if item.get("type") != "token":
                    continue
                attrs = item.get("attributes") or {}
                address = attrs.get("address") or ""
                item_id = item.get("id") or ""
                if address:
                    token_map[item_id] = address

            for pool in payload.get("data", []) or []:
                attrs = pool.get("attributes") or {}
                base_rel = (
                    pool.get("relationships", {})
                    .get("base_token", {})
                    .get("data", {})
                    .get("id", "")
                )
                mint = token_map.get(base_rel, "")
                if not mint or not _is_pump_fun(mint) or mint in seen:
                    continue

                created_ts = _parse_pool_created_at(attrs.get("pool_created_at"))
                if created_ts is None:
                    continue

                age_hours = (datetime.now(timezone.utc).timestamp() - created_ts) / 3600.0
                if age_hours < REACTIVATION_MIN_AGE_HOURS:
                    continue

                volume = attrs.get("volume_usd") or {}
                vol_1h = _to_float(volume.get("h1"))
                vol_24h = _to_float(volume.get("h24"))
                if vol_1h < REACTIVATION_MIN_VOLUME_1H or vol_24h <= 0:
                    continue

                txns = attrs.get("transactions") or {}
                h1 = txns.get("h1") or {}
                buys = _to_float(h1.get("buys"))
                sells = _to_float(h1.get("sells"))
                total = buys + sells
                buy_ratio = buys / total if total > 0 else 0.0

                acceleration = vol_1h / max(vol_24h / 24.0, 1.0)
                price_change = _to_float((attrs.get("price_change_percentage") or {}).get("h1"))

                # Discovery score only. This is intentionally not an
                # AlphaPulse qualification score and cannot create a signal.
                activity_score = min(acceleration, 4.0) * 20.0
                activity_score += max(0.0, min((buy_ratio - 0.50) * 100.0, 25.0))
                activity_score += max(0.0, min(price_change, 25.0))

                qualifies_for_shortlist = (
                    acceleration >= REACTIVATION_MIN_ACCELERATION
                    or buy_ratio >= REACTIVATION_MIN_BUY_RATIO
                    or price_change >= 3.0
                )
                if not qualifies_for_shortlist:
                    continue

                ranked[mint] = max(ranked.get(mint, 0.0), activity_score)
                seen.add(mint)

        except Exception as exc:
            logger.warning("Reactivation discovery source failed (non-fatal): %s", exc)

    ordered = sorted(ranked, key=ranked.get, reverse=True)[:REACTIVATION_SOURCE_LIMIT]

    # The previous implementation returned the top activity candidates
    # directly. In production the first sample showed the downside: a very
    # active but $9.8M token consumed the reactivation slot only to be
    # rejected immediately by the existing $1.5M market-cap ceiling. Enrich
    # the shortlist with the same market-data card used by the main pipeline,
    # apply the exact existing gate, and keep scanning until we have enough
    # candidates that actually belong to the current AlphaPulse universe.
    eligible: list[str] = []
    rejected_by_universe = 0

    for mint in ordered:
        if len(eligible) >= limit:
            break
        try:
            card = await get_token_card_info(mint)
            if not card:
                # Do not reject on missing enrichment here. Let the
                # authoritative analyze_candidate() decide if the candidate
                # is otherwise usable.
                eligible.append(mint)
                continue

            passes_gate, reason = passes_mc_liquidity_gate(card)
            if not passes_gate:
                rejected_by_universe += 1
                logger.info(
                    "Reactivation prefilter skipped %s: existing MC/liquidity gate — %s",
                    mint[:8],
                    reason,
                )
                continue

            eligible.append(mint)
        except Exception as exc:
            # Discovery must never die because enrichment is temporarily
            # unavailable. Keep the candidate and let analyze_candidate()
            # perform the authoritative checks.
            logger.warning(
                "Reactivation prefilter enrichment failed for %s (non-fatal): %s",
                mint[:8],
                exc,
            )
            eligible.append(mint)
        await asyncio.sleep(0.15)

    logger.info(
        "🔎 Reactivation Radar: %d older Pump.fun candidates shortlisted "
        "(%d activity candidates checked, %d excluded by existing MC/liquidity gate)",
        len(eligible),
        len(ordered),
        rejected_by_universe,
    )
    return eligible


async def scan_for_broad_candidates(max_results: int = SIGNALS_PER_CYCLE) -> list[dict]:
    """Combine fresh-launch discovery with older-token reactivation discovery.

    The fresh PumpRadar scan remains intact. Reactivation candidates are
    simply additional inputs to the same analyze_candidate() qualification
    function. No existing token filter or score threshold is bypassed.
    """
    # Reserve half of the cycle capacity for the new discovery source so a
    # strong fresh-token batch cannot starve older-token reactivation scans.
    fresh_limit = max(1, max_results // 2)
    fresh = await scan_for_pump_candidates(max_results=fresh_limit)
    by_contract = {candidate["contract"]: candidate for candidate in fresh}

    reactivation = await fetch_reactivation_candidates()
    for mint in reactivation:
        if mint in by_contract:
            continue

        candidate = await analyze_candidate(mint)
        if candidate:
            by_contract[mint] = candidate

        await asyncio.sleep(0.8)

    results = sorted(
        by_contract.values(),
        key=lambda item: item["pump"]["score"],
        reverse=True,
    )
    logger.info(
        "Qualified broad-discovery candidates this cycle: %d (fresh + reactivation)",
        len(results[:max_results]),
    )
    return results[:max_results]


async def redeliver_undelivered_signals(
    bot,
    subscribers: list[int],
) -> int:
    """Retry recently-created active signals that received zero messages.

    Delivery state is determined exclusively from message_ids_json.
    alert_delivered is intentionally untouched because it is also consumed
    by the real-money automation path.

    This recovery path does not create new SignalToken rows and does not
    invoke auto_buy_for_new_signal(). It reuses the existing candidate
    qualification and alert-delivery path.
    """
    if not subscribers:
        return 0

    cutoff = datetime.utcnow() - timedelta(hours=6)
    delivered_count = 0

    try:
        async with async_session() as session:
            result = await session.execute(
                select(SignalToken).where(
                    SignalToken.status == "active",
                    SignalToken.signaled_at >= cutoff,
                    or_(
                        SignalToken.message_ids_json.is_(None),
                        SignalToken.message_ids_json == "{}",
                        SignalToken.message_ids_json == "",
                    ),
                )
            )
            signals = result.scalars().all()

        if not signals:
            return 0

        logger.info(
            "♻️ Undelivered-signal recovery found %d recent signal(s)",
            len(signals),
        )

        for signal in signals:
            try:
                candidate = await analyze_candidate(signal.contract)

                if not candidate:
                    logger.info(
                        "Undelivered signal %s no longer qualifies; skipping recovery",
                        signal.contract[:8],
                    )
                    continue

                msg_ids: dict[str, int] = {}
                delivery_failures = 0

                for chat_id in subscribers:
                    try:
                        msg = await send_pump_card(
                            bot,
                            chat_id,
                            candidate,
                        )

                        if msg and hasattr(msg, "message_id"):
                            msg_ids[str(chat_id)] = msg.message_id

                    except Exception as exc:
                        delivery_failures += 1
                        logger.warning(
                            "Redelivery failed for chat %s / %s: %s",
                            chat_id,
                            signal.contract[:8],
                            exc,
                        )

                    await asyncio.sleep(0.1)

                if msg_ids:
                    await update_signal_message_ids(
                        signal.contract,
                        msg_ids,
                    )
                    delivered_count += 1

                    logger.info(
                        "♻️ Redelivered signal %s to %d/%d subscribers",
                        signal.contract[:8],
                        len(msg_ids),
                        len(subscribers),
                    )

                if delivery_failures:
                    logger.warning(
                        "Signal %s redelivery: %d delivered, %d failed",
                        signal.contract[:8],
                        len(msg_ids),
                        delivery_failures,
                    )

            except Exception as exc:
                logger.warning(
                    "Undelivered-signal recovery failed for %s: %s",
                    signal.contract[:8],
                    exc,
                )

    except Exception as exc:
        logger.error(
            "Undelivered-signal recovery sweep failed: %s",
            exc,
        )

    return delivered_count


async def enhanced_pump_radar_loop(bot, interval_seconds: int = 45):
    """Deliver all qualified candidates in a cycle instead of one.

    Discovery now includes both fresh Pump.fun launches and older Pump.fun
    tokens showing renewed activity. Both paths converge on the exact same
    qualification pipeline.
    """
    logger.info(
        "🚀 Enhanced Signal Alerts Active — up to %d qualified signals/cycle; "
        "fresh + reactivation discovery; existing filters unchanged",
        SIGNALS_PER_CYCLE,
    )

    while True:
        try:
            try:
                await maybe_adjust_cutoff()
            except Exception as exc:
                logger.warning("Quota cutoff adjustment failed (non-fatal): %s", exc)

            subscribers = await get_pump_subscribers()
            if not subscribers:
                logger.info("No signal-alert subscribers; waiting")
                await asyncio.sleep(20)
                continue

            recovered_alerts = await redeliver_undelivered_signals(
                bot,
                subscribers,
            )

            if recovered_alerts:
                logger.info(
                    "♻️ Recovered %d previously-undelivered signal alert(s)",
                    recovered_alerts,
                )

            if not await has_quota_remaining():
                logger.info("Daily alert cap reached — holding until next UTC day")
                await asyncio.sleep(interval_seconds)
                continue

            candidates = await scan_for_broad_candidates(max_results=SIGNALS_PER_CYCLE)

            if not candidates:
                logger.info("No qualified fresh/reactivation candidates this cycle")
                await asyncio.sleep(interval_seconds)
                continue

            alerts_sent = 0

            for candidate in candidates:
                if not await has_quota_remaining():
                    logger.info("Daily alert cap reached mid-cycle — stopping")
                    break

                mint = candidate["contract"]
                if await was_already_alerted(mint):
                    continue

                created = await create_signal_from_candidate(candidate)
                if not created:
                    logger.info("Signal creation skipped for %s", mint[:8])
                    continue

                try:
                    await bump_signal_count_and_maybe_broadcast(bot)
                except Exception as exc:
                    logger.error("Signal counter/broadcast failed (non-fatal): %s", exc)

                # Production incident fix (mirrors domain/signals/pump_radar.py's
                # pump_radar_loop): SignalToken creation above only proves this
                # candidate passed qualification. It is NOT evidence anyone was
                # ever alerted, and auto-buy must never fire on it alone. Deliver
                # the Signal Alert card to subscribers first, and only mark the
                # signal alert-delivered (and therefore buy-eligible for both the
                # paper auto-buy call below and the real-money automation
                # engine's independent poll) after confirming at least one
                # subscriber genuinely received it this cycle.
                msg_ids: dict[str, int] = {}
                delivery_failures = 0
                for chat_id in subscribers:
                    try:
                        msg = await send_pump_card(bot, chat_id, candidate)
                        if msg and hasattr(msg, "message_id"):
                            msg_ids[str(chat_id)] = msg.message_id
                    except Exception as exc:
                        delivery_failures += 1
                        logger.warning(
                            "Pump card delivery failed for chat %s: %s", chat_id, exc
                        )
                    await asyncio.sleep(0.1)

                if msg_ids:
                    await update_signal_message_ids(mint, msg_ids)

                if delivery_failures:
                    logger.warning(
                        "Signal %s delivered to %d/%d subscribers (%d failed)",
                        mint[:8], len(msg_ids), len(subscribers), delivery_failures,
                    )

                if msg_ids:
                    # At least one subscriber genuinely received the Signal
                    # Alert card this cycle -- only now may either auto-buy
                    # path treat this signal as buy-eligible.
                    try:
                        await mark_signal_alert_delivered(mint)
                    except Exception as exc:
                        logger.error("mark_signal_alert_delivered failed (non-fatal): %s", exc)

                    try:
                        await auto_buy_for_new_signal(bot, candidate)
                    except Exception as exc:
                        logger.error("Auto-buy for signal failed (non-fatal): %s", exc)
                else:
                    logger.warning(
                        "Signal alert for %s: delivered to 0/%d subscribers -- "
                        "skipping auto-buy for this signal (no confirmed Signal "
                        "Alert delivery).",
                        mint[:8], len(subscribers),
                    )

                await mark_alerted(
                    mint,
                    candidate["data"].get("symbol"),
                    candidate["pump"]["score"],
                    name=candidate["data"].get("name"),
                )
                alerts_sent += 1

            if alerts_sent:
                logger.info(
                    "Sent %d qualified signal alert(s) to %d subscriber(s) this cycle",
                    alerts_sent,
                    len(subscribers),
                )

        except Exception as exc:
            logger.error("Enhanced Pump Radar Error: %s", exc)

        await asyncio.sleep(interval_seconds)


async def enhanced_signal_lifecycle_loop(bot, interval_seconds: int = 90):
    """Track active signals and send useful milestone quote alerts.

    Existing token filters are completely untouched. The improvement is in
    the downstream quote behavior: a signal gets one alert when it crosses
    the next meaningful profit milestone instead of one alert for every tiny
    new ATH. Current/ATH tracking continues even when a quote is suppressed.
    """
    await migrate_signal_schema()
    logger.info("📈 Enhanced Signal Lifecycle + Quote Alerts Active")

    while True:
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(SignalToken).where(SignalToken.status == "active")
                )
                signals = result.scalars().all()

            for signal in signals:
                try:
                    data = await get_token_card_info(signal.contract)
                    if not data:
                        continue

                    current_mc = _to_float(data.get("market_cap")) or _to_float(data.get("fdv"))
                    if not signal.entry_market_cap or signal.entry_market_cap <= 0 or current_mc <= 0:
                        continue

                    gain = current_mc / signal.entry_market_cap
                    volume_24h = _to_float(data.get("volume_24h"))
                    currently_trading = volume_24h >= MIN_24H_VOLUME_FOR_QUOTE_ALERT

                    last_alerted = signal.highest_alerted_multiple or 1.0
                    milestone = (
                        _next_quote_milestone(gain, last_alerted)
                        if currently_trading
                        else None
                    )

                    async with async_session() as session2:
                        values = {
                            "current_market_cap": current_mc,
                            "current_multiple": gain,
                        }
                        if gain > (signal.ath_multiple or 1.0):
                            values["ath_multiple"] = gain
                            values["ath_market_cap"] = current_mc
                        await session2.execute(
                            update(SignalToken)
                            .where(SignalToken.id == signal.id)
                            .values(**values)
                        )
                        await session2.commit()

                    if milestone:
                        threshold, label = milestone
                        logger.info(
                            "Quote milestone crossed: %s %s (current %.2fx)",
                            signal.contract[:8], label, gain,
                        )
                        await send_milestone_alert(
                            bot, signal, label, current_mc, gain
                        )

                except Exception as exc:
                    logger.error(
                        "Enhanced signal lifecycle error for %s (%s): %s",
                        signal.id,
                        signal.contract,
                        exc,
                    )

                await asyncio.sleep(0.5)

        except Exception as exc:
            logger.error("Enhanced Signal Lifecycle Error: %s", exc)

        await asyncio.sleep(interval_seconds)
