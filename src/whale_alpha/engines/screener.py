"""DexScreener-first token screener for Whale Alpha production."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from whale_alpha.config import Env
from whale_alpha.db.models import User
from whale_alpha.engines.market_regime import classify_market_regime, market_regime_gate
from whale_alpha.engines.token_hunter import (
    _persist,
    build_alert_keyboard,
    format_alert,
    prefilter_candidates,
    score_token,
)
from whale_alpha.integrations.token_hunter_sources import (
    DiscoveryCandidate,
    _fetch_dexscreener_pairs,
    _provider,
    _retry,
    discover_dexscreener_fallback_candidates,
)
from whale_alpha.utils.http_retry import get_all_provider_metrics
from whale_alpha.utils.logger import child_logger

log = child_logger("tokenScreener")


async def discover_dexscreener_profiles(client: Any, env: Env) -> list[DiscoveryCandidate]:
    """Discover fresh Solana token addresses from DexScreener's latest token-profile feed,
    then resolve real pair market data through DexScreener's tokens endpoint.

    Token boosts are not used as the primary feed: paid promotion is a poor proxy for
    organic early opportunity. If profiles are temporarily empty, fall back to the existing
    DexScreener boost source so the screener remains live rather than silently stopping.
    """
    if not env.DISCOVERY_DEXSCREENER_ENABLED:
        return []

    url = f"{env.DISCOVERY_DEXSCREENER_API_BASE}/token-profiles/latest/v1"
    result = await _provider(env, "dexscreener").get(client, url, max_retries=0, base_backoff_seconds=0, max_backoff_seconds=0)
    if result.response is None or result.response.status_code >= 400:
        log.warning("DexScreener profile discovery failed", status=result.response.status_code if result.response else None)
        return await discover_dexscreener_fallback_candidates(client, env, env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE)

    try:
        payload = result.response.json()
    except ValueError as err:
        log.warning("DexScreener profile discovery returned invalid JSON", err=str(err))
        return []

    addresses: list[str] = []
    seen: set[str] = set()
    entries = payload if isinstance(payload, list) else []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("chainId") != "solana":
            continue
        address = entry.get("tokenAddress")
        if isinstance(address, str) and address and address not in seen:
            seen.add(address)
            addresses.append(address)
        if len(addresses) >= env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE:
            break

    if not addresses:
        return await discover_dexscreener_fallback_candidates(client, env, env.TOKEN_HUNTER_MAX_DISCOVERY_PER_SOURCE)

    candidates = await _fetch_dexscreener_pairs(client, env, addresses)
    log.info("dexscreener_profile_discovery", profiles=len(addresses), resolved=len(candidates))
    return candidates


async def run_screener_cycle(
    env: Env,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    client: Any,
    connection: Any | None = None,
) -> dict[str, int]:
    """Run DexScreener -> cheap filters -> score -> signal alert."""
    now = datetime.now(UTC)
    funnel = {
        "discovered": 0,
        "basic_filter_passed": 0,
        "quality_gate_passed": 0,
        "scored": 0,
        "approved": 0,
        "alert_attempted": 0,
        "alert_delivered": 0,
        "deep_checked": 0,
        "deep_confirmed": 0,
    }

    candidates = await discover_dexscreener_profiles(client, env)
    funnel["discovered"] = len(candidates)

    prequalified, counts = prefilter_candidates(candidates, now=now, env=env)
    funnel.update(counts)

    if not prequalified:
        log.info("WHALE ALPHA SCREENER CYCLE COMPLETE", **funnel)
        log.info("WHALE ALPHA SCREENER PROVIDER TELEMETRY", providers=get_all_provider_metrics())
        return funnel

    regime = None
    if env.TOKEN_HUNTER_MARKET_REGIME_ENABLED and len(prequalified) >= env.TOKEN_HUNTER_MARKET_REGIME_MIN_DATA:
        regime = classify_market_regime([c.snapshot for c in prequalified])
    log.info(
        "screener_market_regime",
        name=regime.name if regime else "INSUFFICIENT_DATA",
        score=regime.score if regime else None,
        breadth_pct=regime.breadth_pct if regime else None,
        sample=len(prequalified),
    )

    async with session_factory() as session:
        result = await session.execute(select(User.telegram_id).where(User.notify_signals.is_(True)))
        subscriber_ids = [str(x) for x in result.scalars().all()]
        admin_ids = set(env.admin_telegram_ids)
        recipients = sorted(admin_ids) if admin_ids else list(dict.fromkeys(subscriber_ids))

        ranked = []
        for candidate in prequalified:
            created = candidate.snapshot.created_at_ms
            age = max(0.0, (now - datetime.fromtimestamp(created / 1000, tz=UTC)).total_seconds() / 60) if created else 0.0
            score = score_token(candidate.snapshot, age_minutes=age, market_regime=regime)
            funnel["scored"] += 1
            severe = {"NO_LIQUIDITY_DATA", "VOLUME_WITHOUT_TRANSACTION_DEPTH", "EXTREME_TRADE_SIZE"}
            gate_ok = True
            gate_flags: tuple[str, ...] = ()
            if regime is not None:
                gate_ok, gate_flags = market_regime_gate(
                    candidate.snapshot,
                    regime,
                    score=score.total,
                    severe_flags=severe.intersection(score.risk_flags),
                    risk_off_min_score=env.TOKEN_HUNTER_RISK_OFF_MIN_SCORE,
                    neutral_min_score=env.TOKEN_HUNTER_NEUTRAL_MIN_SCORE,
                    risk_on_min_score=env.TOKEN_HUNTER_RISK_ON_MIN_SCORE,
                )
            snap = candidate.snapshot
            buy_count = snap.buys_5m
            sell_count = snap.sells_5m
            total_txns = buy_count + sell_count
            buy_sell_ratio = buy_count / max(sell_count, 1)
            liq_mc_ratio = (snap.liquidity_usd / snap.market_cap_usd) if snap.liquidity_usd and snap.market_cap_usd else 0.0
            # Release gate: a signal is a rare high-potential call, not a generic
            # DexScreener discovery alert. These hard floors cannot be weakened by
            # an environment override. Missing/weak market structure stays NO SIGNAL.
            high_potential_gate = (
                score.total >= max(env.TOKEN_HUNTER_ALERT_MIN_SCORE, 88)
                and score.risk_level == "LOW"
                and age <= 180
                and snap.market_cap_usd is not None
                and 10_000 <= snap.market_cap_usd <= 5_000_000
                and snap.liquidity_usd is not None
                and snap.liquidity_usd >= 15_000
                and liq_mc_ratio >= 0.05
                and snap.volume_5m_usd >= 10_000
                and total_txns >= 15
                and buy_count >= 8
                and buy_sell_ratio >= 1.25
                and snap.price_change_5m_pct > -5
                and snap.volume_5m_usd / max(snap.liquidity_usd, 1.0) <= 2.0
                and not severe.intersection(score.risk_flags)
            )
            eligible = gate_ok and high_potential_gate
            ranked.append((score.total, eligible, candidate, score, age, gate_flags))

        ranked.sort(key=lambda x: x[0], reverse=True)
        fast_eligible = [x for x in ranked if x[1]][:3]
        deep_results = {}
        if fast_eligible:
            # Fast scoring is discovery intelligence; the existing reversal evaluator is
            # the expensive evidence layer. Only the top three candidates reach it so we
            # can use holders, smart-money/top-trader flow, OHLCV structure, security and
            # cross-source reconciliation without turning every discovery cycle into a
            # provider storm. Any deep-evaluation failure is fail-closed.
            deep = await evaluate_candidates(
                client, env, [x[2] for x in fast_eligible], connection, now
            )
            deep_results = {item.snapshot.mint: item for item in deep}
            funnel["deep_checked"] = len(deep)

        for _, eligible, candidate, score, age, gate_flags in ranked[: env.TOKEN_HUNTER_MAX_UNIQUE_PER_CYCLE]:
            if not eligible:
                log.info(
                    "screener_rejected",
                    mint=candidate.snapshot.mint,
                    score=score.total,
                    risk=score.risk_level,
                    flags=list(score.risk_flags),
                    market_gate=list(gate_flags),
                )
                continue

            deep = deep_results.get(candidate.snapshot.mint)
            if deep is None or not deep.approved or deep.score < max(env.WHALE_ALPHA_MIN_CONFIDENCE, 87):
                log.info(
                    "screener_deep_rejected",
                    mint=candidate.snapshot.mint,
                    fast_score=score.total,
                    deep_score=deep.score if deep else None,
                    findings=list(deep.hard_rejects) if deep else ["DEEP_EVALUATION_NOT_RUN"],
                )
                continue

            # Final conviction blends broad market screening with independent deep
            # structure/flow/on-chain evidence. The alert cannot be released on the
            # fast score alone.
            conviction = round(score.total * 0.35 + deep.score * 0.65, 2)
            if conviction < 90:
                log.info(
                    "screener_conviction_rejected",
                    mint=candidate.snapshot.mint,
                    fast_score=score.total,
                    deep_score=deep.score,
                    conviction=conviction,
                )
                continue

            funnel["deep_confirmed"] += 1
            o = await _persist(session, candidate.snapshot, score, "dexscreener", now, age)
            funnel["approved"] += 1
            if o.last_alerted_at is not None and now - o.last_alerted_at < timedelta(minutes=env.TOKEN_HUNTER_ALERT_COOLDOWN_MINUTES):
                continue

            text = format_alert(candidate.snapshot, score, age, now) + f"\n\n<b>Conviction:</b> {conviction:.1f}/100"
            delivered = 0
            message_ids: dict[str, int] = {}
            errors: list[str] = []
            for chat_id in recipients:
                try:
                    msg = await bot.send_message(
                        chat_id=int(chat_id),
                        text=text,
                        parse_mode="HTML",
                        reply_markup=build_alert_keyboard(candidate.snapshot),
                    )
                    message_ids[str(chat_id)] = msg.message_id
                    delivered += 1
                except (TelegramAPIError, ValueError) as err:
                    errors.append(str(err))
            o.alert_attempted_at = now
            o.alert_status = "DELIVERED" if delivered else "FAILED"
            o.alert_error = "; ".join(errors)[:1000] if errors else None
            if delivered:
                o.alert_delivered_at = now
                o.last_alerted_at = now
                o.alert_reference_price_usd = candidate.snapshot.price_usd
                o.alert_message_ids = message_ids
                funnel["alert_delivered"] += delivered
            funnel["alert_attempted"] += 1

        await session.commit()

    log.info("WHALE ALPHA SCREENER CYCLE COMPLETE", **funnel)
    log.info("WHALE ALPHA SCREENER PROVIDER TELEMETRY", providers=get_all_provider_metrics())
    return funnel


def start_screener_loop(
    env: Env,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    client: Any,
    connection: Any | None = None,
) -> Callable[[], Any]:
    async def worker() -> None:
        await asyncio.sleep(env.TOKEN_HUNTER_STARTUP_DELAY_SECONDS)
        while True:
            try:
                await run_screener_cycle(env, session_factory, bot, client, connection)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.exception("Screener cycle failed", err=str(err))
            await asyncio.sleep(env.TOKEN_HUNTER_INTERVAL_SECONDS)

    task = asyncio.create_task(worker(), name="token-screener")

    async def stop() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return stop
