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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ReplyParameters
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from whale_alpha.config import Env
from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot, enrich_tokens
from whale_alpha.services.token_scanner import build_expert_alert_card
from whale_alpha.engines.market_regime import MarketRegime, classify_market_regime, market_regime_gate
from whale_alpha.integrations.token_age import resolve_token_ages
from whale_alpha.utils.logger import child_logger

from whale_alpha.db.models import TokenOpportunity, TokenSnapshot, User, WalletEvent, WalletStatus, WhaleWallet
from whale_alpha.integrations.token_hunter_sources import DiscoveryCandidate, discover_token_candidates
from whale_alpha.engines.reversal_hunter import ReversalAnalysis, evaluate_candidates, discover_meme_candidates

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
    symbol = escape(o.symbol or o.name or o.mint[:8])
    mint = escape(o.mint)
    reference = o.alert_reference_price_usd or 0
    return (
        "ð <b>WHALE ALPHA â¢ PERFORMANCE UPDATE</b>\n"
        f"<i>{symbol} crossed the {escape(milestone_label)} milestone</i>\n\n"
        f"ðª <b>${escape(o.symbol or 'TOKEN')}</b>\n"
        f"ð <b>Return:</b> +{gain_pct:.1f}%  <b>({multiple:.2f}x)</b>\n"
        f"ð¯ <b>Milestone:</b> {escape(milestone_label)}\n"
        f"ðµ <b>Live price:</b> <code>${price_usd:.10f}</code>\n"
        f"ð <b>Signal price:</b> <code>${reference:.10f}</code>\n\n"
        "ð§­ <b>STATUS</b>\n"
        "â¢ Milestone crossed from the original signal baseline\n"
        "â¢ Update is a market-performance observation, not a trade instruction\n\n"
        f"ð <b>Contract:</b> <code>{mint}</code>\n"
        "ââââââââââââââââââââ\n"
        "<i>Whale Alpha â¢ Quote Intelligence</i>"
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
    snapshot: TokenMarketSnapshot, *, age_minutes: float, smart_money_score: float | None = None, market_regime: MarketRegime | None = None
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
            "market_trend_alignment": clamp(50 + snapshot.price_change_1h_pct * 4 + (snapshot.buys_5m / max(snapshot.buys_5m + snapshot.sells_5m, 1) - 0.5) * 100),
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
    if market_regime is not None:
        components["market_trend_alignment"] = clamp(50 + snapshot.price_change_1h_pct * 4 + (snapshot.buys_5m / max(snapshot.buys_5m + snapshot.sells_5m, 1) - 0.5) * 100)
        weights["market_trend_alignment"] = 0.10
        for k in list(weights):
            if k != "market_trend_alignment":
                weights[k] *= 0.90
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
    if market_regime is not None:
        regime_bias={"RISK_ON":6,"BULLISH":4,"NEUTRAL":0,"RISK_OFF":-5,"PANIC":-12,"UNKNOWN":-2}.get(market_regime.name,0)
        total=clamp(sum(components[k]*weights[k] for k in weights)+regime_bias-penalty)
    else:
        total=clamp(sum(components[k]*weights[k] for k in weights)-penalty)
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
    return build_expert_alert_card(
        s,
        score=score.total,
        risk_level=score.risk_level,
        risk_flags=score.risk_flags,
        age_minutes=age,
        detected_at=detected,
    )


def build_alert_keyboard(s: TokenMarketSnapshot) -> InlineKeyboardMarkup:
    mint = s.mint
    pair = s.pair_address
    buttons = []
    if mint:
        buttons.append(InlineKeyboardButton(text="⚡ Buy X", url=f"https://jup.ag/swap/SOL-{mint}"))
    if pair:
        buttons.append(InlineKeyboardButton(text="👁 Track", url=f"https://dexscreener.com/solana/{pair}"))
    rows = [buttons] if buttons else []
    if pair:
        rows.append([InlineKeyboardButton(text="📊 Chart", url=f"https://dexscreener.com/solana/{pair}")])
    rows.append([InlineKeyboardButton(text="🔎 Scan", url=f"https://solscan.io/token/{mint}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


async def _outcomes(session: AsyncSession, client: Any, env: Env, now: datetime, bot: Bot) -> None:
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


def format_reversal_alert(a: ReversalAnalysis) -> str:
    p = a.pattern
    f = a.flow
    o = a.onchain
    s = a.snapshot
    age_hours = ((datetime.now(UTC).timestamp() * 1000 - (s.created_at_ms or 0)) / 3_600_000) if s.created_at_ms else 0
    age = f"{age_hours:.1f}h" if age_hours < 48 else f"{age_hours/24:.1f}d"
    return (
        "🚨 <b>WHALE ALPHA SIGNAL</b>\n"
        f"Token: {escape(s.name or 'Unknown')}\n"
        f"Ticker: {escape(s.symbol or 'UNKNOWN')}\n"
        f"Contract Address: <code>{escape(s.mint)}</code>\n"
        f"DEX Pair: <code>{escape(s.pair_address or 'UNKNOWN')}</code>\n"
        f"Age: {age}\n"
        f"Price: ${s.price_usd:.10f}\n"
        f"Market Cap: ${s.market_cap_usd:,.0f}\n"
        f"Liquidity: ${s.liquidity_usd:,.0f}\n"
        f"Liquidity/MC Ratio: {(s.liquidity_usd / s.market_cap_usd * 100):.2f}%\n\n"
        "Pattern:\n"
        f"- Dip %: {p.dip_pct:.2f}%\n"
        f"- Dip Lookback: {p.dip_lookback_hours:.2f}h\n"
        f"- Consolidation Duration: {p.consolidation_minutes:.0f}m\n"
        f"- Consolidation Range %: {p.consolidation_range_pct:.2f}%\n"
        f"- Breakout Status: {'CONFIRMED' if p.breakout_confirmed else 'FAILED'}\n\n"
        "Flow:\n"
        f"- 5m Volume vs Avg: {f.volume_5m_vs_avg:.2f}x\n"
        f"- 15m Volume vs Avg: {f.volume_15m_vs_avg:.2f}x\n"
        f"- Buy/Sell Ratio: {f.buy_sell_ratio:.2f}\n"
        f"- Net Buy Pressure: {'YES' if f.net_buy_pressure else 'NO'}\n"
        f"- Smart Money Status: {escape(f.smart_money_status)}\n"
        f"- Top Trader Status: {escape(f.top_trader_status)}\n\n"
        "On-Chain Risk:\n"
        f"- Top 10 Holder %: {o.top10_pct:.2f}%\n"
        f"- Largest Wallet %: {o.largest_wallet_pct:.2f}%\n"
        f"- Dev Hold %: {o.dev_hold_pct:.2f}%\n"
        f"- Tagged Risk Wallets Combined %: {o.tagged_risk_pct:.2f}%\n"
        f"- Security Flags: {escape(', '.join(o.security_flags) or 'NONE')}\n"
        f"- Authority Flags: {escape(', '.join(o.authority_flags) or 'NONE')}\n\n"
        f"Score: {a.score:.2f}/100\n"
        f"Confidence Tier: {escape(a.tier)}\n\n"
        "Why This Alert Triggered:\n"
        + ''.join(f"- {escape(r)}\n" for r in a.reasons)
        + "\nInvalidation:\n"
        f"- {escape(a.invalidation)}\n\n"
        f"Final Verdict:\n- {escape(a.tier)}"
    )


async def _persist_reversal(session: AsyncSession, analysis: ReversalAnalysis, now: datetime) -> TokenOpportunity:
    s = analysis.snapshot
    result = await session.execute(select(TokenOpportunity).where(TokenOpportunity.mint == s.mint))
    o = result.scalar_one_or_none()
    if o is None:
        o = TokenOpportunity(mint=s.mint, detected_at=now)
        session.add(o)
    age = ((now.timestamp() * 1000 - (s.created_at_ms or now.timestamp() * 1000)) / 60_000)
    o.name = s.name
    o.symbol = s.symbol
    o.detection_source = analysis.candidate.source
    o.last_seen_at = now
    o.age_minutes = max(0.0, age)
    o.market_cap_usd = s.market_cap_usd
    o.liquidity_usd = s.liquidity_usd
    o.price_usd = s.price_usd
    o.volume_5m_usd = s.volume_5m_usd
    o.volume_1h_usd = s.volume_1h_usd
    o.buys_5m = s.buys_5m
    o.sells_5m = s.sells_5m
    o.buys_1h = s.buys_1h
    o.sells_1h = s.sells_1h
    o.score = analysis.score
    o.score_breakdown = analysis.evidence
    o.risk_level = "LOW" if analysis.approved else "HIGH"
    o.risk_flags = list(analysis.hard_rejects)
    o.key_reasons = list(analysis.reasons)
    o.status = "HIGH_POTENTIAL" if analysis.approved else "REJECTED"
    session.add(TokenSnapshot(opportunity=o, observed_at=now, market_cap_usd=s.market_cap_usd, liquidity_usd=s.liquidity_usd, price_usd=s.price_usd, volume_5m_usd=s.volume_5m_usd))
    await session.flush()
    return o


async def run_hunter_cycle(
    env: Env,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    client: Any,
    connection: Any | None = None,
) -> dict[str, int]:
    """Run only the strict Whale Alpha dip -> consolidation -> reversal strategy."""
    now = datetime.now(UTC)
    funnel = {"discovered": 0, "evaluated": 0, "approved": 0, "alert_attempted": 0, "alert_delivered": 0}
    candidates = await discover_meme_candidates(client, env, now)
    funnel["discovered"] = len(candidates)
    analyses = await evaluate_candidates(client, env, candidates, connection, now)
    funnel["evaluated"] = len(analyses)
    async with session_factory() as session:
        for analysis in analyses:
            o = await _persist_reversal(session, analysis, now)
            if not analysis.approved:
                continue
            funnel["approved"] += 1
            if o.last_alerted_at is not None and now - o.last_alerted_at < timedelta(minutes=env.TOKEN_HUNTER_ALERT_COOLDOWN_MINUTES):
                continue
            text = format_reversal_alert(analysis)
            recipients = alert_recipient_ids(env.admin_telegram_ids, [])
            if not recipients:
                result = await session.execute(select(User.telegram_id).where(User.notify_signals.is_(True)))
                recipients = alert_recipient_ids(set(), list(result.scalars().all()))
            delivered = 0
            message_ids: dict[str, int] = {}
            errors: list[str] = []
            for chat_id in recipients:
                try:
                    msg = await bot.send_message(chat_id=int(chat_id), text=text, parse_mode="HTML", reply_markup=build_alert_keyboard(analysis.snapshot))
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
                o.alert_reference_price_usd = analysis.snapshot.price_usd
                o.alert_message_ids = message_ids
                funnel["alert_delivered"] += delivered
            funnel["alert_attempted"] += 1
        await session.commit()
    log.info("WHALE ALPHA REVERSAL CYCLE COMPLETE", **funnel)
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
