"""
Premium Signal Engine.

Two responsibilities, run as one cohesive pipeline:

  1. MONITOR — periodically re-checks a rotating batch of active/watch
     Premium wallets for new on-chain activity (Helius), turning each
     detected transfer into a models/premium_wallet_trade.py row. This
     is what feeds services/premium_wallet_scorer.py's reputation math
     AND what consensus detection below reads from.

  2. CONSENSUS + AI GATE — looks for tokens that multiple distinct
     active Premium wallets bought within a short rolling window
     (Smart Wallet consensus), then — only for tokens that clear that
     bar — runs the existing AlphaPulse AI conviction-scoring pipeline
     (services/conviction_scorer.py, the same engine the free Signal
     Engine uses) on that token. A Premium Signal is only ever created
     when BOTH gates pass. Both thresholds are configurable in
     config/settings.py.

This module deliberately does not touch services/signal_tracker.py or
services/pump_radar.py — it reuses their building-block services
(dexscreener, goplus, holders, conviction_scorer) but writes to its own
models/premium_signal.py table and its own broadcast list, so the free
Signal Engine keeps behaving exactly as it did before.
"""

import asyncio
import html
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, and_, func as sa_func

from infra.db.session import async_session
from config.settings import (
    PREMIUM_MONITOR_BATCH_SIZE,
    PREMIUM_CONSENSUS_MIN_WALLETS,
    PREMIUM_CONSENSUS_WINDOW_MINUTES,
    PREMIUM_CONSENSUS_MIN_AI_SCORE,
    PREMIUM_CONSENSUS_MIN_AVG_REPUTATION,
    PREMIUM_WALLET_INITIAL_TARGET,
    PREMIUM_WALLET_LONGTERM_TARGET,
    PREMIUM_WALLET_HARD_CAP,
)
from models.premium_wallet import PremiumWallet
from models.premium_wallet_trade import PremiumWalletTrade
from models.premium_signal import PremiumSignal
from models.premium_membership import PremiumMembership

from providers.rpc.helius import get_wallet_transactions
from providers.marketdata.dexscreener import get_token_card_info
from providers.marketdata.goplus import check_token_security
from domain.intelligence.holders import get_holder_analysis
from domain.signals.scoring import hard_reject_reasons, score_candidate
from domain.payments.premium_service import format_premium_header, format_premium_badge

logger = logging.getLogger("AlphaPulse.PremiumSignalEngine")

WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
NATIVE_SOL_TOKEN = "SOL"


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else "N/A"


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return default


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _short(addr: str, size: int = 5) -> str:
    if not addr or len(addr) <= size * 2:
        return addr or "Unknown"
    return f"{addr[:size]}...{addr[-size:]}"


# ---------------------------------------------------------------------
# 1. MONITOR — detect new wallet activity, record trades
# ---------------------------------------------------------------------

def _select_monitor_batch(wallets: list[PremiumWallet], batch_size: int) -> list[PremiumWallet]:
    """
    Round-robin batch selection weighted toward elite/core wallets (they
    get checked more often) while still eventually cycling through
    everything, ordered by staleness (least-recently-checked first).
    """
    def _priority(w: PremiumWallet) -> tuple:
        tier_rank = {"elite": 0, "core": 1, "watch": 2, "candidate": 3}.get(w.tier, 3)
        last_checked = w.last_checked_at or datetime.min
        return (tier_rank, last_checked)

    ordered = sorted(wallets, key=_priority)
    return ordered[:batch_size]


def _extract_risk_flags(security: dict) -> list[str]:
    """
    Maps services/goplus.py's normalized security dict onto a short list
    of danger flags relevant to wallet-exposure tracking. Deliberately a
    narrower set than the full free-Signal-Engine reject gate (that one
    also weighs LP-lock %, funding clusters, etc. which aren't available
    here) — this is specifically "did this wallet buy into something
    GoPlus flags as a honeypot/scam", not a full token risk assessment.
    """
    def _on(key: str) -> bool:
        return str(security.get(key, "0")) == "1"

    flags = []
    if _on("is_honeypot"):
        flags.append("honeypot")
    if _on("is_blacklisted"):
        flags.append("blacklisted")
    if _on("cannot_sell_all"):
        flags.append("cannot_sell_all")
    if _on("cannot_buy"):
        flags.append("cannot_buy")
    if _on("hidden_owner"):
        flags.append("hidden_owner")
    if _on("selfdestruct"):
        flags.append("selfdestruct")
    return flags


async def _record_trade(
    session, wallet: PremiumWallet, event: dict, market_cache: dict, security_cache: dict
) -> bool:
    """Inserts one PremiumWalletTrade row for a Helius activity event. Returns True if a trade row was added."""
    token = event.get("token") or ""
    direction = event.get("type")  # "IN" | "OUT" | "TX"
    signature = event.get("signature")

    if direction not in ("IN", "OUT") or token in ("", NATIVE_SOL_TOKEN, "SOLANA_TX"):
        return False
    if not signature:
        return False

    existing = await session.execute(
        select(PremiumWalletTrade.id).where(PremiumWalletTrade.signature == signature)
    )
    if existing.scalar_one_or_none():
        return False

    market = market_cache.get(token)
    if market is None:
        try:
            info = await asyncio.wait_for(get_token_card_info(token), timeout=6)
        except Exception:
            info = None
        market = {
            "price": _to_float(info.get("price")) if info else 0.0,
            # Same DexScreener call already made for price — capturing
            # liquidity here is free, no extra API usage.
            "liquidity": _to_float(info.get("liquidity")) if info and info.get("liquidity") not in (None, "N/A") else None,
        }
        market_cache[token] = market

    price = market.get("price") or 0.0
    liquidity = market.get("liquidity")

    amount = _to_float(event.get("amount"))
    value_usd = amount * price if price else None

    # Rug pull / honeypot / scam-token exposure — only meaningful for
    # what the wallet bought INTO, and only checked once per token per
    # cycle no matter how many wallets traded it (cached), keeping this
    # inside AlphaPulse's existing API rate-limit budget.
    is_flagged_risky = None
    risk_flags_str = None
    if direction == "IN":
        cached = security_cache.get(token, "__unset__")
        if cached == "__unset__":
            try:
                sec = await asyncio.wait_for(check_token_security(token), timeout=8)
            except Exception:
                sec = None
            cached = _extract_risk_flags(sec) if sec else None
            security_cache[token] = cached
        if cached is not None:
            is_flagged_risky = "1" if cached else "0"
            risk_flags_str = ",".join(cached) if cached else None

    trade = PremiumWalletTrade(
        wallet_id=wallet.id,
        wallet_address=wallet.wallet_address,
        token_mint=token,
        token_symbol=None,
        side="buy" if direction == "IN" else "sell",
        amount=amount,
        price_usd_at_detection=price or None,
        value_usd_at_detection=value_usd,
        entry_liquidity_usd=liquidity if direction == "IN" else None,
        is_flagged_risky=is_flagged_risky,
        risk_flags=risk_flags_str,
        signature=signature,
    )
    session.add(trade)
    return True


async def run_monitor_cycle() -> dict:
    stats = {"wallets_checked": 0, "trades_recorded": 0}
    market_cache: dict = {}
    security_cache: dict = {}

    async with async_session() as session:
        res = await session.execute(
            select(PremiumWallet).where(PremiumWallet.status.in_(["active", "watch"]))
        )
        all_wallets = res.scalars().all()

    batch = _select_monitor_batch(all_wallets, PREMIUM_MONITOR_BATCH_SIZE)
    if not batch:
        return stats

    async with async_session() as session:
        for wallet in batch:
            wallet = await session.get(PremiumWallet, wallet.id)
            if not wallet:
                continue

            try:
                events = await asyncio.wait_for(
                    get_wallet_transactions(wallet.wallet_address, limit=8), timeout=12
                )
            except Exception as e:
                logger.debug(f"Monitor fetch failed for {wallet.wallet_address}: {e}")
                events = []

            wallet.last_checked_at = _now()
            stats["wallets_checked"] += 1

            recorded_here = 0
            for ev in events or []:
                added = await _record_trade(session, wallet, ev, market_cache, security_cache)
                if added:
                    recorded_here += 1

            if recorded_here:
                stats["trades_recorded"] += recorded_here
                wallet.last_activity_at = _now()
                wallet.consecutive_empty_checks = 0
            else:
                wallet.consecutive_empty_checks = (wallet.consecutive_empty_checks or 0) + 1

            await asyncio.sleep(0.2)

        await session.commit()

    logger.info(
        f"👁️ Premium monitor cycle: {stats['wallets_checked']} wallet(s) checked, "
        f"{stats['trades_recorded']} new trade(s) recorded"
    )
    return stats


# ---------------------------------------------------------------------
# 2. CONSENSUS DETECTION
# ---------------------------------------------------------------------

async def _active_wallet_count(session) -> int:
    res = await session.execute(
        select(sa_func.count()).select_from(PremiumWallet).where(PremiumWallet.status == "active")
    )
    return res.scalar() or 0


def _capacity_confidence_multiplier(active_count: int) -> float:
    """
    Operational Threshold model:
      - below Minimum Operational Threshold -> the engine doesn't
        generate Premium Intelligence at all (see run_consensus_cycle,
        which gates on this before ever reaching here).
      - Minimum -> Recommended Capacity: confidence ramps 0.85 -> 1.0
        as the database fills in ("Recommended Capacity -> Improved
        Premium confidence").
      - Recommended -> Optimal Capacity: confidence ramps 1.0 -> 1.05
        ("Optimal Capacity -> Highest-quality Premium consensus").
      - at/above Optimal Capacity: capped at 1.05.
    """
    if active_count >= PREMIUM_WALLET_HARD_CAP:
        return 1.05
    if active_count >= PREMIUM_WALLET_LONGTERM_TARGET:
        span = PREMIUM_WALLET_HARD_CAP - PREMIUM_WALLET_LONGTERM_TARGET
        progress = (active_count - PREMIUM_WALLET_LONGTERM_TARGET) / span if span > 0 else 1.0
        return 1.0 + 0.05 * progress
    span = PREMIUM_WALLET_LONGTERM_TARGET - PREMIUM_WALLET_INITIAL_TARGET
    progress = (active_count - PREMIUM_WALLET_INITIAL_TARGET) / span if span > 0 else 1.0
    return 0.85 + 0.15 * max(0.0, min(1.0, progress))


async def _find_consensus_candidates(session) -> list[dict]:
    """
    Finds tokens with >= PREMIUM_CONSENSUS_MIN_WALLETS distinct active
    Premium wallets buying within the rolling consensus window, that
    don't already have a recent active Premium Signal.
    """
    window_start = _now() - timedelta(minutes=PREMIUM_CONSENSUS_WINDOW_MINUTES)

    res = await session.execute(
        select(PremiumWalletTrade, PremiumWallet)
        .join(PremiumWallet, PremiumWallet.id == PremiumWalletTrade.wallet_id)
        .where(
            and_(
                PremiumWalletTrade.side == "buy",
                PremiumWalletTrade.detected_at >= window_start,
                PremiumWallet.status == "active",
            )
        )
    )
    rows = res.all()

    by_token: dict[str, list[PremiumWallet]] = defaultdict(list)
    for trade, wallet in rows:
        by_token[trade.token_mint].append(wallet)

    candidates = []
    for mint, wallets in by_token.items():
        distinct = {w.wallet_address: w for w in wallets}
        if len(distinct) < PREMIUM_CONSENSUS_MIN_WALLETS:
            continue

        avg_rep = sum((w.reputation_score or 0) for w in distinct.values()) / len(distinct)
        if avg_rep < PREMIUM_CONSENSUS_MIN_AVG_REPUTATION:
            continue

        already = await session.execute(
            select(PremiumSignal.id).where(
                and_(
                    PremiumSignal.token_mint == mint,
                    PremiumSignal.status == "active",
                    PremiumSignal.signaled_at >= window_start,
                )
            )
        )
        if already.scalar_one_or_none():
            continue

        candidates.append(
            {
                "mint": mint,
                "wallets": list(distinct.values()),
                "avg_reputation": round(avg_rep, 1),
            }
        )

    return candidates


# ---------------------------------------------------------------------
# 3. AI ANALYSIS GATE (reuses the existing conviction-scoring engine)
# ---------------------------------------------------------------------

async def _run_ai_analysis(mint: str) -> dict | None:
    data = await get_token_card_info(mint)
    if not data:
        return None

    sec = await check_token_security(mint)
    dev_address = (sec or {}).get("creator_address")
    holder_analysis = await get_holder_analysis(mint, dev_address=dev_address)
    holders = holder_analysis.get("total_holders") if holder_analysis else None

    reject_reasons = hard_reject_reasons(data, sec, holder_analysis, mint)
    if reject_reasons:
        return {"eligible": False, "reasons": reject_reasons, "data": data}

    result = score_candidate(data, sec, holder_analysis, holders, mint)
    result["data"] = data
    return result


# ---------------------------------------------------------------------
# 4. SIGNAL CREATION + BROADCAST
# ---------------------------------------------------------------------

def _build_premium_signal_card(signal: PremiumSignal, wallets: list[PremiumWallet]) -> str:
    # Internal Security requirement: the Smart Wallet database (addresses,
    # rankings, reputation scores, tiers, classifications) is confidential
    # and must never be visible to Free or Premium users — only the final
    # signal and analysis. `wallets` is accepted for signature/logging
    # compatibility but deliberately not rendered here beyond a bare count.
    return (
        f"{format_premium_header()}\n\n"
        f"🪙 <b>{_esc(signal.token_name or 'Unknown')} ({_esc(signal.token_symbol or '?')})</b>\n"
        f"<code>{_esc(signal.token_mint)}</code>\n\n"
        f"🧠 AI Conviction Score: <b>{signal.ai_score:.0f}/100</b> ({_esc(signal.ai_tier)})\n"
        f"🤝 Smart Wallet Consensus: <b>{signal.consensus_wallet_count} independent smart wallets</b>\n"
        f"⭐ Combined Confidence: <b>{signal.confidence_score:.0f}/100</b>\n\n"
        f"💧 Liquidity: <b>${_to_float(signal.entry_liquidity):,.0f}</b>\n"
        f"📊 Market Cap: <b>${_to_float(signal.entry_market_cap):,.0f}</b>\n\n"
        f"🔗 <a href=\"{_esc(signal.pair_url or '')}\">View Chart</a>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{format_premium_badge()}"
    )


async def _broadcast_premium_signal(bot, signal: PremiumSignal, wallets: list[PremiumWallet]) -> None:
    if bot is None:
        return

    text_card = _build_premium_signal_card(signal, wallets)

    async with async_session() as session:
        res = await session.execute(
            select(PremiumMembership.user_id).where(
                and_(
                    PremiumMembership.status == "active",
                    PremiumMembership.signal_alerts_enabled.is_(True),
                )
            )
        )
        user_ids = [row[0] for row in res.all()]

    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text_card)
        except Exception as e:
            logger.warning(f"Premium signal broadcast failed for {user_id}: {e}")
        await asyncio.sleep(0.05)


_below_threshold_logged = False


async def run_consensus_cycle(bot=None) -> dict:
    global _below_threshold_logged
    stats = {"candidates": 0, "ai_passed": 0, "signals_created": 0, "active_wallets": 0}

    async with async_session() as session:
        active_count = await _active_wallet_count(session)
    stats["active_wallets"] = active_count

    # Operational Threshold model: the engine simply does not generate
    # Premium Intelligence until the Minimum Operational Threshold is
    # met — no partial/low-confidence signals from a still-bootstrapping
    # database. Bootstrap Discovery (premium_wallet_discovery.py) is
    # already running fast specifically to clear this bar quickly.
    if active_count < PREMIUM_WALLET_INITIAL_TARGET:
        if not _below_threshold_logged:
            logger.info(
                f"⏳ Premium Signal generation paused: {active_count}/{PREMIUM_WALLET_INITIAL_TARGET} "
                "active Smart Wallets — waiting for the Minimum Operational Threshold before signaling."
            )
            _below_threshold_logged = True
        return stats
    if _below_threshold_logged:
        logger.info(f"✅ Minimum Operational Threshold met ({active_count} active wallets) — Premium Signals resuming.")
        _below_threshold_logged = False

    capacity_multiplier = _capacity_confidence_multiplier(active_count)

    async with async_session() as session:
        candidates = await _find_consensus_candidates(session)
    stats["candidates"] = len(candidates)

    for candidate in candidates:
        mint = candidate["mint"]
        try:
            ai_result = await asyncio.wait_for(_run_ai_analysis(mint), timeout=20)
        except Exception as e:
            logger.warning(f"Premium AI analysis failed for {mint}: {e}")
            continue

        if not ai_result or not ai_result.get("eligible"):
            continue

        ai_score = ai_result.get("final_score", 0.0)
        if ai_score < PREMIUM_CONSENSUS_MIN_AI_SCORE:
            continue

        stats["ai_passed"] += 1

        data = ai_result.get("data") or {}
        wallets = candidate["wallets"]

        confidence = round(
            min(
                100.0,
                (
                    (ai_score * 0.6) + (min(100.0, candidate["avg_reputation"]) * 0.25)
                    + (min(100.0, len(wallets) * 15) * 0.15)
                ) * capacity_multiplier,
            ),
            1,
        )

        signal = PremiumSignal(
            token_mint=mint,
            token_name=data.get("name"),
            token_symbol=data.get("symbol"),
            ai_score=ai_score,
            ai_tier=ai_result.get("tier"),
            ai_breakdown_json=json.dumps(ai_result.get("breakdown") or {}),
            consensus_wallet_count=len(wallets),
            consensus_wallet_addresses_json=json.dumps([w.wallet_address for w in wallets]),
            consensus_avg_reputation=candidate["avg_reputation"],
            consensus_window_minutes=PREMIUM_CONSENSUS_WINDOW_MINUTES,
            confidence_score=confidence,
            entry_price=_to_float(data.get("price")) or None,
            entry_market_cap=_to_float(data.get("market_cap")) or _to_float(data.get("fdv")) or None,
            entry_liquidity=_to_float(data.get("liquidity")) or None,
            current_price=_to_float(data.get("price")) or None,
            current_market_cap=_to_float(data.get("market_cap")) or None,
            pair_url=data.get("pair_url"),
        )

        async with async_session() as session:
            session.add(signal)
            for w in wallets:
                db_wallet = await session.get(PremiumWallet, w.id)
                if db_wallet:
                    db_wallet.signals_contributed_to = (db_wallet.signals_contributed_to or 0) + 1
            await session.commit()
            await session.refresh(signal)

        stats["signals_created"] += 1
        logger.info(
            f"💎 Premium Signal created: {signal.token_symbol} "
            f"(AI {ai_score:.0f}, consensus {len(wallets)} wallets, confidence {confidence:.0f})"
        )

        try:
            await _broadcast_premium_signal(bot, signal, wallets)
        except Exception as e:
            logger.error(f"Premium signal broadcast error: {e}")

    return stats


async def premium_monitor_loop(interval_seconds: int, bot=None) -> None:
    logger.info("👁️ Premium Wallet Monitor active")
    while True:
        try:
            await run_monitor_cycle()
            await run_consensus_cycle(bot=bot)
        except Exception as e:
            logger.error(f"Premium monitor/consensus cycle error: {e}")
        await asyncio.sleep(interval_seconds)


# ---------------------------------------------------------------------
# Read helpers for the /premium_signals command
# ---------------------------------------------------------------------

async def get_recent_premium_signals(limit: int = 10) -> list[PremiumSignal]:
    async with async_session() as session:
        res = await session.execute(
            select(PremiumSignal).order_by(PremiumSignal.signaled_at.desc()).limit(limit)
        )
        return res.scalars().all()
