"""
Wallet Intelligence / Scoring Engine.

Turns each wallet's PremiumWalletTrade history into a 0-100 reputation
score built from the factors requested for the Premium Intelligence
Engine: historical profitability, win rate, ROI, trading consistency,
risk management / drawdowns, trade quality, holding behaviour, and a
recency weighting so recent performance matters more than ancient
history while long-term performance still counts.

Design notes:
  - Round-trip matching (FIFO buy -> sell per wallet/token) turns the
    raw buy/sell event ledger into closed trades with a concrete ROI%
    and hold time, which is what the scoring below actually consumes.
  - A wallet with too few closed trades to be statistically meaningful
    gets a conservative provisional score instead of a wildly swingy
    one (see MIN_TRADES_FOR_FULL_CONFIDENCE).
  - Nothing here does network I/O — it only reads/writes the DB — so it
    stays cheap to run on a schedule independent of the monitor loop
    that actually detects new trades.
"""

import asyncio
import logging
import math
from datetime import datetime, timezone

from sqlalchemy import select, and_

from infra.db.session import async_session
from config.settings import (
    PREMIUM_WALLET_ELITE_SCORE,
    PREMIUM_WALLET_CORE_SCORE,
    PREMIUM_WALLET_WATCH_SCORE,
    PREMIUM_WALLET_MIN_ACTIVATE_SCORE,
    PREMIUM_WALLET_MIN_POSITION_USD,
    PREMIUM_WALLET_LOW_LIQUIDITY_USD,
    PREMIUM_WALLET_HIGH_LIQUIDITY_USD,
    PREMIUM_WALLET_SCAM_EXPOSURE_CAP_PCT,
)
from models.premium_wallet import PremiumWallet
from models.premium_wallet_trade import PremiumWalletTrade

logger = logging.getLogger("AlphaPulse.PremiumScorer")

MIN_TRADES_FOR_FULL_CONFIDENCE = 8

# Dynamic Reputation Engine: how much weight a freshly recomputed score
# gets versus the wallet's own prior score. Applied only to wallets that
# already had a real (non-provisional) score going in — brand-new
# candidates still get their first score at full strength so they don't
# take forever to reach an accurate reading. Keeps "historical
# performance influences the score more than short-term fluctuations"
# true without ever freezing a score that's genuinely trending.
SCORE_SMOOTHING_NEW_WEIGHT = 0.35


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


async def _match_round_trips(session, wallet_id: int) -> None:
    """
    FIFO-matches open "buy" rows against later "sell" rows of the same
    wallet+token, filling in roi_pct/hold_minutes/closed on the buy row
    consumed. Cheap O(n) pass per wallet, run right before scoring it.
    """
    res = await session.execute(
        select(PremiumWalletTrade)
        .where(PremiumWalletTrade.wallet_id == wallet_id)
        .order_by(PremiumWalletTrade.detected_at.asc())
    )
    trades = res.scalars().all()

    open_buys: dict[str, list[PremiumWalletTrade]] = {}

    for trade in trades:
        mint = trade.token_mint
        if trade.side == "buy":
            if trade.closed != "closed":
                open_buys.setdefault(mint, []).append(trade)
        elif trade.side == "sell":
            queue = open_buys.get(mint) or []
            if not queue:
                continue
            buy = queue.pop(0)

            buy_price = buy.price_usd_at_detection or 0.0
            sell_price = trade.price_usd_at_detection or 0.0

            if buy_price > 0 and sell_price > 0:
                buy.roi_pct = round(((sell_price - buy_price) / buy_price) * 100, 2)
            else:
                buy.roi_pct = None

            if buy.detected_at and trade.detected_at:
                delta = trade.detected_at - buy.detected_at
                buy.hold_minutes = round(delta.total_seconds() / 60.0, 1)

            buy.closed = "closed"

    await session.flush()


def _profitability_score(closed_trades: list[PremiumWalletTrade]) -> tuple[float, float, float, float, float]:
    """Returns (score, win_rate, avg_roi, best_roi, worst_roi)."""
    rois = [t.roi_pct for t in closed_trades if t.roi_pct is not None]
    if not rois:
        return 50.0, None, None, None, None

    wins = sum(1 for r in rois if r > 0)
    win_rate = wins / len(rois) * 100
    avg_roi = sum(rois) / len(rois)
    best_roi = max(rois)
    worst_roi = min(rois)

    # Blend win rate with average ROI so a wallet with a high win rate
    # of small gains and a wallet with fewer, larger wins can both score
    # well — pure win-rate would unfairly punish disciplined big-swing
    # traders who take controlled losses often.
    win_component = _clamp(win_rate)
    roi_component = _clamp(50 + avg_roi)  # avg_roi=0 -> 50, +50% avg -> 100
    score = _clamp(win_component * 0.5 + roi_component * 0.5)

    return score, round(win_rate, 1), round(avg_roi, 2), round(best_roi, 2), round(worst_roi, 2)


def _consistency_score(closed_trades: list[PremiumWalletTrade]) -> float:
    rois = [t.roi_pct for t in closed_trades if t.roi_pct is not None]
    if len(rois) < 2:
        return 50.0

    mean = sum(rois) / len(rois)
    variance = sum((r - mean) ** 2 for r in rois) / len(rois)
    stdev = math.sqrt(variance)

    # Lower volatility of returns => higher consistency. Scaled so a
    # ~30-point stdev (typical for meme-coin swing trades) lands near
    # the middle rather than tanking every wallet's score to zero.
    score = _clamp(100 - stdev)
    return score


def _risk_score(closed_trades: list[PremiumWalletTrade]) -> tuple[float, float]:
    """Returns (score, max_drawdown_pct) from the running cumulative ROI curve."""
    rois = [t.roi_pct for t in closed_trades if t.roi_pct is not None]
    if not rois:
        return 50.0, None

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rois:
        cumulative += r
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)

    score = _clamp(100 - max_dd)
    return score, round(max_dd, 2)


def _holding_behavior_score(closed_trades: list[PremiumWalletTrade]) -> tuple[float, float]:
    """Returns (score, avg_hold_minutes). Rewards deliberate holding over pure sniping/dumping."""
    holds = [t.hold_minutes for t in closed_trades if t.hold_minutes is not None]
    if not holds:
        return 50.0, None

    avg_hold = sum(holds) / len(holds)

    # Sub-2-minute average hold looks like sniping/bot behavior (still
    # allowed, just scored neutrally-low on "quality" rather than
    # penalized as pure luck); multi-hour+ average holds score highest
    # up to a point, then plateau (holding forever isn't inherently
    # better — this factor is about deliberateness, not diamond-handing).
    if avg_hold < 2:
        score = 40.0
    elif avg_hold < 15:
        score = 55.0
    elif avg_hold < 120:
        score = 75.0
    elif avg_hold < 1440:
        score = 90.0
    else:
        score = 80.0

    return score, round(avg_hold, 1)


def _position_size_score(closed_trades: list[PremiumWalletTrade]) -> tuple[float, float]:
    """
    Returns (score, avg_position_usd). Rewards meaningful, deliberately
    sized positions over dust-sized bets (too small to reflect real
    conviction) or wildly erratic sizing (gambling rather than a
    consistent strategy) — the "Average Position Size" qualification
    factor.
    """
    values = [t.value_usd_at_detection for t in closed_trades if t.value_usd_at_detection]
    if not values:
        return 50.0, None

    avg_value = sum(values) / len(values)

    if avg_value < PREMIUM_WALLET_MIN_POSITION_USD:
        size_component = 30.0
    elif avg_value < 500:
        size_component = 55.0
    elif avg_value < 5000:
        size_component = 80.0
    else:
        size_component = 90.0

    if len(values) >= 3:
        variance = sum((v - avg_value) ** 2 for v in values) / len(values)
        stdev = math.sqrt(variance)
        coeff_variation = (stdev / avg_value) if avg_value else 0.0
        consistency_component = _clamp(100 - coeff_variation * 40)
    else:
        consistency_component = 60.0

    score = _clamp(size_component * 0.6 + consistency_component * 0.4)
    return score, round(avg_value, 2)


def _liquidity_preference_score(closed_trades: list[PremiumWalletTrade]) -> tuple[float, float]:
    """
    Returns (score, avg_entry_liquidity_usd). Rewards wallets that
    consistently enter tokens with real liquidity behind them rather
    than thin, easily-rugged pools — the "Liquidity Preference"
    qualification factor.
    """
    liquidities = [t.entry_liquidity_usd for t in closed_trades if t.entry_liquidity_usd is not None]
    if not liquidities:
        return 50.0, None

    avg_liquidity = sum(liquidities) / len(liquidities)

    if avg_liquidity < PREMIUM_WALLET_LOW_LIQUIDITY_USD:
        score = 35.0
    elif avg_liquidity < PREMIUM_WALLET_HIGH_LIQUIDITY_USD:
        # Linear ramp between the low and high bands rather than a
        # hard step, so a wallet just above the low-liquidity floor
        # isn't scored identically to one right at the high band.
        span = PREMIUM_WALLET_HIGH_LIQUIDITY_USD - PREMIUM_WALLET_LOW_LIQUIDITY_USD
        progress = (avg_liquidity - PREMIUM_WALLET_LOW_LIQUIDITY_USD) / span if span > 0 else 1.0
        score = 55.0 + progress * 30.0
    else:
        score = 90.0

    return round(_clamp(score), 1), round(avg_liquidity, 2)


def _scam_exposure_score(all_buy_trades: list[PremiumWalletTrade]) -> tuple[float, float | None]:
    """
    Returns (score, exposure_pct). Penalizes wallets that repeatedly buy
    into tokens services/goplus.py flags as honeypots/scams/blacklisted
    — the "Exposure to Rug Pulls / Honeypots / Scam Tokens" qualification
    factor. Trades where the security check never resolved (is_flagged_
    risky is NULL) are excluded from the denominator entirely — an
    unknown result is never treated as evidence of risk.
    """
    checked = [t for t in all_buy_trades if t.is_flagged_risky is not None]
    if not checked:
        return 60.0, None  # no verified data yet -> mildly neutral, not punitive

    risky = sum(1 for t in checked if t.is_flagged_risky == "1")
    exposure_pct = risky / len(checked) * 100
    score = _clamp(100 - exposure_pct * 1.5)
    return round(score, 1), round(exposure_pct, 1)


def _activity_recency_weight(last_activity_at) -> float:
    """0.5-1.0 multiplier — recently active wallets are weighted higher
    since we care most about whether a wallet is STILL good, not just
    whether it used to be."""
    if not last_activity_at:
        return 0.6

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    days_since = max(0.0, (now - last_activity_at).total_seconds() / 86400)

    if days_since <= 3:
        return 1.0
    if days_since <= 14:
        return 0.9
    if days_since <= 30:
        return 0.75
    return 0.5


def _classify_wallet_archetypes(
    wallet: PremiumWallet,
    final_score: float,
    win_rate: float | None,
    avg_roi: float | None,
    avg_hold: float | None,
    trade_count: int,
) -> str:
    """
    Internal-only behavioural tagging (Blueprint: "Wallet Classification
    ... for internal use only"). Purely descriptive — does not feed into
    reputation_score itself, and is never rendered to Free or Premium
    users anywhere in bot/commands/. Multiple tags can apply; returns a
    comma-separated string for the classification column.
    """
    tags: list[str] = []

    if final_score >= PREMIUM_WALLET_ELITE_SCORE:
        tags.append("smart_money")

    if trade_count >= MIN_TRADES_FOR_FULL_CONFIDENCE:
        if avg_hold is not None:
            if avg_hold < 15:
                tags.append("scalper")
            elif avg_hold < 120:
                tags.append("momentum_trader")
            elif avg_hold < 1440:
                tags.append("swing_trader")
            else:
                tags.append("long_term_investor")

        if trade_count >= 25 and (avg_hold is None or avg_hold < 240):
            tags.append("meme_coin_specialist")

        if win_rate is not None and avg_roi is not None and win_rate >= 60 and avg_roi >= 40:
            tags.append("high_conviction_trader")

    if (wallet.wallet_value_usd or 0.0) >= 50_000:
        tags.append("whale_wallet")

    if wallet.source == "winning_signal_holder":
        tags.append("early_buyer")

    if not tags:
        tags.append("unclassified")

    return ",".join(dict.fromkeys(tags))  # de-dupe, preserve order


def _tier_for_score(score: float) -> str:
    if score >= PREMIUM_WALLET_ELITE_SCORE:
        return "elite"
    if score >= PREMIUM_WALLET_CORE_SCORE:
        return "core"
    if score >= PREMIUM_WALLET_WATCH_SCORE:
        return "watch"
    return "candidate"


async def score_wallet(session, wallet: PremiumWallet) -> None:
    """Recomputes every score component for one wallet and updates the row in place (no commit)."""
    prior_score = wallet.reputation_score
    had_prior_score = bool((wallet.trades_observed or 0) > 0 and prior_score is not None)

    await _match_round_trips(session, wallet.id)

    res = await session.execute(
        select(PremiumWalletTrade).where(
            and_(
                PremiumWalletTrade.wallet_id == wallet.id,
                PremiumWalletTrade.side == "buy",
                PremiumWalletTrade.closed == "closed",
            )
        )
    )
    closed_trades = res.scalars().all()

    res_all_buys = await session.execute(
        select(PremiumWalletTrade).where(
            and_(
                PremiumWalletTrade.wallet_id == wallet.id,
                PremiumWalletTrade.side == "buy",
            )
        )
    )
    all_buy_trades = res_all_buys.scalars().all()

    profit_score, win_rate, avg_roi, best_roi, worst_roi = _profitability_score(closed_trades)
    consistency = _consistency_score(closed_trades)
    risk, max_dd = _risk_score(closed_trades)
    holding_score, avg_hold = _holding_behavior_score(closed_trades)
    position_score, avg_position_usd = _position_size_score(closed_trades)
    liquidity_score, avg_entry_liquidity = _liquidity_preference_score(closed_trades)
    # Scam exposure looks at ALL buys (not just closed round-trips) —
    # a wallet buying into a flagged token is a red flag whether or not
    # it has sold yet.
    scam_score, scam_exposure_pct = _scam_exposure_score(all_buy_trades)

    recency_weight = _activity_recency_weight(wallet.last_activity_at)

    trade_count = len(closed_trades)
    # Confidence damps the score toward a neutral 50 when there isn't
    # enough closed-trade history yet, so a wallet with 1 lucky trade
    # doesn't jump straight to "elite".
    confidence = min(1.0, trade_count / MIN_TRADES_FOR_FULL_CONFIDENCE) if trade_count else 0.0

    raw_score = (
        profit_score * 0.28
        + consistency * 0.13
        + risk * 0.13
        + holding_score * 0.08
        + (win_rate or 50.0) * 0.08 / 100 * 100  # keep win_rate as an explicit component too
        + position_score * 0.08
        + liquidity_score * 0.08
        + scam_score * 0.14
    )
    # ^ simplifies to profit*0.28 + consistency*0.13 + risk*0.13 + holding*0.08
    #   + win_rate*0.08 + position_size*0.08 + liquidity_preference*0.08
    #   + scam_exposure*0.14

    blended = raw_score * confidence + 50.0 * (1 - confidence)
    final_score = _clamp(blended * (0.7 + 0.3 * recency_weight))

    if had_prior_score:
        # Dynamic Reputation Engine, volatility control: an established
        # wallet's score moves toward its freshly computed value rather
        # than jumping straight to it — long-term consistency still
        # dominates, one noisy cycle can't swing the tier boundary.
        final_score = _clamp(
            prior_score * (1 - SCORE_SMOOTHING_NEW_WEIGHT) + final_score * SCORE_SMOOTHING_NEW_WEIGHT
        )

    wallet.profitability_score = round(profit_score, 1)
    wallet.consistency_score = round(consistency, 1)
    wallet.risk_score = round(risk, 1)
    wallet.holding_behavior_score = round(holding_score, 1)
    wallet.activity_score = round(recency_weight * 100, 1)
    wallet.position_size_score = round(position_score, 1)
    wallet.liquidity_preference_score = round(liquidity_score, 1)
    wallet.scam_exposure_score = round(scam_score, 1)

    wallet.trades_observed = trade_count
    wallet.wins = sum(1 for t in closed_trades if (t.roi_pct or 0) > 0)
    wallet.losses = sum(1 for t in closed_trades if (t.roi_pct or 0) <= 0)
    wallet.win_rate = win_rate
    wallet.avg_roi_pct = avg_roi
    wallet.best_roi_pct = best_roi
    wallet.worst_roi_pct = worst_roi
    wallet.max_drawdown_pct = max_dd
    wallet.avg_hold_minutes = avg_hold
    wallet.avg_position_usd = avg_position_usd
    wallet.avg_entry_liquidity_usd = avg_entry_liquidity
    wallet.scam_exposure_pct = scam_exposure_pct

    wallet.reputation_score = round(final_score, 1)

    # Wallets still in "candidate" status get promoted to "active" the
    # moment they clear the activation bar — this is what makes new
    # elite-wallet detection fully automatic (no admin approval step).
    if wallet.status == "candidate" and final_score >= PREMIUM_WALLET_MIN_ACTIVATE_SCORE:
        wallet.status = "active"

    if wallet.status in ("active", "watch"):
        tier = _tier_for_score(final_score)
        # Verified rug/honeypot/scam exposure above the cap keeps a
        # wallet out of elite/core no matter how strong its other
        # numbers look — high-quality returns built partly on scam
        # tokens aren't the kind of "smart money" Premium should weight
        # heavily in consensus.
        if (
            scam_exposure_pct is not None
            and scam_exposure_pct >= PREMIUM_WALLET_SCAM_EXPOSURE_CAP_PCT
            and tier in ("elite", "core")
        ):
            tier = "watch"
        wallet.tier = tier

    wallet.classification = _classify_wallet_archetypes(
        wallet, final_score, win_rate, avg_roi, avg_hold, trade_count
    )


async def run_scoring_cycle(batch_limit: int = 500) -> int:
    """Rescans every non-removed wallet and recomputes its score. Returns count scored."""
    async with async_session() as session:
        res = await session.execute(
            select(PremiumWallet).where(PremiumWallet.status != "removed").limit(batch_limit)
        )
        wallets = res.scalars().all()

        for wallet in wallets:
            try:
                await score_wallet(session, wallet)
            except Exception as e:
                logger.warning(f"Scoring failed for wallet {wallet.wallet_address}: {e}")

        await session.commit()

    logger.info(f"🧮 Premium scoring cycle complete: {len(wallets)} wallet(s) rescored")
    return len(wallets)


async def premium_scoring_loop(interval_seconds: int) -> None:
    logger.info("📊 Premium Wallet Intelligence Scoring Engine active")
    while True:
        try:
            await run_scoring_cycle()
        except Exception as e:
            logger.error(f"Premium scoring cycle error: {e}")
        await asyncio.sleep(interval_seconds)
