"""
Discovery Engine B — Robinhood Chain Token Discovery (via DexScreener).

    DexScreener Discovery -> Two Intelligence Lanes -> Validation ->
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

v2 — two intelligence-driven lanes instead of one shared formula that
rewarded raw pumped %:

  * FRESH lane (new pairs) — scored for quality + moderate/sustained
    momentum, never a token's already-completed move. A candidate
    already up 80%+ in the last hour is scored as MORE likely near a
    local top than "about to pump", not less. Only the top N
    candidates BY SCORE get alerted each cycle (ROBINHOOD_FRESH_TOP_N_
    PER_CYCLE) — genuine "top picks", not everything above a static
    bar. See score_fresh_potential().

  * REVIVAL lane (older pairs) — only ever alerts a token our own
    cross-cycle history (models.robinhood_token_watch.RobinhoodTokenWatch,
    updated every cycle for every candidate observed) shows genuinely
    dumped from a tracked local high and is now recovering off a
    tracked local low — never just "any old token with rising volume",
    which is what the previous "renewed" bucket alerted on. See
    score_revival_potential().

Both lanes share the same pre-filters, safety-validation pipeline,
cooldown ledger, and Telegram delivery as before.
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
    ROBINHOOD_SCORE_WEIGHT_VOLUME,
    ROBINHOOD_SCORE_WEIGHT_VOLUME_ACCELERATION,
    ROBINHOOD_SCORE_WEIGHT_BUY_SELL_PRESSURE,
    ROBINHOOD_SCORE_WEIGHT_TX_ACCELERATION,
    ROBINHOOD_SCORE_WEIGHT_MOMENTUM_QUALITY,
    ROBINHOOD_MIN_SCORE_TO_ALERT,
    ROBINHOOD_FRESH_MAX_PRICE_CHANGE_1H_PCT,
    ROBINHOOD_FRESH_MAX_PRICE_CHANGE_5M_PCT,
    ROBINHOOD_FRESH_TOP_N_PER_CYCLE,
    ROBINHOOD_REVIVAL_MIN_SAMPLES,
    ROBINHOOD_REVIVAL_MIN_DRAWDOWN_PCT,
    ROBINHOOD_REVIVAL_MIN_RECOVERY_PCT,
    ROBINHOOD_REVIVAL_MAX_RECOVERY_PCT,
    ROBINHOOD_REVIVAL_TOP_N_PER_CYCLE,
    ROBINHOOD_COOLDOWN_HOURS,
)
from models.robinhood_discovery_signal import RobinhoodDiscoverySignal
from models.robinhood_token_watch import RobinhoodTokenWatch
from providers.marketdata.dexscreener import (
    get_latest_token_profiles,
    get_latest_boosted_tokens,
    search_pairs_by_chain,
    get_token_card_info,
)
from domain.signals.candidate_validation import build_validated_candidate
from domain.signals.pump_radar import send_pump_card, get_pump_subscribers
from domain.signals.channel_config import load_channel_ids
from domain.signals.signal_tracker import create_signal_from_candidate, update_signal_message_ids

logger = logging.getLogger("WhaleAlpha.RobinhoodDiscovery")

ROBINHOOD_TITLE = "🚀 <b>WHALEALPHA — ROBINHOOD DISCOVERY</b>"
FRESH_TITLE = "🚀 <b>WHALEALPHA — ROBINHOOD DISCOVERY</b> · 🔥 Fresh Pick"
REVIVAL_TITLE = "🚀 <b>WHALEALPHA — ROBINHOOD DISCOVERY</b> · 🔁 Second Wind"


def _now():
    # Naive UTC, matching every other DateTime column in this codebase
    # (TIMESTAMP WITHOUT TIME ZONE) -- a tz-aware datetime cannot be bound
    # to those columns by asyncpg.
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    created = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)
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

    This only decides which DexScreener FEED a candidate came from
    (source label) and a coarse pre-filter on the "renewed" feed to
    avoid wasting work on obviously dead pairs. The actual FRESH vs
    REVIVAL lane assignment happens later in run_robinhood_discovery_
    cycle, based on the candidate's real observed age and our own
    tracked price history -- not this label.

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
        # Coarse pre-filter: require SOME real volume acceleration
        # signal before this even enters consideration for the REVIVAL
        # lane's much stricter dump-then-recovery check below.
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


async def _get_or_update_token_watch(session, contract: str, data: dict) -> RobinhoodTokenWatch:
    """
    Records this observation in the cross-cycle history table and
    returns the updated row. Called for EVERY candidate that clears
    the activity pre-filter, whether or not it ends up promoted or
    alerted -- the REVIVAL lane's drawdown/recovery detection depends
    on this running continuously, not just on cycles that send
    something. Tracks a local high (reset whenever a new high is set)
    and the lowest price seen since that high (the "dump bottom"), so
    a real dump-then-recovery shape can be measured across cycles
    instead of guessed from one DexScreener snapshot.
    """
    price = _f(data.get("price"))
    liquidity = _f(data.get("liquidity"))
    volume_1h = _f(data.get("volume_1h"))
    now = _now()

    res = await session.execute(
        select(RobinhoodTokenWatch).where(RobinhoodTokenWatch.token_contract == contract)
    )
    row = res.scalar_one_or_none()

    if row is None:
        row = RobinhoodTokenWatch(
            token_contract=contract,
            first_seen_at=now,
            last_seen_at=now,
            samples_count=0,
        )
        session.add(row)

    if price > 0:
        if row.local_high_price is None or price > row.local_high_price:
            # New high -- the drawdown/recovery window resets: this
            # cycle's price becomes the new peak with no dump recorded
            # under it yet.
            row.local_high_price = price
            row.local_high_at = now
            row.local_low_price_since_high = None
            row.local_low_at = None
        elif row.local_low_price_since_high is None or price < row.local_low_price_since_high:
            row.local_low_price_since_high = price
            row.local_low_at = now
        row.last_price = price

    if liquidity > 0:
        row.last_liquidity = liquidity
    if volume_1h > 0:
        row.last_volume_1h = volume_1h

    row.last_seen_at = now
    row.samples_count = (row.samples_count or 0) + 1
    return row


def score_fresh_potential(data: dict) -> tuple[float, dict]:
    """
    FRESH-lane score -- for new pairs. Rewards quality plus moderate,
    sustained, multi-timeframe-aligned momentum; never raw pumped %.

    Replaces the old formula, which counted "price already pumped in
    the last hour" TWICE (once as "momentum", again as a
    "liquidity_change" proxy that was, per its own old comment,
    literally the same number -- DexScreener has no real
    liquidity-change field). That double-counted, unbounded reward for
    tokens that had ALREADY spiked hard was the single biggest driver
    of "alert fires right as the token tops out, then dumps".

    momentum_quality below rewards a 5-40% 1h move (the "just starting
    to build" sweet spot) most highly, decays the reward above that,
    and actively PENALIZES moves past ROBINHOOD_FRESH_MAX_PRICE_CHANGE_
    1H_PCT -- an already-extended token is scored as closer to a local
    top than to an entry. A single 5-minute candle accounting for most
    of the 1h move (a blow-off-top shape, not a trend) is penalized
    regardless of the 1h number, and multi-timeframe alignment (5m, 1h,
    6h all positive) earns a small bonus for looking like a real trend
    rather than an isolated spike.
    """
    liquidity = _f(data.get("liquidity"))
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

    pc_5m = _f(data.get("price_change_5m"))
    pc_1h = _f(data.get("price_change_1h"))
    pc_6h = _f(data.get("price_change_6h"))

    def _momentum_quality() -> float:
        if pc_1h < 0:
            base = max(0.0, 40.0 + pc_1h)
        elif pc_1h <= 5:
            base = 50.0 + pc_1h * 2.0
        elif pc_1h <= 40:
            base = 60.0 + (pc_1h - 5.0) * (40.0 / 35.0)
        elif pc_1h <= ROBINHOOD_FRESH_MAX_PRICE_CHANGE_1H_PCT:
            span = max(1.0, ROBINHOOD_FRESH_MAX_PRICE_CHANGE_1H_PCT - 40.0)
            base = 100.0 - (pc_1h - 40.0) * (40.0 / span)
        else:
            over = pc_1h - ROBINHOOD_FRESH_MAX_PRICE_CHANGE_1H_PCT
            base = max(0.0, 60.0 - over * 0.5)

        if pc_1h > 0 and abs(pc_5m) >= ROBINHOOD_FRESH_MAX_PRICE_CHANGE_5M_PCT:
            base *= 0.5  # blow-off-top shape: most of the move in one 5m candle

        if pc_5m > 0 and pc_1h > 0 and pc_6h > 0:
            base = min(100.0, base + 10.0)  # aligned trend across timeframes

        return max(0.0, min(100.0, base))

    components = {
        "liquidity": min(100.0, (liquidity / 100_000.0) * 100),
        "volume": min(100.0, (volume_1h / 50_000.0) * 100),
        "volume_acceleration": min(100.0, volume_acceleration * 25),
        "buy_sell_pressure": buy_sell_pressure * 100,
        "tx_acceleration": min(100.0, tx_acceleration * 25),
        "momentum_quality": _momentum_quality(),
    }
    weights = {
        "liquidity": ROBINHOOD_SCORE_WEIGHT_LIQUIDITY,
        "volume": ROBINHOOD_SCORE_WEIGHT_VOLUME,
        "volume_acceleration": ROBINHOOD_SCORE_WEIGHT_VOLUME_ACCELERATION,
        "buy_sell_pressure": ROBINHOOD_SCORE_WEIGHT_BUY_SELL_PRESSURE,
        "tx_acceleration": ROBINHOOD_SCORE_WEIGHT_TX_ACCELERATION,
        "momentum_quality": ROBINHOOD_SCORE_WEIGHT_MOMENTUM_QUALITY,
    }
    total_weight = sum(weights.values()) or 1.0
    score = sum(components[k] * weights[k] for k in weights) / total_weight
    return round(score, 1), components


def score_revival_potential(data: dict, watch) -> "tuple[float, dict] | None":
    """
    REVIVAL-lane score -- for older pairs. Returns None ("not
    qualified for this lane at all", not just "scored low") unless our
    own cross-cycle history on `watch` shows a genuine dump-then-
    recovery shape:

      1. Enough observed history to trust the high/low
         (samples_count >= ROBINHOOD_REVIVAL_MIN_SAMPLES)
      2. A real drawdown from the tracked local high
         (>= ROBINHOOD_REVIVAL_MIN_DRAWDOWN_PCT)
      3. A real bounce off the tracked local low
         (>= ROBINHOOD_REVIVAL_MIN_RECOVERY_PCT)
      4. Not already back near the old high
         (<= ROBINHOOD_REVIVAL_MAX_RECOVERY_PCT of the drawdown
         reclaimed) -- past that point this is chasing an
         already-complete recovery, not catching a fresh one

    An old token with rising volume that never actually corrected
    first (never dumped) fails step 2 and is rejected outright --
    that's not a revival, whatever its volume looks like.
    """
    if watch is None or (watch.samples_count or 0) < ROBINHOOD_REVIVAL_MIN_SAMPLES:
        return None

    high = watch.local_high_price
    low = watch.local_low_price_since_high
    last = watch.last_price
    if not high or not low or not last or high <= 0 or low <= 0 or high <= low:
        return None

    drawdown_pct = (high - low) / high * 100.0
    if drawdown_pct < ROBINHOOD_REVIVAL_MIN_DRAWDOWN_PCT:
        return None

    recovery_pct = (last - low) / low * 100.0
    if recovery_pct < ROBINHOOD_REVIVAL_MIN_RECOVERY_PCT:
        return None

    recovery_of_drawdown_pct = (last - low) / (high - low) * 100.0
    if recovery_of_drawdown_pct > ROBINHOOD_REVIVAL_MAX_RECOVERY_PCT:
        return None

    liquidity = _f(data.get("liquidity"))
    volume_1h = _f(data.get("volume_1h"))
    volume_24h = _f(data.get("volume_24h"))
    avg_hourly_24h = volume_24h / 24.0 if volume_24h else 0.0
    volume_acceleration = (volume_1h / avg_hourly_24h) if avg_hourly_24h > 0 else 1.0
    buys_1h = _f(data.get("txns_1h_buys"))
    sells_1h = _f(data.get("txns_1h_sells"))
    buy_sell_pressure = (buys_1h / (buys_1h + sells_1h)) if (buys_1h + sells_1h) > 0 else 0.5

    components = {
        "liquidity": min(100.0, (liquidity / 100_000.0) * 100),
        # Deeper capitulation reads as a more legitimate reset, up to a point.
        "drawdown_depth": min(100.0, drawdown_pct * 1.25),
        # Stronger bounce off the bottom = stronger reversal signal.
        "recovery_strength": min(100.0, recovery_pct * 2.0),
        "volume_resurgence": min(100.0, volume_acceleration * 30),
        "buy_sell_pressure": buy_sell_pressure * 100,
    }
    weights = {
        "liquidity": 0.15,
        "drawdown_depth": 0.20,
        "recovery_strength": 0.30,
        "volume_resurgence": 0.20,
        "buy_sell_pressure": 0.15,
    }
    total_weight = sum(weights.values()) or 1.0
    score = sum(components[k] * weights[k] for k in weights) / total_weight
    components["drawdown_pct"] = round(drawdown_pct, 1)
    components["recovery_pct"] = round(recovery_pct, 1)
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
    if expires.tzinfo is not None:
        expires = expires.replace(tzinfo=None)
    return expires > _now()


async def run_robinhood_discovery_cycle(bot=None) -> dict:
    stats = {
        "pairs_scanned": 0,
        "tokens_discovered": 0,
        "tokens_rejected": 0,
        "tokens_promoted": 0,
        "fresh_promoted": 0,
        "revival_promoted": 0,
        "signals_sent": 0,
        "signals_skipped_no_recipients": 0,
        "cooldown_skipped": 0,
    }

    # Real subscriber base (users table via pump_alert_subscriptions), the same
    # list every other alert type (scheduled broadcasts, milestone alerts, the
    # free Signal Engine) already delivers to -- plus any Robinhood-specific
    # extra channels from ROBINHOOD_ALERT_CHANNEL_IDS (optional, on top of the
    # real subscribers, not instead of them).
    try:
        recipients = await get_pump_subscribers()
    except Exception as e:
        logger.error(f"Robinhood discovery: subscriber fetch failed: {e}")
        recipients = []
    extra_channel_ids = load_channel_ids("ROBINHOOD_ALERT_CHANNEL_IDS", fallback_env_var=None)
    recipients = list(dict.fromkeys(list(recipients) + list(extra_channel_ids)))
    if not recipients:
        logger.warning(
            "Robinhood discovery: no subscribers and no ROBINHOOD_ALERT_CHANNEL_IDS/"
            "PUMP_ALERT_CHANNEL_IDS configured -- discovered tokens will be scored and "
            "stored but no alert will be sent to anyone this cycle."
        )

    try:
        candidates = await discover_candidates()
    except Exception as e:
        logger.error(f"Robinhood discovery: candidate discovery failed: {e}")
        return stats

    stats["pairs_scanned"] = len(candidates)

    fresh_pool: list = []
    revival_pool: list = []

    async with async_session() as session:
        for entry in candidates:
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

            # Always update cross-cycle history, whether or not this
            # candidate ends up promoted -- the REVIVAL lane's
            # drawdown/recovery detection needs continuous observation,
            # not just observations on cycles where something alerts.
            watch = await _get_or_update_token_watch(session, contract, data)

            age_hours = _pair_age_hours(data.get("pair_created"))
            is_fresh = age_hours is None or age_hours < ROBINHOOD_NEW_MAX_AGE_HOURS

            if is_fresh:
                bucket = "fresh"
                score, breakdown = score_fresh_potential(data)
                if score < ROBINHOOD_MIN_SCORE_TO_ALERT:
                    stats["tokens_rejected"] += 1
                    continue
            else:
                bucket = "revival"
                result = score_revival_potential(data, watch)
                if result is None:
                    # No genuine dump-then-recovery shape in our tracked
                    # history -- reject outright, don't fall back to
                    # generic scoring for old tokens.
                    stats["tokens_rejected"] += 1
                    continue
                score, breakdown = result
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

            pool_entry = {
                "contract": contract, "source": source, "data": data, "card": card,
                "score": score, "breakdown": breakdown, "existing": existing,
            }
            (fresh_pool if bucket == "fresh" else revival_pool).append(pool_entry)

        # Rank each lane by score and alert only the true top picks --
        # "top picks and hot" for fresh, the strongest confirmed
        # dump-then-recovery setups for revival -- never everything
        # that merely cleared the static score bar.
        fresh_pool.sort(key=lambda e: e["score"], reverse=True)
        revival_pool.sort(key=lambda e: e["score"], reverse=True)
        to_alert = (
            [("fresh", e) for e in fresh_pool[:ROBINHOOD_FRESH_TOP_N_PER_CYCLE]]
            + [("revival", e) for e in revival_pool[:ROBINHOOD_REVIVAL_TOP_N_PER_CYCLE]]
        )[:ROBINHOOD_MAX_ALERTS_PER_CYCLE]

        for bucket, item in to_alert:
            contract = item["contract"]
            source = item["source"]
            data = item["data"]
            card = item["card"]
            score = item["score"]
            breakdown = item["breakdown"]
            existing = item["existing"]

            stats["tokens_promoted"] += 1
            stats["fresh_promoted" if bucket == "fresh" else "revival_promoted"] += 1

            lane_label = (
                "🔥 Fresh Pick — hot & early" if bucket == "fresh"
                else "🔁 Second Wind — dumped, now recovering"
            )
            why_selected = [
                f"📡 Source: DexScreener ({'new pair' if source == 'dexscreener_new' else 'renewed activity'}) · {lane_label}",
                f"🧮 Discovery score: <b>{score}/100</b>",
                f"💧 Liquidity: ${_f(data.get('liquidity')):,.0f} | 📊 1h Volume: ${_f(data.get('volume_1h')):,.0f}",
            ]
            if bucket == "revival":
                why_selected.append(
                    f"📉 Drawdown <b>{breakdown.get('drawdown_pct')}%</b> from local high"
                    f" → 📈 Recovery <b>{breakdown.get('recovery_pct')}%</b> off the bottom"
                )
            why_selected.append(
                "📚 Standard educational on-chain and fundamental analysis only — not financial advice. DYOR."
            )

            # Captured BEFORE `existing` is possibly reassigned just
            # below -- this is the one moment (the very first time this
            # contract is ever alerted by this engine) a SignalToken row
            # + message ids should be created, mirroring how
            # domain.signals.pump_radar.pump_radar_loop does it for the
            # classic Pump.fun path. Every later re-alert on cooldown
            # keeps updating the RobinhoodDiscoverySignal row above as
            # before, but never touches SignalToken again -- the
            # lifecycle loop takes over price/milestone tracking for it
            # from here.
            is_first_alert = existing is None

            if existing is None:
                existing = RobinhoodDiscoverySignal(token_contract=contract)
                session.add(existing)
                existing.times_alerted = 0
                existing.first_alerted_at = _now()

            existing.pair_address = data.get("pool_address")
            existing.token_symbol = data.get("symbol")
            existing.token_name = data.get("name")
            existing.chain = ROBINHOOD_CHAIN_ID
            existing.discovery_source = f"{source}:{bucket}"
            existing.discovery_score = score
            existing.score_breakdown_json = json.dumps(breakdown)
            existing.reasons_json = json.dumps(why_selected)
            existing.snapshot_json = json.dumps(data)
            existing.status = "active"
            existing.times_alerted = (existing.times_alerted or 0) + 1
            existing.last_alerted_at = _now()
            existing.cooldown_expires_at = _now() + timedelta(hours=ROBINHOOD_COOLDOWN_HOURS)

            title = FRESH_TITLE if bucket == "fresh" else REVIVAL_TITLE
            msg_ids = {}
            if bot is not None and recipients:
                for chat_id in recipients:
                    try:
                        sent = await send_pump_card(
                            bot, chat_id, card,
                            title=title,
                            extra_block=why_selected,
                        )
                        if is_first_alert and sent and hasattr(sent, "message_id"):
                            msg_ids[str(chat_id)] = sent.message_id
                    except Exception as e:
                        logger.warning(f"Robinhood discovery: send failed for {contract} -> {chat_id}: {e}")
                stats["signals_sent"] += 1
            else:
                stats["signals_skipped_no_recipients"] += 1

            if is_first_alert:
                # Best-effort: a failure here must never break the
                # RobinhoodDiscoverySignal row/re-alert flow above, which
                # has already been committed to `existing` in this same
                # session regardless of what happens next.
                try:
                    created = await create_signal_from_candidate(
                        {"contract": contract, "data": data, "pump": {"score": score, "breakdown": breakdown}},
                        enforce_pumpfun_policy=False,
                        chain=ROBINHOOD_CHAIN_ID,
                    )
                    if created and msg_ids:
                        await update_signal_message_ids(contract, msg_ids)
                except Exception as e:
                    logger.error(f"Robinhood discovery: SignalToken creation failed for {contract}: {e}")

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
