"""
Discovery Engine B — Robinhood Chain Token Discovery (via DexScreener).

    DexScreener Discovery -> Potential Token Scoring -> Validation ->
    Snapshot -> Telegram Signal

Completely independent of Discovery Engine A / the Solana wallet
consensus rule (WhaleAlpha spec: "Do not accidentally require wallet
consensus for Robinhood tokens"). Reuses:

  * DexScreener client       -> providers.marketdata.dexscreener
                                 (get_latest_token_profiles /
                                 get_latest_boosted_tokens for "new"
                                 tokens, search_pairs_by_chain for
                                 "renewed activity" older tokens)
  * Safety validation + rich
    snapshot                 -> domain.signals.candidate_validation
                                 (same shared pipeline Engine A uses,
                                 so "Existing hard-reject conditions
                                 remain authoritative" here too)
  * Rich Telegram card       -> domain.signals.pump_radar.send_pump_card
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from infra.db.session import async_session
from config.settings import (
    ROBINHOOD_CHAIN_ID,
    ROBINHOOD_MAX_CANDIDATES_PER_CYCLE,
    ROBINHOOD_MAX_ALERTS_PER_CYCLE,
    ROBINHOOD_NEW_MAX_AGE_HOURS,
    ROBINHOOD_RENEWED_MIN_AGE_HOURS,
    ROBINHOOD_RENEWED_MIN_VOLUME_ACCELERATION,
    ROBINHOOD_MIN_LIQUIDITY_USD,
    ROBINHOOD_MIN_VOLUME_1H_USD,
    ROBINHOOD_MIN_TXNS_1H,
    ROBINHOOD_MAX_MC_LIQUIDITY_RATIO,
    ROBINHOOD_SCORE_WEIGHT_LIQUIDITY,
    ROBINHOOD_SCORE_WEIGHT_LIQUIDITY_CHANGE,
    ROBINHOOD_SCORE_WEIGHT_VOLUME,
    ROBINHOOD_SCORE_WEIGHT_VOLUME_ACCELERATION,
    ROBINHOOD_SCORE_WEIGHT_BUY_SELL_PRESSURE,
    ROBINHOOD_SCORE_WEIGHT_TX_ACCELERATION,
    ROBINHOOD_SCORE_WEIGHT_MOMENTUM,
    ROBINHOOD_MIN_SCORE_TO_ALERT,
    ROBINHOOD_COOLDOWN_HOURS,
)
from models.robinhood_discovery_signal import RobinhoodDiscoverySignal
from providers.marketdata.dexscreener import (
    get_latest_token_profiles,
    get_latest_boosted_tokens,
    search_pairs_by_chain,
    get_token_card_info,
)
from domain.signals.candidate_validation import build_validated_candidate
from domain.signals.pump_radar import send_pump_card
from domain.signals.channel_config import load_channel_ids

logger = logging.getLogger("WhaleAlpha.RobinhoodDiscovery")

ROBINHOOD_TITLE = "🚀 <b>WHALEALPHA — ROBINHOOD DISCOVERY</b>"


def _now():
    return datetime.now(timezone.utc)


def _f(value, default: float = 0.0) -> float:
    try:
        if value in (None, "N/A", ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _pair_age_hours(pair_created_at) -> float | None:
    try:
        ms = float(pair_created_at)
    except (TypeError, ValueError):
        return None
    created = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return (_now() - created).total_seconds() / 3600.0


async def discover_candidates() -> list[dict]:
    """
    Discovers BOTH:
      * new Robinhood Chain tokens (fresh pairs from DexScreener's
        latest-profiles/latest-boosts feeds, filtered to
        ROBINHOOD_CHAIN_ID)
      * older Robinhood Chain tokens showing renewed/high-potential
        activity (via search_pairs_by_chain, age- and
        volume-acceleration-filtered)

    Deduplicates by contract address. Never fabricates data -- any pair
    missing usable market data is simply skipped.
    """
    seen: dict[str, dict] = {}

    for fetch_fn, source in (
        (get_latest_token_profiles, "dexscreener_new"),
        (get_latest_boosted_tokens, "dexscreener_new"),
    ):
        try:
            items = await fetch_fn()
        except Exception as e:
            logger.warning(f"Robinhood discovery: {fetch_fn.__name__} failed: {e}")
            continue
        for item in items or []:
            if (item.get("chainId") or "").strip().lower() != ROBINHOOD_CHAIN_ID:
                continue
            contract = (item.get("tokenAddress") or item.get("address") or "").strip()
            if not contract or contract in seen:
                continue
            seen[contract] = {"contract": contract, "source": source, "prefetched_data": None}

    try:
        renewed_pairs = await search_pairs_by_chain(ROBINHOOD_CHAIN_ID, ROBINHOOD_CHAIN_ID)
    except Exception as e:
        logger.warning(f"Robinhood discovery: search_pairs_by_chain failed: {e}")
        renewed_pairs = []

    for pair in renewed_pairs:
        contract = pair.get("contract")
        if not contract:
            continue
        age_hours = _pair_age_hours(pair.get("pair_created"))
        if age_hours is not None and age_hours < ROBINHOOD_RENEWED_MIN_AGE_HOURS:
            # A young pair belongs in the "new" bucket, not "renewed".
            if contract not in seen:
                seen[contract] = {"contract": contract, "source": "dexscreener_new", "prefetched_data": pair}
            continue
        # "Renewed activity": require a real volume acceleration signal
        # (1h volume annualized-to-24h vs the trailing 24h average),
        # never raw volume alone.
        vol_1h = _f(pair.get("volume_1h"))
        vol_24h = _f(pair.get("volume_24h"))
        avg_hourly_24h = vol_24h / 24.0 if vol_24h else 0.0
        acceleration = (vol_1h / avg_hourly_24h) if avg_hourly_24h > 0 else 0.0
        if acceleration < ROBINHOOD_RENEWED_MIN_VOLUME_ACCELERATION:
            continue
        seen[contract] = {"contract": contract, "source": "dexscreener_renewed", "prefetched_data": pair}

    return list(seen.values())[:ROBINHOOD_MAX_CANDIDATES_PER_CYCLE]


def _passes_activity_filters(data: dict) -> tuple[bool, list[str]]:
    """Coarse pre-filter (liquidity/volume/tx floors + mc:liquidity
    sanity ratio) applied BEFORE the shared safety pipeline, to avoid
    wasting security-API calls on obviously unusable/illiquid candidates.
    """
    reasons = []
    liquidity = _f(data.get("liquidity"))
    volume_1h = _f(data.get("volume_1h"))
    txns_1h = int(_f(data.get("txns_1h_buys")) + _f(data.get("txns_1h_sells")))
    market_cap = _f(data.get("market_cap"))

    if liquidity < ROBINHOOD_MIN_LIQUIDITY_USD:
        reasons.append("liquidity_below_minimum")
    if volume_1h < ROBINHOOD_MIN_VOLUME_1H_USD:
        reasons.append("volume_below_minimum")
    if txns_1h < ROBINHOOD_MIN_TXNS_1H:
        reasons.append("txns_below_minimum")
    if liquidity > 0 and market_cap > 0 and (market_cap / liquidity) > ROBINHOOD_MAX_MC_LIQUIDITY_RATIO:
        reasons.append("mc_liquidity_ratio_too_high")

    return (len(reasons) == 0), reasons


def score_potential(data: dict) -> tuple[float, dict]:
    """
    Configurable weighted potential score. Every weight is a
    config.settings.ROBINHOOD_SCORE_WEIGHT_* value (see spec: "The
    exact scoring formula should be configurable and should avoid
    treating raw volume alone as sufficient evidence") -- raw volume is
    one of seven weighted components here, never scored alone.

    Each component is normalized to 0-100 before weighting.
    """
    liquidity = _f(data.get("liquidity"))
    liquidity_change_1h = _f(data.get("price_change_1h"))  # proxy: DexScreener has no direct liq-change field
    volume_1h = _f(data.get("volume_1h"))
    volume_24h = _f(data.get("volume_24h"))
    avg_hourly_24h = volume_24h / 24.0 if volume_24h else 0.0
    volume_acceleration = (volume_1h / avg_hourly_24h) if avg_hourly_24h > 0 else 1.0
    buys_1h = _f(data.get("txns_1h_buys"))
    sells_1h = _f(data.get("txns_1h_sells"))
    buy_sell_pressure = (buys_1h / (buys_1h + sells_1h)) if (buys_1h + sells_1h) > 0 else 0.5
    txns_24h = _f(data.get("txns_24h_buys")) + _f(data.get("txns_24h_sells"))
    txns_1h_total = buys_1h + sells_1h
    avg_hourly_txns_24h = txns_24h / 24.0 if txns_24h else 0.0
    tx_acceleration = (txns_1h_total / avg_hourly_txns_24h) if avg_hourly_txns_24h > 0 else 1.0
    momentum = _f(data.get("price_change_1h"))

    components = {
        "liquidity": min(100.0, (liquidity / 100_000.0) * 100),
        "liquidity_change": max(0.0, min(100.0, 50 + liquidity_change_1h)),
        "volume": min(100.0, (volume_1h / 50_000.0) * 100),
        "volume_acceleration": min(100.0, volume_acceleration * 25),
        "buy_sell_pressure": buy_sell_pressure * 100,
        "tx_acceleration": min(100.0, tx_acceleration * 25),
        "momentum": max(0.0, min(100.0, 50 + momentum)),
    }

    weights = {
        "liquidity": ROBINHOOD_SCORE_WEIGHT_LIQUIDITY,
        "liquidity_change": ROBINHOOD_SCORE_WEIGHT_LIQUIDITY_CHANGE,
        "volume": ROBINHOOD_SCORE_WEIGHT_VOLUME,
        "volume_acceleration": ROBINHOOD_SCORE_WEIGHT_VOLUME_ACCELERATION,
        "buy_sell_pressure": ROBINHOOD_SCORE_WEIGHT_BUY_SELL_PRESSURE,
        "tx_acceleration": ROBINHOOD_SCORE_WEIGHT_TX_ACCELERATION,
        "momentum": ROBINHOOD_SCORE_WEIGHT_MOMENTUM,
    }

    total_weight = sum(weights.values()) or 1.0
    score = sum(components[k] * weights[k] for k in weights) / total_weight
    return round(score, 1), components


async def _get_or_init_signal_row(session, contract: str) -> RobinhoodDiscoverySignal | None:
    res = await session.execute(
        select(RobinhoodDiscoverySignal).where(RobinhoodDiscoverySignal.token_contract == contract)
    )
    return res.scalar_one_or_none()


def _cooldown_active(row: RobinhoodDiscoverySignal | None) -> bool:
    if row is None or row.cooldown_expires_at is None:
        return False
    expires = row.cooldown_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > _now()


async def run_robinhood_discovery_cycle(bot=None) -> dict:
    stats = {
        "pairs_scanned": 0,
        "tokens_discovered": 0,
        "tokens_rejected": 0,
        "tokens_promoted": 0,
        "signals_sent": 0,
        "cooldown_skipped": 0,
    }

    channel_ids = load_channel_ids("ROBINHOOD_ALERT_CHANNEL_IDS")

    try:
        candidates = await discover_candidates()
    except Exception as e:
        logger.error(f"Robinhood discovery: candidate discovery failed: {e}")
        return stats

    stats["pairs_scanned"] = len(candidates)
    alerts_sent_this_cycle = 0

    async with async_session() as session:
        for entry in candidates:
            if alerts_sent_this_cycle >= ROBINHOOD_MAX_ALERTS_PER_CYCLE:
                break

            contract = entry["contract"]
            source = entry["source"]
            prefetched = entry.get("prefetched_data")

            existing = await _get_or_init_signal_row(session, contract)
            if _cooldown_active(existing):
                stats["cooldown_skipped"] += 1
                continue

            data = prefetched
            if data is None:
                try:
                    data = await get_token_card_info(contract, ROBINHOOD_CHAIN_ID)
                except Exception as e:
                    logger.warning(f"Robinhood discovery: market data fetch failed for {contract}: {e}")
                    continue
            if not data:
                continue

            stats["tokens_discovered"] += 1

            passes, reasons = _passes_activity_filters(data)
            if not passes:
                stats["tokens_rejected"] += 1
                continue

            score, breakdown = score_potential(data)
            if score < ROBINHOOD_MIN_SCORE_TO_ALERT:
                stats["tokens_rejected"] += 1
                continue

            try:
                card, reject_reasons = await build_validated_candidate(
                    contract, chain=ROBINHOOD_CHAIN_ID, prefetched_data=data
                )
            except Exception as e:
                logger.error(f"Robinhood discovery: validation failed for {contract}: {e}")
                continue

            if reject_reasons:
                logger.info(f"Robinhood discovery: {contract} rejected by safety validation: {reject_reasons}")
                stats["tokens_rejected"] += 1
                continue

            stats["tokens_promoted"] += 1

            why_selected = [
                f"📡 Source: DexScreener ({'new pair' if source == 'dexscreener_new' else 'renewed activity'})",
                f"🧮 Discovery score: <b>{score}/100</b>",
                f"💧 Liquidity: ${_f(data.get('liquidity')):,.0f} | 📊 1h Volume: ${_f(data.get('volume_1h')):,.0f}",
            ]

            if existing is None:
                existing = RobinhoodDiscoverySignal(token_contract=contract)
                session.add(existing)
                existing.times_alerted = 0
                existing.first_alerted_at = _now()

            existing.pair_address = data.get("pool_address")
            existing.token_symbol = data.get("symbol")
            existing.token_name = data.get("name")
            existing.chain = ROBINHOOD_CHAIN_ID
            existing.discovery_source = source
            existing.discovery_score = score
            existing.score_breakdown_json = json.dumps(breakdown)
            existing.reasons_json = json.dumps(why_selected)
            existing.snapshot_json = json.dumps(data)
            existing.status = "active"
            existing.times_alerted = (existing.times_alerted or 0) + 1
            existing.last_alerted_at = _now()
            existing.cooldown_expires_at = _now() + timedelta(hours=ROBINHOOD_COOLDOWN_HOURS)

            if bot is not None and channel_ids:
                for chat_id in channel_ids:
                    try:
                        await send_pump_card(
                            bot, chat_id, card,
                            title=ROBINHOOD_TITLE,
                            extra_block=why_selected,
                        )
                    except Exception as e:
                        logger.warning(f"Robinhood discovery: send failed for {contract} -> {chat_id}: {e}")

            stats["signals_sent"] += 1
            alerts_sent_this_cycle += 1

        await session.commit()

    return stats


async def robinhood_discovery_loop(bot, interval_seconds: int = 300) -> None:
    """Background loop -- see workers/signal_trading_worker.py for wiring
    (same run_as_leader(...) convention as every other scanning loop)."""
    import asyncio

    while True:
        try:
            stats = await run_robinhood_discovery_cycle(bot)
            logger.info(f"Robinhood discovery cycle: {stats}")
        except Exception as e:
            logger.error(f"Robinhood discovery cycle failed: {e}")
        await asyncio.sleep(interval_seconds)
