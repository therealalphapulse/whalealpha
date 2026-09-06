"""
Discovery Engine A — Solana Profitable Wallet Consensus.

    Profitable Wallet Discovery -> Purchase Tracking -> 3+ Unique
    Wallet Consensus -> Validation -> Snapshot -> Telegram Signal

Reuses existing infrastructure end-to-end rather than rebuilding it:

  * Wallet / WalletPurchase entities  -> models.premium_wallet.PremiumWallet
                                          models.premium_wallet_trade.PremiumWalletTrade
  * Wallet discovery                  -> domain.intelligence.premium_wallet_discovery
  * Wallet scoring/classification     -> domain.intelligence.premium_wallet_scorer
  * Purchase tracking (Helius poll)   -> domain.intelligence.premium_signal_engine.run_monitor_cycle
  * Safety validation + rich snapshot -> domain.signals.candidate_validation
                                          (itself reusing domain.signals.scoring and
                                          domain.intelligence.holders)
  * Rich Telegram card                -> domain.signals.pump_radar.send_pump_card /
                                          _build_pump_card_text (extended, not forked)

This module is deliberately independent of the Premium membership
broadcast in premium_signal_engine.py (run_consensus_cycle /
_broadcast_premium_signal): those keep sending their own simpler card
to paying members with a different (AI-score-gated) trigger, while
THIS module sends the existing rich public signal card to
config.settings.WALLET_CONSENSUS_ALERT_CHANNEL_IDS whenever the pure
wallet-consensus threshold (default: 3 distinct qualifying profitable
wallets, 60-minute window) is met -- exactly the WhaleAlpha spec's
Discovery Engine A trigger, with no additional AI-conviction gate.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, and_

from infra.db.session import async_session
from config.settings import (
    WALLET_CONSENSUS_MIN_WALLETS,
    WALLET_CONSENSUS_WINDOW_MINUTES,
    WALLET_CONSENSUS_MIN_WALLET_SCORE,
    WALLET_CONSENSUS_REQUIRE_PROFITABLE_CLASSIFICATION,
    WALLET_CONSENSUS_MAX_PURCHASE_AGE_MINUTES,
    WALLET_CONSENSUS_COOLDOWN_HOURS,
)
from models.premium_wallet import PremiumWallet
from models.premium_wallet_trade import PremiumWalletTrade
from models.wallet_consensus_signal import WalletConsensusSignal
from domain.intelligence.premium_signal_engine import run_monitor_cycle
from domain.intelligence.funding_graph import get_funding_clusters
from domain.signals.candidate_validation import build_validated_candidate
from domain.signals.pump_radar import send_pump_card
from domain.signals.channel_config import load_channel_ids

logger = logging.getLogger("WhaleAlpha.WalletConsensusEngine")

WALLET_CONSENSUS_TITLE = "🐋 <b>WHALEALPHA — WALLET CONSENSUS</b>"


def _now():
    return datetime.now(timezone.utc)


def _short(addr: str, size: int = 5) -> str:
    if not addr or len(addr) <= size * 2 + 3:
        return addr or ""
    return f"{addr[:size]}...{addr[-size:]}"


def _wallet_qualifies(wallet: PremiumWallet) -> bool:
    """A "qualifying profitable wallet" per the WhaleAlpha spec: active,
    above the reputation bar, and (by default) explicitly tagged
    profitable_trader by domain.intelligence.premium_wallet_scorer."""
    if wallet.status != "active":
        return False
    if (wallet.reputation_score or 0) < WALLET_CONSENSUS_MIN_WALLET_SCORE:
        return False
    if WALLET_CONSENSUS_REQUIRE_PROFITABLE_CLASSIFICATION:
        tags = (wallet.classification or "").split(",")
        if "profitable_trader" not in [t.strip() for t in tags]:
            return False
    return True


def _group_consensus_candidates(
    rows: list[tuple],
    *,
    window_start: datetime,
    min_wallets: int = WALLET_CONSENSUS_MIN_WALLETS,
) -> list[dict]:
    """
    Pure grouping/threshold logic, factored out of
    find_wallet_consensus_candidates() so it is directly unit-testable
    with plain (trade, wallet) tuples -- no DB/session mocking required.

    `rows` is expected to already be (trade, wallet) pairs for side=="buy"
    trades on active wallets (the SQL query below applies that same
    filter), ordered ascending by detected_at. This function re-applies
    the observation-window cutoff itself (belt-and-braces against any
    caller that hands it unfiltered rows -- e.g. tests) and is the ONLY
    place the "distinct wallet" / "3+ threshold" / "one buy or many buys
    counts once" rules live.
    """
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)

    by_token: dict[str, dict[str, tuple]] = defaultdict(dict)
    token_symbols: dict[str, str] = {}

    for trade, wallet in rows:
        detected_at = trade.detected_at
        if detected_at is not None:
            if detected_at.tzinfo is None:
                detected_at = detected_at.replace(tzinfo=timezone.utc)
            if detected_at < window_start:
                continue  # outside the observation window -- does not count
        if not _wallet_qualifies(wallet):
            continue
        # keep the EARLIEST qualifying buy per wallet per token as the
        # evidence row (rows are expected ordered ascending) -- multiple
        # buys / transactions by the same wallet still count as exactly
        # one contributor, regardless of how many rows it has here.
        by_token[trade.token_mint].setdefault(wallet.wallet_address, (trade, wallet))
        if trade.token_symbol:
            token_symbols[trade.token_mint] = trade.token_symbol

    candidates = []
    for mint, contributors in by_token.items():
        if len(contributors) < min_wallets:
            continue

        wallets = [w for _, w in contributors.values()]
        avg_rep = sum((w.reputation_score or 0) for w in wallets) / len(wallets)

        candidates.append(
            {
                "mint": mint,
                "token_symbol": token_symbols.get(mint),
                "contributors": contributors,  # {address: (trade, wallet)}
                "avg_reputation": round(avg_rep, 1),
            }
        )

    return candidates


async def find_wallet_consensus_candidates(session) -> list[dict]:
    """
    Finds Solana tokens where >= WALLET_CONSENSUS_MIN_WALLETS DISTINCT
    qualifying profitable wallets bought within
    WALLET_CONSENSUS_WINDOW_MINUTES. The same wallet counts only once
    no matter how many buys/transactions it made (dict keyed by
    wallet_address, per spec example).

    Uses transaction-level evidence only (PremiumWalletTrade rows
    created from real detected transfers) -- never current holdings --
    per spec: "Merely observing that wallets currently hold a token is
    not sufficient to establish a new consensus event."
    """
    window_start = _now() - timedelta(minutes=WALLET_CONSENSUS_WINDOW_MINUTES)
    purchase_age_cutoff = _now() - timedelta(minutes=WALLET_CONSENSUS_MAX_PURCHASE_AGE_MINUTES)
    effective_start = max(window_start, purchase_age_cutoff)

    res = await session.execute(
        select(PremiumWalletTrade, PremiumWallet)
        .join(PremiumWallet, PremiumWallet.id == PremiumWalletTrade.wallet_id)
        .where(
            and_(
                PremiumWalletTrade.side == "buy",
                PremiumWalletTrade.detected_at >= effective_start,
                PremiumWallet.status == "active",
            )
        )
        .order_by(PremiumWalletTrade.detected_at.asc())
    )
    rows = res.all()

    return _group_consensus_candidates(rows, window_start=effective_start)


async def _get_or_init_signal_row(session, mint: str) -> WalletConsensusSignal | None:
    res = await session.execute(
        select(WalletConsensusSignal).where(WalletConsensusSignal.token_contract == mint)
    )
    return res.scalar_one_or_none()


def _cooldown_active(row: WalletConsensusSignal | None) -> bool:
    if row is None:
        return False
    if row.cooldown_expires_at is None:
        return False
    now = _now()
    expires = row.cooldown_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > now


async def run_wallet_consensus_cycle(bot=None) -> dict:
    """
    One full pass of Discovery Engine A:

      1. Purchase tracking (delegates to the existing Premium monitor).
      2. Consensus detection (>=3 distinct qualifying wallets/token).
      3. Existing safety/risk validation (never bypassed).
      4. Rich snapshot + WALLET_CONSENSUS Telegram signal.
      5. Persistence of evidence + dedupe/cooldown/re-arm.
    """
    stats = {
        "purchases_observed": 0,
        "consensus_candidates": 0,
        "signals_sent": 0,
        "safety_rejected": 0,
        "cooldown_skipped": 0,
    }

    try:
        monitor_stats = await run_monitor_cycle()
        stats["purchases_observed"] = (monitor_stats or {}).get("trades_recorded", 0)
    except Exception as e:
        # Provider failure must not crash the whole bot -- purchase
        # tracking is best-effort per cycle; consensus detection below
        # still runs against whatever evidence already exists.
        logger.error(f"Wallet consensus: purchase tracking cycle failed: {e}")

    channel_ids = load_channel_ids("WALLET_CONSENSUS_ALERT_CHANNEL_IDS")

    async with async_session() as session:
        candidates = await find_wallet_consensus_candidates(session)
        stats["consensus_candidates"] = len(candidates)

        for candidate in candidates:
            mint = candidate["mint"]
            contributors = candidate["contributors"]

            existing = await _get_or_init_signal_row(session, mint)
            if _cooldown_active(existing):
                stats["cooldown_skipped"] += 1
                continue

            try:
                card, reject_reasons = await build_validated_candidate(mint, chain="solana")
            except Exception as e:
                logger.error(f"Wallet consensus: validation failed for {mint}: {e}")
                continue

            if reject_reasons:
                logger.info(f"Wallet consensus: {mint} rejected by safety validation: {reject_reasons}")
                stats["safety_rejected"] += 1
                continue

            wallet_addresses = sorted(contributors.keys())
            classifications = {addr: (w.classification or "") for addr, (_, w) in contributors.items()}
            evidence = [
                {
                    "wallet": addr,
                    "signature": trade.signature,
                    "detected_at": trade.detected_at.isoformat() if trade.detected_at else None,
                    "amount": trade.amount,
                    "value_usd": trade.value_usd_at_detection,
                }
                for addr, (trade, _) in contributors.items()
            ]

            coordinated_cluster = None
            try:
                clusters = await get_funding_clusters(wallet_addresses)
                if clusters and clusters.get("largest_cluster_size", 0) >= 2:
                    coordinated_cluster = clusters
            except Exception as e:
                logger.warning(f"Wallet consensus: funding-cluster check failed for {mint}: {e}")

            confidence_score = round(
                0.6 * (card.get("pump", {}).get("final_score") or 0) + 0.4 * candidate["avg_reputation"],
                1,
            )

            extra_lines = [
                f"🤝 <b>{len(wallet_addresses)} distinct profitable wallets</b> bought within "
                f"{int(WALLET_CONSENSUS_WINDOW_MINUTES)}m",
            ]
            for addr in wallet_addresses[:8]:
                tags = classifications.get(addr, "") or "unclassified"
                extra_lines.append(f"  • <code>{_short(addr)}</code> — {tags}")
            if coordinated_cluster:
                extra_lines.append(
                    f"⚠️ Coordinated funding cluster detected among {coordinated_cluster.get('largest_cluster_size')} of these wallets"
                )
            extra_lines.append(f"⭐ Avg wallet reputation: {candidate['avg_reputation']}/100")

            if existing is None:
                existing = WalletConsensusSignal(token_contract=mint)
                session.add(existing)
                existing.times_alerted = 0
                existing.first_alerted_at = _now()

            d = card["data"]
            existing.token_symbol = d.get("symbol") or candidate.get("token_symbol")
            existing.token_name = d.get("name")
            existing.chain = "solana"
            existing.wallet_count = len(wallet_addresses)
            existing.wallet_addresses_json = json.dumps(wallet_addresses)
            existing.wallet_classifications_json = json.dumps(classifications)
            existing.transaction_evidence_json = json.dumps(evidence)
            existing.coordinated_cluster_json = json.dumps(coordinated_cluster) if coordinated_cluster else None
            existing.avg_wallet_reputation = candidate["avg_reputation"]
            existing.observation_window_minutes = WALLET_CONSENSUS_WINDOW_MINUTES
            existing.confidence_score = confidence_score
            existing.snapshot_json = json.dumps(d)
            existing.status = "active"
            existing.times_alerted = (existing.times_alerted or 0) + 1
            existing.last_alerted_at = _now()
            existing.cooldown_expires_at = _now() + timedelta(hours=WALLET_CONSENSUS_COOLDOWN_HOURS)

            if bot is not None and channel_ids:
                for chat_id in channel_ids:
                    try:
                        await send_pump_card(
                            bot, chat_id, card,
                            title=WALLET_CONSENSUS_TITLE,
                            extra_block=extra_lines,
                        )
                    except Exception as e:
                        logger.warning(f"Wallet consensus: send failed for {mint} -> {chat_id}: {e}")

            stats["signals_sent"] += 1

        await session.commit()

    return stats


async def wallet_consensus_loop(bot, interval_seconds: int = 120) -> None:
    """Background loop -- see workers/signal_trading_worker.py for wiring
    (same run_as_leader(...) convention as every other scanning loop)."""
    import asyncio

    while True:
        try:
            stats = await run_wallet_consensus_cycle(bot)
            logger.info(f"Wallet consensus cycle: {stats}")
        except Exception as e:
            # Per-provider isolation: one bad cycle never kills the loop.
            logger.error(f"Wallet consensus cycle failed: {e}")
        await asyncio.sleep(interval_seconds)
