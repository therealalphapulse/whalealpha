"""High-precision early Solana token opportunity detector."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ReplyParameters
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from whale_alpha.config import Env
from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot, enrich_tokens
from whale_alpha.integrations.token_age import resolve_token_ages
from whale_alpha.utils.logger import child_logger

from whale_alpha.db.models import TokenOpportunity, TokenSnapshot, User, WalletEvent, WalletStatus, WhaleWallet
from whale_alpha.integrations.token_hunter_sources import DiscoveryCandidate, discover_token_candidates

log = child_logger("tokenHunter")


def alert_recipient_ids(admin_ids: set[str], subscriber_ids: list[str]) -> list[str]:
    """Return admin recipients when configured, otherwise subscribed users."""
    if admin_ids:
        return sorted(admin_ids)
    return list(dict.fromkeys(str(chat_id) for chat_id in subscriber_ids if str(chat_id).strip()))


def quote_milestones_for_gain(gain_pct: float) -> list[int]:
    """Return crossed quote milestones: +25/+50/+75/+100%, then each whole X."""
    if gain_pct < 25:
        return []
    milestones = [m for m in range(25, 101, 25) if gain_pct >= m]
    if gain_pct >= 200:
        milestones.extend(range(200, int(gain_pct // 100) * 100 + 1, 100))
    return milestones


def format_quote_alert(o: TokenOpportunity, gain_pct: float, milestone_pct: int, price_usd: float) -> str:
    multiple = 1 + (gain_pct / 100)
    milestone_multiple = 1 + (milestone_pct / 100)
    milestone_label = f"{milestone_multiple:.0f}x" if milestone_pct >= 200 else f"+{milestone_pct}%"
    return (
        f"📈 <b>WHALE ALPHA • QUOTE ALERT</b>\n"
        f"<i>{escape(o.symbol or o.name or o.mint[:8])} hit {escape(milestone_label)}</i>\n\n"
        f"🪙 <b>${escape(o.symbol or 'TOKEN')}</b>\n"
        f"🚀 <b>Gain:</b> +{gain_pct:.1f}%  <b>({multiple:.2f}x)</b>\n"
        f"🎯 <b>Milestone:</b> {escape(milestone_label)}\n"
        f"💵 <b>Price:</b> ${price_usd:.10f}\n"
        f"📊 <b>From signal:</b> ${o.alert_reference_price_usd:.10f}\n\n"
        f"🔥 <b>Momentum confirmed.</b>"
    )


@dataclass(frozen=True)
class TokenScore:
    total: float
    components: dict[str, float]
    risk_level: str
    risk_flags: tuple[str, ...]
    reasons: tuple[str, ...]


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def age_score(minutes: float) -> float:
    if minutes <= 10:
        return 100
    if minutes <= 30:
        return 92
    if minutes <= 60:
        return 82
    if minutes <= 180:
        return 68
    if minutes <= 360:
        return 48
    if minutes <= 720:
        return 25
    return 0


def acceleration_score(
    short_value: float, long_value: float, short_minutes: float = 5, long_minutes: float = 60
) -> float:
    sr = max(0, short_value) / short_minutes
    lr = max(0, long_value) / long_minutes
    if lr <= 0:
        return 100 if sr > 0 else 0
    return clamp((sr / lr - 0.5) * 55 + 50)


def imbalance_score(buys: int, sells: int) -> float:
    total = buys + sells
    if total <= 0:
        return 0
    return clamp(50 + (buys / total - 0.5) * 250)


def liquidity_health_score(mc: float | None, liq: float | None) -> tuple[float, list[str]]:
    if not liq or liq <= 0:
        return 0, ["NO_LIQUIDITY_DATA"]
    if liq < 5000:
        return 15, ["THIN_LIQUIDITY"]
    if not mc or mc <= 0:
        return 55, []
    ratio = liq / mc
    if ratio < 0.03:
        return 25, ["LOW_LIQUIDITY_TO_MC"]
    if ratio < 0.05:
        return 45, ["LOW_LIQUIDITY_TO_MC"]
    if ratio <= 0.30:
        return 100, []
    if ratio <= 0.60:
        return 82, []
    return 60, []


def organic_activity_score(volume: float, txns: int, buys: int, sells: int) -> tuple[float, list[str]]:
    if txns <= 0:
        return 0, ["NO_RECENT_ACTIVITY"]
    avg = volume / txns
    score = 45.0
    flags = []
    if txns >= 25:
        score += 25
    elif txns >= 12:
        score += 15
    elif txns >= 6:
        score += 8
    if 20 <= avg <= 5000:
        score += 20
    elif avg > 25000:
        score -= 25
        flags.append("ABNORMAL_AVG_TRADE_SIZE")
    if buys > 0 and sells > 0 and txns >= 8:
        score += 10
    if volume > 20000 and txns < 5:
        score -= 35
        flags.append("VOLUME_WITHOUT_TRANSACTION_DEPTH")
    return clamp(score), flags


def score_token(
    snapshot: TokenMarketSnapshot, *, age_minutes: float, smart_money_score: float | None = None
) -> TokenScore:
    components = {
        "age": age_score(age_minutes),
        "transaction_acceleration": acceleration_score(
            snapshot.buys_5m + snapshot.sells_5m, snapshot.buys_1h + snapshot.sells_1h
        ),
        "buyer_acceleration": acceleration_score(snapshot.buys_5m, snapshot.buys_1h),
        "buy_sell_balance": imbalance_score(snapshot.buys_5m, snapshot.sells_5m),
    }
    liq, lf = liquidity_health_score(snapshot.market_cap_usd, snapshot.liquidity_usd)
    organic, of = organic_activity_score(
        snapshot.volume_5m_usd, snapshot.buys_5m + snapshot.sells_5m, snapshot.buys_5m, snapshot.sells_5m
    )
    components.update(
        {
            "volume_acceleration": acceleration_score(snapshot.volume_5m_usd, snapshot.volume_1h_usd),
            "liquidity_health": liq,
            "market_cap_momentum": clamp(50 + snapshot.price_change_1h_pct * 2.5),
            "organic_activity": organic,
            "metadata_presence": 80 if snapshot.metadata_present else 35,
        }
    )
    weights = {
        "age": 0.10,
        "transaction_acceleration": 0.13,
        "buyer_acceleration": 0.14,
        "buy_sell_balance": 0.10,
        "volume_acceleration": 0.13,
        "liquidity_health": 0.16,
        "market_cap_momentum": 0.07,
        "organic_activity": 0.12,
        "metadata_presence": 0.05,
    }
    if smart_money_score is not None:
        components["smart_money_activity"] = clamp(smart_money_score)
        weights["smart_money_activity"] = 0.10
        for k in list(weights):
            if k != "smart_money_activity":
                weights[k] *= 0.90
    flags = lf + of
    if snapshot.buys_5m + snapshot.sells_5m < 5:
        flags.append("LOW_ACTIVITY_DEPTH")
    if snapshot.volume_5m_usd / max(snapshot.buys_5m + snapshot.sells_5m, 1) > 50000:
        flags.append("EXTREME_TRADE_SIZE")
    if snapshot.price_change_5m_pct > 150:
        flags.append("VERTICAL_PRICE_MOVE")
    severe = {"NO_LIQUIDITY_DATA", "VOLUME_WITHOUT_TRANSACTION_DEPTH", "EXTREME_TRADE_SIZE"}
    penalty = 18 * sum(f in severe for f in flags) + 8 * sum(
        f in {"THIN_LIQUIDITY", "LOW_LIQUIDITY_TO_MC", "VERTICAL_PRICE_MOVE"} for f in flags
    )
    total = clamp(sum(components[k] * weights[k] for k in weights) - penalty)
    risk = (
        "LOW"
        if total >= 82 and not severe.intersection(flags) and len(flags) <= 1
        else ("MEDIUM" if total >= 65 else "HIGH")
    )
    reasons = tuple(
        k.replace("_", " ").title()
        for k, v in sorted(components.items(), key=lambda x: x[1], reverse=True)
        if v >= 75
    )[:4]
    return TokenScore(
        round(total, 2),
        {k: round(v, 2) for k, v in components.items()},
        risk,
        tuple(dict.fromkeys(flags)),
        reasons,
    )


def cheap_filter(snapshot: TokenMarketSnapshot, *, age_minutes: float, env: Env) -> tuple[bool, str | None]:
    if age_minutes < env.TOKEN_HUNTER_MIN_AGE_MINUTES or age_minutes > env.TOKEN_HUNTER_MAX_AGE_MINUTES:
        return False, "AGE_OUTSIDE_WINDOW"
    if not snapshot.market_cap_usd or snapshot.market_cap_usd < env.TOKEN_HUNTER_MIN_MARKET_CAP_USD:
        return False, "MARKET_CAP_TOO_LOW"
    if snapshot.market_cap_usd > env.TOKEN_HUNTER_MAX_MARKET_CAP_USD:
        return False, "MARKET_CAP_TOO_HIGH"
    if not snapshot.liquidity_usd or snapshot.liquidity_usd < env.TOKEN_HUNTER_MIN_LIQUIDITY_USD:
        return False, "LIQUIDITY_TOO_LOW"
    if snapshot.volume_5m_usd < env.TOKEN_HUNTER_MIN_VOLUME_5M_USD:
        return False, "VOLUME_TOO_LOW"
    if snapshot.buys_5m + snapshot.sells_5m < env.TOKEN_HUNTER_MIN_TXNS_5M:
        return False, "ACTIVITY_TOO_LOW"
    return True, None


def quality_gate(snapshot: TokenMarketSnapshot, *, age_minutes: float, env: Env) -> tuple[bool, str | None]:
    if age_minutes > env.TOKEN_HUNTER_MAX_ENRICHED_AGE_MINUTES:
        return False, "TOO_OLD_FOR_EARLY_OPPORTUNITY"
    if snapshot.buys_5m < env.TOKEN_HUNTER_MIN_BUYS_5M:
        return False, "TOO_FEW_BUYS"
    if snapshot.sells_5m == 0 and snapshot.buys_5m < 10:
        return False, "NO_SELL_SIDE_DEPTH"
    if (
        snapshot.liquidity_usd
        and snapshot.market_cap_usd
        and snapshot.liquidity_usd / snapshot.market_cap_usd < env.TOKEN_HUNTER_MIN_LIQUIDITY_MC_RATIO
    ):
        return False, "UNHEALTHY_LIQUIDITY_MC"
    return True, None


def prefilter_candidates(
    candidates: list[DiscoveryCandidate], *, now: datetime, env: Env
) -> tuple[list[DiscoveryCandidate], dict[str, int]]:
    """Apply only cheap discovery-data gates; never performs provider enrichment."""
    counts = {"basic_filter_passed": 0, "quality_gate_passed": 0}
    prequalified: list[DiscoveryCandidate] = []
    for candidate in candidates:
        snapshot = candidate.snapshot
        age = _age(snapshot.created_at_ms, now)
        if age is None:
            log.info("Token rejected", stage="basic_filter", mint=snapshot.mint, reason="NO_TOKEN_AGE")
            continue
        ok, reason = cheap_filter(snapshot, age_minutes=age, env=env)
        if not ok:
            log.info("Token rejected", stage="basic_filter", mint=snapshot.mint, reason=reason)
            continue
        counts["basic_filter_passed"] += 1
        ok, reason = quality_gate(snapshot, age_minutes=age, env=env)
        if not ok:
            log.info("Token rejected", stage="quality_gate", mint=snapshot.mint, reason=reason)
            continue
        counts["quality_gate_passed"] += 1
        prequalified.append(candidate)
    return prequalified, counts


def _age(created: int | None, now: datetime) -> float | None:
    if not created:
        return None
    return max(0, (now - datetime.fromtimestamp(created / 1000, tz=UTC)).total_seconds() / 60)


def _money(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1000:
        return f"${v / 1000:.1f}K"
    return f"${v:,.0f}"


def format_alert(s: TokenMarketSnapshot, score: TokenScore, age: float, detected: datetime) -> str:
    risk_icon = "🟢" if score.risk_level == "LOW" else "🟡" if score.risk_level == "MEDIUM" else "🔴"
    symbol = escape(s.symbol or "Unknown")
    name = escape(s.name or "Unknown")
    mint = escape(s.mint)
    reasons = score.reasons or ("Multiple independent activity signals",)
    warnings = score.risk_flags or ("None observed",)
    reason_lines = "\n".join(f"• {escape(reason.replace('_', ' ').title())}" for reason in reasons[:4])
    warning_lines = "\n".join(f"• {escape(flag.replace('_', ' ').title())}" for flag in warnings[:4])
    return (
        "🚨 <b>WHALE ALPHA • TOKEN HUNTER</b>\n"
        "<i>High-potential early opportunity detected</i>\n\n"
        f"🪙 <b>${symbol}</b>  <i>{name}</i>\n"
        f"🎯 <b>Score:</b> {score.total:.0f}/100  |  {risk_icon} <b>{escape(score.risk_level)} RISK</b>\n"
        f"⏱ <b>Age:</b> {age:.0f}m\n\n"
        "📊 <b>MARKET SNAPSHOT</b>\n"
        f"• Market cap: <b>{escape(_money(s.market_cap_usd))}</b>\n"
        f"• Liquidity: <b>{escape(_money(s.liquidity_usd))}</b>\n"
        f"• 5m volume: <b>{escape(_money(s.volume_5m_usd))}</b>\n"
        f"• Flow: <b>{s.buys_5m}</b> buys / <b>{s.sells_5m}</b> sells\n"
        f"• 1h price: <b>{s.price_change_1h_pct:+.1f}%</b>\n\n"
        "🧠 <b>WHY IT TRIGGERED</b>\n"
        f"{reason_lines}\n\n"
        "🛡 <b>RISK CHECK</b>\n"
        f"{warning_lines}\n\n"
        "🔐 <b>CONTRACT</b>\n"
        f"<code>{mint}</code>\n\n"
        f"🕒 Detected: <code>{detected.strftime('%Y-%m-%d %H:%M:%S')} UTC</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Whale Alpha • Signal Intelligence</i>"
    )


async def _smart_money(session: AsyncSession, mint: str, now: datetime) -> float | None:
    since = now - timedelta(minutes=30)
    result = await session.execute(
        select(WalletEvent.wallet_id)
        .join(WhaleWallet, WhaleWallet.id == WalletEvent.wallet_id)
        .where(
            WalletEvent.token_mint == mint,
            WalletEvent.observed_at >= since,
            WhaleWallet.status == WalletStatus.APPROVED,
            WalletEvent.side == "BUY",
        )
    )
    wallets = len(set(result.scalars().all()))
    return None if wallets == 0 else clamp(45 + wallets * 18)


async def _persist(
    session: AsyncSession, s: TokenMarketSnapshot, score: TokenScore, source: str, now: datetime, age: float
) -> TokenOpportunity:
    result = await session.execute(select(TokenOpportunity).where(TokenOpportunity.mint == s.mint))
    o = result.scalar_one_or_none()
    if o is None:
        o = TokenOpportunity(mint=s.mint, detected_at=now)
        session.add(o)
    o.name = s.name
    o.symbol = s.symbol
    o.detection_source = source
    o.last_seen_at = now
    o.age_minutes = age
    o.market_cap_usd = s.market_cap_usd
    o.liquidity_usd = s.liquidity_usd
    o.price_usd = s.price_usd
    o.score = score.total
    o.score_breakdown = score.components
    o.risk_level = score.risk_level
    o.risk_flags = list(score.risk_flags)
    o.key_reasons = list(score.reasons)
    o.status = "HIGH_POTENTIAL" if score.total >= 82 and score.risk_level != "HIGH" else "SCORED"
    o.volume_5m_usd = s.volume_5m_usd
    o.volume_1h_usd = s.volume_1h_usd
    o.buys_5m = s.buys_5m
    o.sells_5m = s.sells_5m
    o.buys_1h = s.buys_1h
    o.sells_1h = s.sells_1h
    session.add(
        TokenSnapshot(
            opportunity=o,
            observed_at=now,
            market_cap_usd=s.market_cap_usd,
            liquidity_usd=s.liquidity_usd,
            price_usd=s.price_usd,
            volume_5m_usd=s.volume_5m_usd,
        )
    )
    await session.flush()
    return o


async def _outcomes(session: AsyncSession, client: Any, env: Env, now: datetime) -> None:
    result = await session.execute(
        select(TokenOpportunity).where(TokenOpportunity.detected_at >= now - timedelta(hours=2))
    )
    opportunities = list(result.scalars())
    for offset in range(0, len(opportunities), 30):
        batch = opportunities[offset : offset + 30]
        snapshots = await enrich_tokens(client, env, [o.mint for o in batch])
        for o in batch:
            s = snapshots.get(o.mint)
            if s is None or s.market_cap_usd is None:
                continue
            elapsed = (now - o.detected_at).total_seconds() / 60
            if elapsed >= 5 and o.mc_after_5m is None:
                o.mc_after_5m = s.market_cap_usd
            if elapsed >= 15 and o.mc_after_15m is None:
                o.mc_after_15m = s.market_cap_usd
            if elapsed >= 30 and o.mc_after_30m is None:
                o.mc_after_30m = s.market_cap_usd
            if elapsed >= 60 and o.mc_after_1h is None:
                o.mc_after_1h = s.market_cap_usd
            o.max_mc_usd = max(o.max_mc_usd or 0, s.market_cap_usd)
            o.min_mc_usd = min(o.min_mc_usd or s.market_cap_usd, s.market_cap_usd)
            if o.market_cap_usd:
                o.max_return_pct = max(o.max_return_pct or 0, (o.max_mc_usd / o.market_cap_usd - 1) * 100)
                o.max_drawdown_pct = min(o.max_drawdown_pct or 0, (o.min_mc_usd / o.market_cap_usd - 1) * 100)

            if o.alert_reference_price_usd and s.price_usd and s.price_usd > 0 and o.alert_message_ids:
                gain_pct = (s.price_usd / o.alert_reference_price_usd - 1) * 100
                sent_milestones = set(o.quote_milestones or [])
                crossed = [m for m in quote_milestones_for_gain(gain_pct) if m not in sent_milestones]
                for milestone in crossed:
                    quote_text = format_quote_alert(o, gain_pct, milestone, s.price_usd)
                    delivered_quote = 0
                    for chat_id, message_id in list((o.alert_message_ids or {}).items()):
                        try:
                            await bot.send_message(
                                chat_id=int(chat_id),
                                text=quote_text,
                                parse_mode="HTML",
                                reply_parameters=ReplyParameters(message_id=int(message_id)),
                            )
                            delivered_quote += 1
                        except (TelegramAPIError, ValueError) as err:
                            log.warning(
                                "quote_alert_failed",
                                mint=o.mint,
                                milestone=milestone,
                                chat_id=chat_id,
                                error=str(err),
                            )
                    if delivered_quote:
                        sent_milestones.add(milestone)
                        o.quote_milestones = sorted(sent_milestones)
                        log.info(
                            "quote_alert_delivered",
                            mint=o.mint,
                            milestone=milestone,
                            gain_pct=round(gain_pct, 2),
                            delivered=delivered_quote,
                        )


async def run_hunter_cycle(
    env: Env,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    client: Any,
    connection: Any | None = None,
) -> dict[str, int]:
    now = datetime.now(UTC)
    funnel = {
        k: 0
        for k in (
            "discovered",
            "basic_filter_passed",
            "quality_gate_passed",
            "enriched",
            "scored",
            "high_potential",
            "alert_attempted",
            "alert_delivered",
        )
    }
    sources = await discover_token_candidates(client, env)
    candidates: dict[str, DiscoveryCandidate] = {}
    for _, values in sources.items():
        for candidate in values:
            candidates.setdefault(candidate.snapshot.mint, candidate)
    funnel["discovered"] = len(candidates)

    async with session_factory() as session:
        limited = list(candidates.values())[: env.TOKEN_HUNTER_MAX_UNIQUE_PER_CYCLE]
        age_resolutions = await resolve_token_ages(client, env, limited, now, connection)
        resolved_candidates = [
            replace(
                candidate,
                snapshot=replace(
                    candidate.snapshot,
                    created_at_ms=age_resolutions[candidate.snapshot.mint].created_at_ms,
                ),
            )
            for candidate in limited
            if candidate.snapshot.mint in age_resolutions
        ]
        prequalified, prefilter_counts = prefilter_candidates(resolved_candidates, now=now, env=env)
        funnel["basic_filter_passed"] = prefilter_counts["basic_filter_passed"]
        funnel["quality_gate_passed"] = prefilter_counts["quality_gate_passed"]
        for offset in range(0, len(prequalified), 30):
            batch = prequalified[offset : offset + 30]
            snapshots = await enrich_tokens(client, env, [c.snapshot.mint for c in batch])
            funnel["enriched"] += len(snapshots)
            for candidate in batch:
                mint = candidate.snapshot.mint
                s = snapshots.get(mint)
                if s is None:
                    continue
                resolution = age_resolutions.get(mint)
                created_at_ms = resolution.created_at_ms if resolution else s.created_at_ms
                age = _age(created_at_ms, now)
                if age is None:
                    log.info("Token rejected", stage="enriched", mint=mint, reason="NO_TOKEN_AGE")
                    continue
                s = replace(s, created_at_ms=created_at_ms)
                score = score_token(
                    s, age_minutes=age, smart_money_score=await _smart_money(session, mint, now)
                )
                funnel["scored"] += 1
                o = await _persist(session, s, score, candidate.source, now, age)
                if score.total < env.TOKEN_HUNTER_ALERT_MIN_SCORE or score.risk_level == "HIGH":
                    continue
                funnel["high_potential"] += 1
                if o.last_alerted_at is not None and now - o.last_alerted_at < timedelta(
                    minutes=env.TOKEN_HUNTER_ALERT_COOLDOWN_MINUTES
                ):
                    continue
                o.alert_attempted_at = now
                o.alert_status = "ATTEMPTED"
                funnel["alert_attempted"] += 1
                delivered = 0
                errors: list[str] = []
                message_ids: dict[str, int] = {}
                text = format_alert(s, score, age, o.detected_at)
                recipient_ids = alert_recipient_ids(env.admin_telegram_ids, [])
                for chat_id in recipient_ids:
                    try:
                        message = await bot.send_message(chat_id=int(chat_id), text=text, parse_mode="HTML")
                        message_ids[str(chat_id)] = message.message_id
                        delivered += 1
                    except (TelegramAPIError, ValueError) as err:
                        errors.append(str(err))
                if delivered == 0:
                    subscriber_result = await session.execute(
                        select(User.telegram_id).where(User.notify_signals.is_(True))
                    )
                    subscriber_ids = alert_recipient_ids(set(), list(subscriber_result.scalars().all()))
                    for chat_id in subscriber_ids:
                        try:
                            message = await bot.send_message(chat_id=int(chat_id), text=text, parse_mode="HTML")
                            message_ids[str(chat_id)] = message.message_id
                            delivered += 1
                        except (TelegramAPIError, ValueError) as err:
                            errors.append(str(err))
                if delivered:
                    o.alert_delivered_at = now
                    o.last_alerted_at = now
                    o.alert_status = "DELIVERED"
                    o.alert_error = "; ".join(errors)[:1000] if errors else None
                    if o.alert_reference_price_usd is None and s.price_usd is not None and s.price_usd > 0:
                        o.alert_reference_price_usd = s.price_usd
                    o.alert_message_ids = message_ids
                    o.quote_milestones = list(o.quote_milestones or [])
                    funnel["alert_delivered"] += delivered
                    log.info("alert_delivered", mint=mint, score=score.total, delivered=delivered)
                else:
                    o.alert_status = "FAILED"
                    o.alert_error = (
                        "; ".join(errors)[:1000] or "No configured admin chat accepted the message"
                    )
                    log.error("alert_failed", mint=mint, score=score.total, error=o.alert_error)
        await _outcomes(session, client, env, now)
        await session.commit()
    log.info("TOKEN HUNTER CYCLE COMPLETE", **funnel)
    return funnel


def start_token_hunter_loop(
    env: Env, session_factory: async_sessionmaker[AsyncSession], bot: Bot, client: Any, connection: Any | None = None
) -> Callable[[], Any]:
    async def worker() -> None:
        await asyncio.sleep(env.TOKEN_HUNTER_STARTUP_DELAY_SECONDS)
        while True:
            try:
                await run_hunter_cycle(env, session_factory, bot, client, connection)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.exception("Token hunter cycle failed", err=str(err))
            await asyncio.sleep(env.TOKEN_HUNTER_INTERVAL_SECONDS)

    task = asyncio.create_task(worker(), name="token-hunter")

    async def stop() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return stop
