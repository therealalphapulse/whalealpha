import asyncio
import html
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp
from sqlalchemy import select, text

from infra.db.session import async_session
from models.pump_subscription import PumpAlertSubscription
from models.pump_alerted_token import PumpAlertedToken
from models.signal_token import SignalToken
from providers.marketdata.dexscreener import get_token_card_info
from providers.marketdata.goplus import check_token_security
from domain.intelligence.holders import get_holder_analysis
from domain.intelligence.kol_tracker import get_matching_kol_holders
from domain.intelligence.whale_tracker import get_matching_tracked_whales
from domain.intelligence.funding_graph import get_funding_clusters, MIN_CLUSTER_SIZE as FUNDING_MIN_CLUSTER_SIZE
from domain.intelligence.deployer_history import get_deployer_launch_history
from domain.intelligence.lp_lock_checker import get_real_lp_lock_pct, assess_unverified_lock_risk
from domain.trading.real.jupiter_price import check_price_agreement
from domain.signals.signal_tracker import (
    create_signal_from_candidate,
    update_signal_message_ids,
    bump_signal_count_and_maybe_broadcast,
    mark_signal_alert_delivered,
    _milestones_crossed,
    MIN_24H_VOLUME_FOR_QUOTE_ALERT,
)
from domain.trading.paper.paper_engine import execute_paper_buy, get_autobuy_settings, get_filters_map
from domain.intelligence.narrative_scanner import classify_token
from domain.signals.scoring import (
    hard_reject_reasons,
    score_candidate,
    compute_confidence,
    passes_mc_liquidity_gate,
    effective_market_cap,
    SEND_CUTOFF,
    MIN_CONFIDENCE_SCORE,
    MIN_LIQUIDITY_USD,
    MIN_MARKET_CAP,
    MAX_MARKET_CAP,
    MC_LP_MIN_RATIO,
    DEV_HOLDING_WARN_PCT,
)
from domain.intelligence.risk_engine import (
    estimate_fake_volume_ratio,
    estimate_wash_trading_risk,
    estimate_sniper_wallet_risk,
    estimate_liquidity_lock_score,
    evaluate_verified_red_flags,
    LP_LOCK_WARN_BELOW,
    SERIAL_DEPLOYER_WARN_AT,
)
from domain.signals.quota import (
    record_qualifying_candidate,
    get_current_cutoff,
    has_quota_remaining,
    maybe_adjust_cutoff,
)
from domain.signals.qualification import (
    qualify_candidate,
    evaluate_signal_readiness,
    candidate_worth_full_enrichment,
)
from domain.signals.validation_queue import (
    schedule_revalidation,
    clear_revalidation,
    get_pending_contracts,
)
from config.settings import (
    SIGNAL_COOLDOWN_HOURS,
    DISCOVERY_MAX_AGE_HOURS,
    DISCOVERY_RECOVERY_MAX_AGE_HOURS,
)
# v4: was `from app_platform.keyboards.token_actions import token_actions_keyboard`
# — a domain-layer module must not import the presentation layer (audit
# finding, confirmed layering violation). Fixed via dependency injection;
# see domain/signals/keyboard_provider.py for the rationale.
from domain.signals.keyboard_provider import build_token_actions_keyboard

logger = logging.getLogger("AlphaPulse.PumpRadar")

# --------------------------------------------------
# QUALITY FILTERS (but Pump.fun ONLY)
# --------------------------------------------------
# Tightened vs the old "loose" thresholds — we'd rather send fewer, higher
# quality signals than flood users with weak/risky candidates.
#
# MIN_LIQUIDITY_USD, MIN_MARKET_CAP, MAX_MARKET_CAP, and MC_LP_MIN_RATIO
# used to be defined here AND duplicated (with different, disagreeing
# numbers) in scoring.hard_reject_reasons() and in a separate
# _passes_mc_lp_safety_filter() applied after the whole expensive
# pipeline had already run. That triple-definition is exactly why
# tightening or loosening "MC/liquidity quality" in one place didn't
# reliably change behavior — see scoring.passes_mc_liquidity_gate()'s
# docstring. They now live in exactly one place (scoring.py, imported
# at the top of this file) and are used directly by name here.
MIN_VOLUME_1H = 1500
# Fallback/legacy floor only — the real gate is now the weighted
# conviction score in services/conviction_scorer.py (hard 80/65 cutoffs,
# Blueprint 1.3), dynamically tightened/loosened between 70-80 by
# services/quota_governor.py to hold the 100-150/day target (Blueprint
# 3.4). This constant just keeps scan_for_pump_candidates' signature
# backward compatible for any external caller passing min_score=.
MIN_SCORE_TO_ALERT = 68
MIN_HOLDERS = 40
MAX_SELL_PRESSURE_RATIO = 0.25  # reject if buys make up less than this share of 1h txns
# Additional strict, deterministic noise/manipulation filter for the
# pre-enrichment quality gate: a token can clear MIN_VOLUME_1H in
# dollar terms from a literal handful of large trades (a couple of
# wash trades, or a single whale round-trip) while still being
# functionally dead/untraded. Requiring a minimum absolute 1h
# transaction count on top of (not instead of) the existing dollar
# volume floor catches that case without touching MIN_VOLUME_1H,
# MAX_SELL_PRESSURE_RATIO, or any other existing threshold.
MIN_TX_1H = 4
MAX_ALERTS_PER_CYCLE = 2
MAX_CANDIDATES_TO_SCAN = 30
# Cap on how many previously-rejected (data-unavailable) contracts get
# folded back into a single scan cycle for re-validation, on top of that
# cycle's fresh candidates. See services/validation_queue.py — without a
# cap this backlog only grows, and every cycle re-adds ALL of it, so
# per-cycle demand on shared downstream APIs (Helius) compounds cycle
# over cycle with no way to ever drain.
MAX_REVALIDATIONS_PER_CYCLE = 10

# NOTE: token age is no longer used as a hard cutoff/filter. Signals are
# selected on narrative strength + momentum quality (volume growth, price
# action, buy pressure), regardless of how old the token is. A token that
# launched weeks ago with strong narrative + momentum scores the same as a
# freshly launched one with the same profile.
AGE_DISPLAY_ONLY_HOURS = 48

# --- MC vs LP quality gate ---
# Market Cap must clear MIN_MARKET_CAP/MAX_MARKET_CAP and be at least
# MC_LP_MIN_RATIO times liquidity (rejects tight/near-equal MC-vs-LP
# spreads, e.g. MC $61.4K vs Liq $48.3K, ~1.27x). MIN_MARKET_CAP was
# raised from an old value of 150000 — that ceiling was far too tight
# for the actual pool of candidates coming out of GeckoTerminal's
# new/trending pools (real low-cap Pump.fun plays routinely sit in the
# low hundreds-of-thousands to low millions before they're
# "established"); with the old value the scanner rejected essentially
# every candidate on market cap alone. All of this is now evaluated in
# exactly one place — scoring.passes_mc_liquidity_gate() (imported
# above) — called early in analyze_candidate(), before any paid/rate-
# limited lookup. See that function's docstring for why this used to
# be three separate, disagreeing checks scattered across this file.

# --- Newly launched tokens with a verifiable project profile ---
# A brand new token can't realistically have MIN_HOLDERS yet just by virtue
# of being new — that's a timing problem, not a quality problem. Instead of
# blocking all new tokens, allow a lower holder floor ONLY when the token is
# genuinely fresh AND shows at least one credible, publicly verifiable
# project signal (website, X/Twitter, or Telegram, as returned by
# DexScreener token info). This keeps the honeypot/blacklist/holder-
# concentration/liquidity/volume checks fully intact — it only relaxes the
# "not enough holders yet" gate, and only for tokens that look like a real
# project rather than an anonymous copy-paste rug.
NEW_TOKEN_AGE_HOURS_THRESHOLD = 6
MIN_HOLDERS_NEW_WITH_PROFILE = 15
# --------------------------------------------------

# --- Duplicate signal prevention / re-arm rules ---
# A token normally gets exactly one signal alert, ever — never a second
# alert on ordinary price noise, and not later either, UNLESS one of two
# structural conditions is met (Signal Lifecycle, Blueprint Problem 4):
#
#   A. It makes a completely new ATH beyond the highest level that was
#      in effect the last time it was actually alerted.
#
#   OR
#
#   B. It has genuinely corrected off its own ATH (a real pullback, not
#      a fixed retrace ratio applied uniformly to every token) and is
#      being reconsidered from scratch — in which case it is NOT
#      re-alerted directly. It's only allowed back into
#      scan_for_pump_candidates, where this file's full existing
#      pipeline (hard_reject_reasons, score_candidate's conviction
#      scoring, and the live quota_governor dynamic cutoff — see
#      analyze_candidate above) has to independently rate it a genuine
#      high-confidence setup all over again, with fresh volume/momentum/
#      security data, exactly as it would for a brand-new contract. That
#      full re-scoring pass IS the "existing AlphaPulse AI Intelligence"
#      decision Blueprint Problem 4B calls for — there is deliberately
#      no second, separate percentage-based gate here anymore (no fixed
#      "must have hit +X%" / "must have retraced to <=Y%" thresholds).
#
# See _signal_rearm_eligible() below.
# --------------------------------------------------


def _is_high_risk(sec: dict | None) -> str | None:
    """
    Strict security gate. Returns a short reason string if the token should
    be rejected outright, or None if it passes. This runs before scoring so
    risky tokens never reach the alert pipeline regardless of how strong
    their narrative/momentum numbers look.
    """
    if not sec:
        return None

    def flag(value) -> bool:
        return str(value).lower() in ("1", "true", "yes", "enabled")

    if flag(sec.get("is_honeypot")):
        return "Honeypot"
    if flag(sec.get("cannot_sell_all")):
        return "Cannot sell"
    if flag(sec.get("cannot_buy")):
        return "Cannot buy"
    if flag(sec.get("is_blacklisted")):
        return "Blacklisted"
    if flag(sec.get("hidden_owner")):
        return "Hidden owner"

    top_holder = sec.get("top_holder_percent")
    if isinstance(top_holder, (int, float)) and top_holder >= 35:
        return "Holder concentration too high"

    top_10 = sec.get("top_10_holder_percent")
    if isinstance(top_10, (int, float)) and top_10 >= 70:
        return "Top 10 holder concentration too high"

    return None


def _load_channel_ids() -> list:
    """Parses PUMP_ALERT_CHANNEL_IDS: comma-separated numeric chat IDs
    (e.g. -1001234567890) and/or public channel @usernames (e.g.
    @therealalphapulse). Usernames are passed through as-is — aiogram's
    send_message/send_photo accept either a numeric chat_id or an
    "@username" string for public channels. Existing numeric-ID behavior
    is unchanged.
    """
    raw = os.getenv("PUMP_ALERT_CHANNEL_IDS", "").strip()
    if not raw:
        return []
    ids = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith("@"):
            ids.append(item)
            continue
        try:
            ids.append(int(item))
        except ValueError:
            pass
    return ids


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return default


def _fmt_usd(value, default: str = "N/A") -> str:
    """Formats a raw numeric/string market value as compact, comma'd USD.
    Returns the default placeholder (never a fake number) if the value
    can't be parsed."""
    try:
        if value is None or value == "N/A":
            return default
        num = float(str(value).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return default
    if num >= 1_000_000:
        return f"${num / 1_000_000:.2f}M"
    if num >= 1_000:
        return f"${num / 1_000:.1f}K"
    return f"${num:.2f}"


def _fmt_pct(value, default: str = "N/A") -> str:
    if value is None:
        return default
    try:
        return f"{float(value):.1f}%"
    except (ValueError, TypeError):
        return default


def _is_pump_fun(contract: str) -> bool:
    return bool(contract and contract.lower().endswith("pump"))


def _has_verifiable_profile(data: dict) -> bool:
    """
    True if the token exposes at least one credible, publicly verifiable
    project signal — a website, X/Twitter, or Telegram link, as surfaced by
    DexScreener's token info. Anonymous copy-paste rugs typically ship with
    none of these filled in, so this is used as a light-weight legitimacy
    signal for tokens too new to have built up a holder base yet.
    """
    return bool(
        (data.get("website_url") or "").strip()
        or (data.get("twitter_url") or "").strip()
        or (data.get("telegram_url") or "").strip()
    )


def format_age(pair_created_ms):
    try:
        created = datetime.fromtimestamp(int(pair_created_ms) / 1000, tz=timezone.utc)
        delta = datetime.now(timezone.utc) - created
        days = delta.days
        if days >= 365:
            return f"{days // 365}y"
        elif days >= 30:
            return f"{days // 30}mo"
        elif days >= 1:
            return f"{days}d"
        elif delta.seconds // 3600 >= 1:
            return f"{delta.seconds // 3600}h"
        else:
            return f"{max(delta.seconds // 60, 1)}m"
    except Exception:
        return "N/A"


def _age_hours(pair_created_ms) -> float:
    try:
        created = datetime.fromtimestamp(int(pair_created_ms) / 1000, tz=timezone.utc)
        delta = datetime.now(timezone.utc) - created
        return delta.total_seconds() / 3600
    except Exception:
        return 9999.0


# --------------------------------------------------
# TWO-LANE DISCOVERY (GeckoTerminal, pump.fun-verified)
# --------------------------------------------------
# Restores the original, pre-Aug-16 GeckoTerminal-primary discovery
# thesis (GeckoTerminal's new_pools + trending_pools feeds as the
# candidate SOURCE, pump.fun mint-suffix verification mandatory) that
# was later relegated to a secondary/merged-in source behind a
# DexScreener-first adapter (domain/signals/_radar_discovery_adapter.py,
# now retired — see that module's docstring). This restores it as a
# clean, self-contained two-lane system instead of the single flat
# candidate list the original thesis produced:
#
#   Fresh Momentum
#       Newly created pools (age <= DISCOVERY_MAX_AGE_HOURS) ranked by
#       genuine early momentum: volume relative to liquidity, short-term
#       price action, and buy-side pressure.
#
#   Post-Launch Recovery / Consolidation
#       Older pools (DISCOVERY_MAX_AGE_HOURS < age <=
#       DISCOVERY_RECOVERY_MAX_AGE_HOURS) ranked by signs of a bounce
#       off a dip or a stable-price/sustained-volume consolidation —
#       "still alive and getting renewed attention", not raw momentum.
#
# Both lanes draw from the SAME merged, deduplicated GeckoTerminal
# candidate pool — lane assignment is decided purely by each
# candidate's own age + price/volume/activity numbers, NEVER by which
# GT feed (new_pools vs trending_pools) it happened to come from.
# Concretely: appearing in GeckoTerminal's trending_pools feed makes a
# mint a CANDIDATE, nothing more — it has zero effect on which lane a
# mint lands in, its rank score within that lane, or whether it passes
# the cheap gate below. "Trending" is a candidate source, never a buy
# signal.
#
# pump.fun-origin verification (_is_pump_fun — mint address suffix) is
# mandatory and non-negotiable for every candidate in either lane,
# exactly as in the original thesis.
#
# Every candidate is also required to clear _pre_enrichment_quality_gate
# (below) — the SAME cheap, IO-free liquidity/MC/activity gate the
# downstream pipeline (scan_for_pump_candidates) applies again anyway —
# BEFORE it is ever handed to that pipeline, so nothing here weakens or
# duplicates a disagreeing threshold; it only rejects earlier, before a
# single enrichment call (DexScreener/GoPlus/holders) is spent on a
# candidate that would have been rejected anyway. This is single-source-
# of-truth reuse, not a second set of numbers: passes_mc_liquidity_gate()
# / MIN_VOLUME_1H / MIN_TX_1H / MAX_SELL_PRESSURE_RATIO are the exact
# same constants already used elsewhere in this file.
#
# Discovery decides only which mints are worth the existing enrichment/
# scoring pipeline's attention, and in which order — never how strong a
# candidate is. Raw score, the dynamic cutoff, qualification, alerts,
# and trading are entirely untouched by anything in this section.


@dataclass
class _GeckoPoolCandidate:
    """One GeckoTerminal pool's cheap, already-fetched attributes for a
    verified pump.fun mint — everything the cheap gate and both lane
    ranking functions need, with zero additional network calls."""
    mint: str
    liquidity: float
    market_cap: float
    fdv: float
    volume_1h: float
    price_change_1h: float
    price_change_24h: float
    buys_1h: float
    sells_1h: float
    age_hours: float

    def as_gate_data(self) -> dict:
        """Reshaped to the same field names every other cheap gate in
        this module already expects (data['liquidity'] / ['market_cap']
        / ['fdv'] / ['volume_1h'] / ['txns_1h_buys'] / ['txns_1h_sells']),
        so _pre_enrichment_quality_gate() and scoring.passes_mc_liquidity_gate()
        can be reused verbatim on GeckoTerminal data instead of a second,
        possibly-disagreeing implementation of the same thresholds."""
        return {
            "contract": self.mint,
            "liquidity": self.liquidity,
            "market_cap": self.market_cap,
            "fdv": self.fdv,
            "volume_1h": self.volume_1h,
            "txns_1h_buys": self.buys_1h,
            "txns_1h_sells": self.sells_1h,
        }


def _gt_pool_age_hours(created_at) -> float | None:
    """Hours since a GeckoTerminal pool's own `pool_created_at` timestamp
    (ISO 8601, e.g. '2026-08-25T10:15:23Z'). Returns None when missing or
    unparseable — an unknown age must never silently default into either
    discovery lane."""
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 3600.0)
    except (ValueError, TypeError):
        return None


def _extract_gecko_pool_candidate(mint: str, pool: dict) -> "_GeckoPoolCandidate | None":
    """Cheap, IO-free extraction of everything needed downstream from one
    already-fetched GeckoTerminal pool record. Returns None only when the
    pool has no usable creation timestamp — age is required for lane
    assignment and is never guessed."""
    attrs = pool.get("attributes") or {}
    age_hours = _gt_pool_age_hours(attrs.get("pool_created_at"))
    if age_hours is None:
        return None

    volume = attrs.get("volume_usd") or {}
    price_change = attrs.get("price_change_percentage") or {}
    txns_h1 = (attrs.get("transactions") or {}).get("h1") or {}

    return _GeckoPoolCandidate(
        mint=mint,
        liquidity=_to_float(attrs.get("reserve_in_usd")),
        market_cap=_to_float(attrs.get("market_cap_usd")),
        fdv=_to_float(attrs.get("fdv_usd")),
        volume_1h=_to_float(volume.get("h1")),
        price_change_1h=_to_float(price_change.get("h1")),
        price_change_24h=_to_float(price_change.get("h24")),
        buys_1h=_to_float(txns_h1.get("buys")),
        sells_1h=_to_float(txns_h1.get("sells")),
        age_hours=age_hours,
    )


def _fresh_momentum_score(c: "_GeckoPoolCandidate") -> float:
    """Ordering-ONLY heuristic for the Fresh Momentum lane. Never rejects
    anything and is never consulted by the cheap gate, scoring, or
    qualification — mirrors the same philosophy as _pre_filter_priority()
    below. Rewards genuine early momentum: volume relative to liquidity,
    positive short-term price action, buy-side pressure, and recency."""
    score = 0.0
    if c.liquidity > 0:
        score += min(c.volume_1h / c.liquidity, 2.0) * 25.0
    score += max(min(c.price_change_1h, 50.0), -20.0) * 0.8
    total_tx = c.buys_1h + c.sells_1h
    if total_tx > 0:
        score += ((c.buys_1h / total_tx) - 0.5) * 40.0
    score += min(total_tx, 40.0) * 0.3
    if c.age_hours <= 1:
        score += 20.0
    elif c.age_hours <= 3:
        score += 12.0
    elif c.age_hours <= 6:
        score += 6.0
    if c.liquidity >= 20000:
        score += 10.0
    elif c.liquidity >= 10000:
        score += 6.0
    return score


def _recovery_strength_score(c: "_GeckoPoolCandidate") -> float:
    """Ordering-ONLY heuristic for the Post-Launch Recovery/Consolidation
    lane. Rewards renewed strength after a token's initial launch window
    — a bounce off a longer-term dip, or a stable price alongside real
    sustained volume — rather than raw "still pumping" momentum, which is
    what the Fresh Momentum lane already rewards. Being present in
    GeckoTerminal's trending_pools feed has NO effect on this score —
    only the pool's own liquidity, volume, price-action, and activity
    numbers do (trending is a candidate source, never a ranking signal).
    """
    score = 0.0
    if c.liquidity > 0:
        score += min(c.volume_1h / c.liquidity, 1.5) * 18.0

    if c.price_change_24h < 0 and c.price_change_1h > 0:
        # Bounce: recovering off a longer-term dip.
        score += min(c.price_change_1h, 30.0)
    elif -5.0 <= c.price_change_1h <= 5.0 and c.volume_1h > 0:
        # Consolidation: roughly flat short-term move with real volume.
        score += 12.0

    if c.price_change_1h < -10.0 and c.price_change_24h < -10.0:
        # Still actively falling — not a recovery/consolidation case.
        score -= 20.0

    total_tx = c.buys_1h + c.sells_1h
    if total_tx > 0:
        score += ((c.buys_1h / total_tx) - 0.5) * 30.0
    score += min(total_tx, 40.0) * 0.25

    if c.age_hours >= DISCOVERY_MAX_AGE_HOURS:
        # Older, still-alive tokens have already proven some staying
        # power — tapering reward, capped so extreme age can't dominate.
        score += min((c.age_hours - DISCOVERY_MAX_AGE_HOURS) / 24.0, 3.0) * 4.0

    if c.liquidity >= 20000:
        score += 8.0
    elif c.liquidity >= 10000:
        score += 5.0
    return score


async def _fetch_gecko_pools(url: str) -> list[tuple[str, dict]]:
    """One raw GeckoTerminal pools page (new_pools or trending_pools),
    with each pool's base-token mint address already resolved from the
    response's `included` section. Returns [] on any failure — discovery
    must never raise, only ever produce fewer candidates."""
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"Accept": "application/json"}) as resp:
                if resp.status != 200:
                    logger.warning(f"GeckoTerminal HTTP {resp.status} for {url}")
                    return []
                payload = await resp.json()
    except Exception as e:
        logger.warning(f"GeckoTerminal fetch error ({url}): {e}")
        return []

    token_map: dict[str, str] = {}
    for item in payload.get("included", []):
        if item.get("type") != "token":
            continue
        attrs = item.get("attributes", {})
        addr = attrs.get("address", "")
        token_id = item.get("id", "")
        if not addr and "_" in token_id:
            addr = token_id.split("_")[-1]
        if addr:
            token_map[token_id] = addr

    pools: list[tuple[str, dict]] = []
    for pool in payload.get("data", []):
        base_id = pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
        mint = token_map.get(base_id)
        if mint:
            pools.append((mint, pool))
    return pools


async def fetch_pump_fun_launches(limit: int = 30) -> list[str]:
    """
    Two-lane GeckoTerminal discovery (pump.fun-origin mandatory).

    GeckoTerminal's new_pools and trending_pools feeds are the candidate
    SOURCE. Every mint returned by this function has already:

      1. Been verified as a genuine pump.fun mint (_is_pump_fun) — no
         exceptions.
      2. Cleared _pre_enrichment_quality_gate() on GeckoTerminal's own
         cheap pool attributes (liquidity / MC / 1h volume / 1h tx count
         / sell-pressure) — the SAME gate the downstream pipeline applies
         again anyway, so this only moves rejection earlier.
      3. Been assigned to exactly one lane by age + its own price/volume/
         activity numbers — Fresh Momentum or Post-Launch Recovery/
         Consolidation — never by which GT feed it came from (see module
         section docstring above).
      4. Been deduplicated by mint address across both feeds.

    Both lanes are ranked independently (_fresh_momentum_score /
    _recovery_strength_score) and interleaved into the returned list so
    neither lane can crowd out the other purely because its raw feed was
    larger this cycle. Every candidate returned is still independently
    re-fetched, gated, enriched, and scored by the unmodified downstream
    pipeline (analyze_candidate / scoring.score_candidate) — discovery
    decides only which mints are worth that pipeline's attention, never
    how strong they are.
    """
    raw_pools: list[tuple[str, dict]] = []
    for url in (
        "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?include=base_token&page=1",
        "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?include=base_token&page=1",
    ):
        raw_pools.extend(await _fetch_gecko_pools(url))

    fresh_lane: dict[str, float] = {}
    recovery_lane: dict[str, float] = {}
    seen_mints: set[str] = set()
    fetched = len(raw_pools)
    pumpfun_verified = 0
    gate_rejected = 0

    for mint, pool in raw_pools:
        if mint in seen_mints:
            continue
        seen_mints.add(mint)

        # Mandatory, non-negotiable pump.fun-origin verification.
        if not _is_pump_fun(mint):
            continue
        pumpfun_verified += 1

        candidate = _extract_gecko_pool_candidate(mint, pool)
        if candidate is None:
            continue

        gate_ok, gate_reason = _pre_enrichment_quality_gate(mint, candidate.as_gate_data())
        if not gate_ok:
            gate_rejected += 1
            logger.debug(f"[Discovery] {mint[:8]} rejected pre-enrichment: {gate_reason}")
            continue

        if candidate.age_hours <= DISCOVERY_MAX_AGE_HOURS:
            fresh_lane[mint] = _fresh_momentum_score(candidate)
        elif candidate.age_hours <= DISCOVERY_RECOVERY_MAX_AGE_HOURS:
            recovery_lane[mint] = _recovery_strength_score(candidate)
        # else: older than both lane windows — gate-passed but outside
        # the discovery-relevant age range for either lane this cycle.

    fresh_ranked = [m for m, _ in sorted(fresh_lane.items(), key=lambda kv: kv[1], reverse=True)]
    recovery_ranked = [m for m, _ in sorted(recovery_lane.items(), key=lambda kv: kv[1], reverse=True)]

    # Interleave the two lanes so neither one can crowd out the other
    # purely because its raw feed happened to be larger this cycle.
    merged: list[str] = []
    i = j = 0
    while len(merged) < limit and (i < len(fresh_ranked) or j < len(recovery_ranked)):
        if i < len(fresh_ranked):
            merged.append(fresh_ranked[i])
            i += 1
            if len(merged) >= limit:
                break
        if j < len(recovery_ranked):
            merged.append(recovery_ranked[j])
            j += 1

    logger.info(
        "Discovery (GeckoTerminal, two-lane): fetched=%d pumpfun_verified=%d "
        "gate_rejected=%d fresh=%d recovery=%d selected=%d",
        fetched, pumpfun_verified, gate_rejected,
        len(fresh_ranked), len(recovery_ranked), len(merged),
    )
    return merged[:limit]

def calculate_pump_fun_score(data: dict, sec: dict | None, holders: int | None) -> dict:
    score = 25
    reasons = []

    liq = _to_float(data.get("liquidity"))
    vol_1h = _to_float(data.get("volume_1h"))
    mc = _to_float(data.get("market_cap")) or _to_float(data.get("fdv"))
    price_change_1h = _to_float(data.get("price_change_1h"))
    price_change_24h = _to_float(data.get("price_change_24h"))

    buys = _to_float(data.get("txns_1h_buys"))
    sells = _to_float(data.get("txns_1h_sells"))
    total_tx = buys + sells
    buy_ratio = buys / total_tx if total_tx > 0 else 0.0

    if mc < MIN_MARKET_CAP or mc > MAX_MARKET_CAP:
        return {"score": 0, "verdict": "REJECT", "reasons": ["MC out of range"]}

    score += 10
    reasons.append("Low-cap")

    # --- Narrative strength (trending theme/sector relevance) ---
    name = data.get("name", "") or ""
    symbol = data.get("symbol", "") or ""
    narratives = [n for n in classify_token(name, symbol) if n != "OTHER"]
    if narratives:
        score += 15
        reasons.append(f"{'/'.join(narratives[:2])} narrative")

    # --- Liquidity strength ---
    if liq >= 20000:
        score += 16
        reasons.append("Strong liq")
    elif liq >= 10000:
        score += 12
    elif liq >= MIN_LIQUIDITY_USD:
        score += 8

    # --- Momentum: volume growth relative to liquidity ---
    if liq > 0:
        ratio = vol_1h / liq
        if ratio >= 1.0:
            score += 18
            reasons.append("Explosive volume")
        elif ratio >= 0.5:
            score += 13
            reasons.append("Strong volume")
        elif ratio >= 0.2:
            score += 7

    # --- Momentum: price action ---
    if price_change_1h >= 15:
        score += 10
        reasons.append("Strong 1h momentum")
    elif price_change_1h >= 5:
        score += 5
    elif price_change_1h <= -15:
        score -= 8
        reasons.append("Sharp 1h drop")

    if price_change_24h >= 30:
        score += 4

    # --- Momentum: buy/sell pressure ---
    if total_tx >= 6:
        if buy_ratio >= 0.65:
            score += 12
            reasons.append("Buy pressure")
        elif buy_ratio >= 0.55:
            score += 7
        elif buy_ratio < 0.35:
            score -= 10
            reasons.append("Heavy sell pressure")
        elif buy_ratio < 0.45:
            score -= 5

    # --- Holder base / community activity ---
    if holders and holders >= 150:
        score += 8
        reasons.append("Strong holder base")
    elif holders and holders >= 60:
        score += 5

    # --- New launch with a verifiable project profile ---
    if (
        _age_hours(data.get("pair_created")) <= NEW_TOKEN_AGE_HOURS_THRESHOLD
        and _has_verifiable_profile(data)
    ):
        score += 5
        reasons.append("New + verified profile")

    # --- Security ---
    if sec:
        if str(sec.get("is_honeypot")) == "1":
            return {"score": 0, "verdict": "REJECT", "reasons": ["HONEYPOT"]}
        if str(sec.get("mintable")) == "0":
            score += 6
        if str(sec.get("freezable")) == "0":
            score += 5

    score = max(0, min(score, 100))

    if score >= 80:
        verdict = "🔥 HIGH POTENTIAL"
    elif score >= 65:
        verdict = "🟢 STRONG"
    elif score >= 55:
        verdict = "🟡 WATCH"
    else:
        verdict = "⚪ WEAK"

    return {"score": score, "verdict": verdict, "reasons": reasons[:4]}


@dataclass
class PipelineStats:
    """Per-scan-cycle funnel counters. Purely observational — never
    read by any gate, scorer, or trading decision, so adding/reading
    these counters cannot change which candidates qualify or how they
    score. `seen` backs the in-cycle deduplication check below.
    """
    discovered: int = 0
    pre_filtered: int = 0
    rejected_by_quality_gate: int = 0
    enriched: int = 0
    scored: int = 0
    qualified: int = 0
    seen: set = field(default_factory=set)


def _pre_enrichment_quality_gate(contract: str, data: dict) -> tuple[bool, str | None]:
    """
    Cheap, IO-free quality gate applied immediately after discovery —
    before GoPlus security and, most importantly, before the expensive
    Vybe/Birdeye/Solana Tracker holder-enrichment chain
    (domain.intelligence.holders.get_holder_analysis) ever runs for a
    candidate. This bundles the SAME checks/constants that already
    gated this exact point in the pipeline (scoring.passes_mc_liquidity_gate
    for MC/liquidity/LP-ratio sanity, MIN_VOLUME_1H, MAX_SELL_PRESSURE_RATIO)
    into one named, independently-testable checkpoint, plus an explicit
    "obviously dead" check (zero 1h transactions AND zero 1h volume).
    Nothing about WHICH candidates pass changed — every threshold here
    is the same constant already used elsewhere in this file/module;
    this only makes the checkpoint explicit and cheap to log/measure.
    The one addition beyond what already existed is MIN_TX_1H (below):
    a minimum absolute 1h transaction-count floor, on top of the
    existing dollar volume floor, so a candidate can't clear
    MIN_VOLUME_1H on a literal handful of large/wash trades while
    otherwise showing no genuine trading activity. This is a strict
    tightening only — it cannot cause anything that passed before to
    fail unless it was already right at the edge of "no real activity".
    Never touches the Score engine, scoring formulas, or qualification
    cutoffs — those still run, unchanged, further down the pipeline.
    """
    vol = _to_float(data.get("volume_1h"))
    buys = _to_float(data.get("txns_1h_buys"))
    sells = _to_float(data.get("txns_1h_sells"))
    total_tx = buys + sells

    # Obvious dead/low-quality token: no trading activity at all in the
    # last hour. A strict subset of what MIN_VOLUME_1H already rejects
    # in practice — added for a clearer, purpose-specific log reason.
    if total_tx <= 0 and vol <= 0:
        return False, "dead token: no 1h transactions or volume"

    # Mandatory MC / Liquidity / LP-ratio quality gate — single source
    # of truth (scoring.passes_mc_liquidity_gate), unchanged.
    mc_liq_ok, mc_liq_reason = passes_mc_liquidity_gate(data)
    if not mc_liq_ok:
        return False, mc_liq_reason

    if vol < MIN_VOLUME_1H:
        return False, f"low vol {vol}"

    if total_tx < MIN_TX_1H:
        return False, f"too few 1h txns to trust reported volume ({int(total_tx)})"

    if total_tx >= 6 and (buys / total_tx) < MAX_SELL_PRESSURE_RATIO:
        return False, f"heavy sell pressure ({buys}/{total_tx})"

    return True, None


def _pre_filter_priority(data: dict) -> float:
    """
    Ordering-ONLY heuristic for candidates that have already passed
    _pre_enrichment_quality_gate(). It never rejects anything and is
    never consulted by scoring.score_candidate(), qualify_candidate(),
    the dynamic cutoff, or trading logic — its only effect is deciding
    which gate-survivors get first claim on this cycle's limited
    enrichment budget (Vybe/Birdeye/Solana Tracker) in
    scan_for_pump_candidates(), so that when max_results is hit and
    the scan stops, the strongest-looking candidates were the ones
    actually evaluated, not just whichever appeared first in the raw
    discovery feed. Built only from fields already present in the
    cheap DexScreener `data` payload the gate itself uses — no extra
    provider calls, no manufactured signals. Higher = higher priority.
    """
    priority = 0.0

    # Young PumpSwap tokens: migrated off the bonding curve onto a
    # real AMM pool, so liquidity/MC numbers are structurally more
    # meaningful than a pre-migration Pump.fun curve quote.
    if str(data.get("dex") or "").strip().lower() == "pumpswap":
        priority += 30.0

    # Healthy (young) age — tapering reward, mirrors the discovery
    # adapter's own "fresher is stronger" rationale.
    age_hours = _age_hours(data.get("pair_created"))
    if age_hours <= 1:
        priority += 25.0
    elif age_hours <= 6:
        priority += 18.0
    elif age_hours <= 24:
        priority += 10.0
    elif age_hours <= 48:
        priority += 4.0

    # Liquidity/MC structure: reward liquidity depth relative to
    # market cap — further above the MC_LP_MIN_RATIO floor than the
    # bare minimum required to pass the gate at all.
    liq = _to_float(data.get("liquidity"))
    mc = effective_market_cap(data)
    if liq > 0 and mc > 0:
        priority += min(liq / mc, 1.0) * 20.0

    # Absolute liquidity strength — same tiers calculate_pump_fun_score
    # already uses elsewhere, reused here rather than redefined.
    if liq >= 20000:
        priority += 12.0
    elif liq >= 10000:
        priority += 8.0
    elif liq >= MIN_LIQUIDITY_USD:
        priority += 4.0

    # Meaningful volume/activity: volume relative to liquidity, plus
    # raw 1h transaction count as an activity-depth signal.
    vol_1h = _to_float(data.get("volume_1h"))
    if liq > 0:
        priority += min(vol_1h / liq, 2.0) * 6.0

    buys = _to_float(data.get("txns_1h_buys"))
    sells = _to_float(data.get("txns_1h_sells"))
    priority += min(buys + sells, 40.0) * 0.3

    # Verifiable, publicly-checkable project profile.
    if _has_verifiable_profile(data):
        priority += 15.0

    return priority


async def analyze_candidate(contract: str, stats: "PipelineStats | None" = None) -> dict | None:
    if not _is_pump_fun(contract):
        return None

    # --- Cheap deduplication gate (no provider API calls) ---
    # Runs before the DexScreener re-fetch and every paid/rate-limited
    # lookup below, so a candidate that can never become a NEW signal
    # this cycle doesn't burn provider budget getting to that
    # conclusion. Two independent checks:
    #  1. In-cycle dedup — the same contract appearing twice in one
    #     scan cycle (e.g. merged discovery sources plus the
    #     revalidation queue) is only ever analyzed once.
    #  2. was_already_alerted() — the exact same duplicate-signal /
    #     cooldown / re-arm check the alert-delivery loop already
    #     performs right before sending. Checking it here too just
    #     means it also runs before enrichment, not only right before
    #     send; the original check there is left in place, unchanged,
    #     as a final safety net.
    if stats is not None:
        if contract in stats.seen:
            return None
        stats.seen.add(contract)

    if await was_already_alerted(contract):
        logger.info(
            f"Rejected {contract[:8]}: already alerted / cooldown active — "
            "skipped before enrichment"
        )
        return None

    data = await get_token_card_info(contract)
    if not data:
        return None

    # Cheap early quality gate — MC/liquidity/LP-ratio sanity, volume
    # floor, sell-pressure, and obvious-dead-token checks, all IO-free
    # given `data` already fetched above. See
    # _pre_enrichment_quality_gate() docstring: this is the single
    # checkpoint standing between discovery and every paid/rate-limited
    # lookup that follows (GoPlus, Helius, Raydium, and especially the
    # Vybe/Birdeye/Solana Tracker holder-enrichment chain).
    gate_ok, gate_reason = _pre_enrichment_quality_gate(contract, data)
    if not gate_ok:
        logger.info(f"Rejected {contract[:8]}: {gate_reason}")
        return None

    if stats is not None:
        stats.pre_filtered += 1

    sec = await check_token_security(contract)

    # --- Production Validation Policy: Fail Closed ---
    # Honeypot detection, mint/freeze authority, blacklist, and ownership
    # checks all come from this single GoPlus call. If it fails, times
    # out, gets rate-limited, or otherwise can't be completed, that is an
    # UNKNOWN verification state, not a passing one — it must never be
    # treated as "no risk found". Reject this cycle and automatically
    # retry once GoPlus is reachable again, instead of scoring/sending a
    # token whose security status was never actually confirmed.
    if sec is None:
        logger.warning(
            f"Rejected {contract[:8]}: security data unavailable (GoPlus) "
            "— queued for re-validation"
        )
        schedule_revalidation(contract, "goplus_security_unavailable")
        return None

    # Full holder distribution (top1/top10/top25 + bundle cluster + dev
    # wallet share) via a single Helius call. A separate get_holder_count()
    # call used to run right before this and hit the SAME Helius endpoint
    # again for the same underlying data — doubling rate-limit pressure on
    # every candidate and being the real cause of holder count / bundle
    # info intermittently showing "N/A". `holders` is now derived from
    # this one call instead of a redundant second fetch.
    # analyze_candidate() already gated on _is_pump_fun(contract) above, so
    # every candidate reaching this point is a Pump.fun token.
    dev_address = sec.get("creator_address") or None
    # This is the expensive Vybe/Birdeye/Solana Tracker holder-enrichment
    # chain (see domain.intelligence.holders.get_holder_analysis) — the
    # provider call the pre-enrichment gate above exists to protect.
    # Only candidates that survived discovery, dedup, and the cheap
    # quality gate reach this point.
    if stats is not None:
        stats.enriched += 1
    holder_analysis = await get_holder_analysis(contract, dev_address=dev_address, is_pump_fun=True)

    # --- Production Validation Policy: Holder Retrieval Is Non-Blocking ---
    # Holder data (count, concentration, bundle clustering) is valuable
    # SECURITY evidence when it's available, but a Helius/Solana Tracker/
    # Birdeye outage must never be able to suppress a real signal. This
    # used to hard-reject the whole candidate and queue a revalidation
    # whenever holder_analysis came back None, which meant a provider
    # outage silently blocked alerts instead of just narrowing what the
    # bot can verify. Genuine hard security/risk gates — honeypot,
    # mint/freeze authority, GoPlus concentration data — are untouched
    # and still enforced via hard_reject_reasons(); this policy only
    # concerns the case where holder-specific data (total holders, dev
    # holding %, bundle clustering) is unavailable.
    #
    # When holder_analysis is None (all eligible RPC providers
    # failed/exhausted — see services/holders.py) or the RPC succeeded
    # but returned zero accounts for a brand-new Pump.fun mint
    # (holder_analysis_status="unavailable_early_token"), we continue
    # with a neutral/unknown holder profile instead of rejecting.
    # Holder-derived hard rejects (concentration, bundles, dev holding)
    # simply have nothing to flag in that case rather than being
    # treated as passing — they are skipped, not defeated.
    if holder_analysis is None:
        logger.warning(
            f"[HolderDiag] {contract[:8]}: holder analysis unavailable this cycle "
            "(all eligible RPC providers failed/exhausted) — continuing with a "
            "neutral holder profile per non-blocking holder policy; holder-derived "
            "hard rejects have nothing to flag until data returns"
        )
        holder_analysis = {
            "holder_analysis_status": "unavailable_provider_degraded",
            "total_holders": None,
            "top_holder_pct": None,
            "dev_holding_pct": None,
            "bundle_wallet_count": 0,
            "bundle_pct": None,
            "top_holder_addresses": [],
        }

    if holder_analysis.get("holder_analysis_status") in (
        "unavailable_early_token",
        "unavailable_provider_degraded",
    ):
        # Either a brand-new Pump.fun mint that doesn't expose holder
        # accounts yet (known coverage gap, not a provider failure) or a
        # genuine provider outage handled just above. Either way: don't
        # hard-reject, continue with liquidity/market cap/volume/buy-sell
        # pressure/security checks. Holder-derived hard rejects
        # (concentration, bundles, dev holding) simply have nothing to
        # flag rather than being treated as passing.
        logger.info(
            f"[HolderDiag] {contract[:8]}: holder data unavailable "
            f"({holder_analysis.get('holder_analysis_status')}) — continuing with "
            "partial scoring, holder checks skipped rather than blocking"
        )

    holders = holder_analysis.get("total_holders")

    # Security (GoPlus) data was fetched successfully this pass, so this
    # contract no longer needs an automatic retry for that reason — any
    # rejection from this point on is a genuine, verified finding, not a
    # missing-data condition. Holder-data unavailability is no longer a
    # revalidation trigger at all (see non-blocking holder policy above).
    clear_revalidation(contract)

    risk_reason = _is_high_risk(sec)
    if risk_reason:
        logger.info(f"Rejected {contract[:8]}: risk={risk_reason}")
        return None

    # Blueprint 2.2 hard-reject gates — non-negotiable regardless of
    # volume/hype, evaluated before the token is scored at all.
    reject_reasons = hard_reject_reasons(data, sec, holder_analysis, contract)
    if reject_reasons:
        logger.info(f"Rejected {contract[:8]}: hard gate={reject_reasons}")
        return None

    min_holders_required = MIN_HOLDERS
    is_new_with_profile = (
        _age_hours(data.get("pair_created")) <= NEW_TOKEN_AGE_HOURS_THRESHOLD
        and _has_verifiable_profile(data)
    )
    if is_new_with_profile:
        min_holders_required = MIN_HOLDERS_NEW_WITH_PROFILE

    if holders is not None and holders < min_holders_required:
        logger.info(f"Rejected {contract[:8]}: low holders {holders}")
        return None

    # Smart-money / tracked-whale cross-reference — moved ahead of
    # scoring (Signal Intelligence upgrade) so a genuine, verified Smart
    # Money or whale position can actually influence the conviction
    # score via _smart_money_whale_bonus() in conviction_scorer.py,
    # instead of being fetched only to decorate the card after the send
    # decision was already made. Still only runs for candidates that
    # already survived every hard gate and the holder-count gate above,
    # so this doesn't add a DB round-trip to obviously-rejected junk.
    holder_addresses = holder_analysis.get("top_holder_addresses") or []
    try:
        smart_money = await get_matching_kol_holders(holder_addresses)
    except Exception as e:
        logger.warning(f"Smart-money lookup failed (non-fatal): {e}")
        smart_money = []
    try:
        whale_holders = await get_matching_tracked_whales(holder_addresses)
    except Exception as e:
        logger.warning(f"Whale lookup failed (non-fatal): {e}")
        whale_holders = []

    # Weighted 0-100 conviction score (Blueprint 1.3) — replaces the old
    # binary calculate_pump_fun_score gate. Ranking is only meaningful
    # among tokens that already survived every gate above.
    pump = score_candidate(
        data, sec, holder_analysis, holders, contract,
        smart_money=smart_money, whale_holders=whale_holders,
    )
    if stats is not None:
        stats.scored += 1

    logger.info(
        f"Candidate {data.get('symbol', contract[:6])}: "
        f"base={pump['base_score']} final={pump['final_score']} tier={pump['tier']}"
    )

    # Signal Quality & Alert Qualification Upgrade: the accept/reject
    # decision is now the single combined evaluate_signal_readiness()
    # call at the end of this function (score + confidence/evidence
    # together), not two independent serial hard gates. This early check
    # is only a cheap resource-protection pre-filter -- see
    # qualification.candidate_worth_full_enrichment() docstring -- wide
    # enough that a genuine near-miss on score still reaches the combined
    # decision below, where strong verified evidence may legitimately
    # compensate for it.
    try:
        dynamic_cutoff = await get_current_cutoff()
    except Exception as e:
        logger.warning(f"Quota governor cutoff read failed, using default: {e}")
        dynamic_cutoff = SEND_CUTOFF

    if not candidate_worth_full_enrichment(pump["final_score"], dynamic_cutoff):
        logger.info(
            f"Qualification: symbol={data.get('symbol', contract[:6])} "
            f"raw_score={pump['base_score']:.1f} "
            f"adjusted_score={pump['final_score']:.1f} "
            f"dynamic_cutoff={dynamic_cutoff:.1f} "
            f"qualification=REJECTED "
            f"qualification_reason=BELOW_DYNAMIC_CUTOFF "
            f"quota_state=NOT_ELIGIBLE"
        )
        return None

    # Quota governor's rolling-average "how much qualifying supply
    # existed today" signal is tracked at the same point and against the
    # same score-only bar as before this upgrade (final_score >=
    # dynamic_cutoff, strictly) -- unchanged on purpose, since retuning
    # quota.py's own calibration is outside this change's scope. A
    # candidate that only reaches EMERGING_WATCH via the compensation
    # path below (i.e. genuinely below dynamic_cutoff on score) does not
    # count here, exactly as it wouldn't have counted before this upgrade.
    decision = qualify_candidate(
        pump["base_score"],
        dynamic_cutoff,
        final_score=pump["final_score"],
    )
    if decision.qualified:
        logger.info(
            f"Qualification: symbol={data.get('symbol', contract[:6])} "
            f"raw_score={pump['base_score']:.1f} "
            f"adjusted_score={pump['final_score']:.1f} "
            f"dynamic_cutoff={dynamic_cutoff:.1f} "
            f"qualification=QUALIFIED "
            f"qualification_reason={decision.reason} "
            f"quota_state=ELIGIBLE"
        )
        try:
            await record_qualifying_candidate()
        except Exception as e:
            logger.warning(f"Quota governor logging failed (non-fatal): {e}")
    else:
        logger.info(
            f"Qualification: symbol={data.get('symbol', contract[:6])} "
            f"raw_score={pump['base_score']:.1f} "
            f"adjusted_score={pump['final_score']:.1f} "
            f"dynamic_cutoff={dynamic_cutoff:.1f} "
            f"qualification=NEAR_MISS "
            f"qualification_reason=WITHIN_NEAR_MISS_BAND "
            f"quota_state=PENDING_EVIDENCE"
        )

    # Real (non-estimated) LP burn/lock check against Raydium's own pool
    # records — see services/lp_lock_checker.py. Feeds evaluate_verified_
    # red_flags() below in addition to being shown on the card.
    try:
        real_lp_lock_pct = await get_real_lp_lock_pct(data.get("pool_address"))
    except Exception as e:
        logger.warning(f"LP lock check failed (non-fatal): {e}")
        real_lp_lock_pct = None

    # NOTE: liquidity-lock rejection for a CONFIRMED value intentionally
    # happens in exactly one place — risk_engine.evaluate_verified_red_
    # flags() further down this function, which only rejects when
    # real_lp_lock_pct is a confirmed, verified value below
    # LP_LOCK_REJECT_BELOW. A second, stricter copy of that gate used to
    # live here and rejected every unverifiable (None) pool outright —
    # since that provider's lookup fails to verify most real candidates,
    # it was silently rejecting almost everything. That hard reject-on-
    # None was removed as a duplicate (Problem 3).
    #
    # Locked Liquidity Policy (production hardening): "never reject
    # solely for being unverifiable" is not the same thing as "give an
    # unverifiable pool a completely free pass". When the lock status
    # genuinely can't be confirmed, require corroborating evidence of
    # safety from data already fetched above (real liquidity depth,
    # holder concentration, bundle concentration) before proceeding —
    # see services/lp_lock_checker.assess_unverified_lock_risk(). A
    # confirmed lock value (real_lp_lock_pct is not None) skips this
    # entirely and is judged solely by evaluate_verified_red_flags(),
    # unchanged.
    if real_lp_lock_pct is None:
        lock_safe_enough, lock_risk_reason = assess_unverified_lock_risk(data, holder_analysis)
        if not lock_safe_enough:
            logger.info(f"Rejected {contract[:8]}: {lock_risk_reason}")
            return None

    # Real funding-graph cluster detection — replaces the balance-size
    # bundle heuristic with an actual shared-funder fact. Feeds
    # evaluate_verified_red_flags() below in addition to being shown on
    # the card.
    try:
        funding_clusters = await get_funding_clusters(holder_addresses)
    except Exception as e:
        logger.warning(f"Funding-graph lookup failed (non-fatal): {e}")
        funding_clusters = {"clusters": [], "largest_cluster_size": 0, "traced": 0}

    # Deployer prior-launch history. Feeds evaluate_verified_red_flags()
    # below in addition to being shown on the card — see
    # services/deployer_history.py for the serial-launch reject threshold.
    try:
        deployer_history = await get_deployer_launch_history(dev_address, contract)
    except Exception as e:
        logger.warning(f"Deployer history lookup failed (non-fatal): {e}")
        deployer_history = None

    # Independent second price source (Jupiter) — catches a stale/wrong
    # DexScreener pair before it ever reaches a card. `agrees` is only
    # ever True/False when BOTH prices were actually fetched; see
    # services/jupiter_price.py. Feeds evaluate_verified_red_flags()
    # below in addition to being shown on the card.
    try:
        price_check = await check_price_agreement(contract, data.get("price"))
    except Exception as e:
        logger.warning(f"Jupiter price cross-check failed (non-fatal): {e}")
        price_check = {"jupiter_price": None, "mismatch_pct": None, "agrees": None}

    # Final verified-data gate (risk_engine.evaluate_verified_red_flags).
    # Runs after every other check specifically because it needs the real
    # (non-estimated) LP lock / funding-cluster / deployer-history / price
    # cross-check results above — confirmed danger here now blocks the
    # send outright, the same way hard_reject_reasons() already does for
    # estimated signals earlier in this function.
    verified = evaluate_verified_red_flags(
        real_lp_lock_pct=real_lp_lock_pct,
        funding_clusters=funding_clusters,
        deployer_history=deployer_history,
        price_check=price_check,
        funding_cluster_min_size=FUNDING_MIN_CLUSTER_SIZE,
    )
    if verified["reject"]:
        logger.info(f"Rejected {contract[:8]}: verified red flag(s)={verified['reasons']}")
        return None

    # Confidence scoring (Signal Intelligence upgrade) — a SEPARATE gate
    # from the score/cutoff check above. "Multiple conditions must
    # agree" (avoid random tokens / prioritize quality over quantity):
    # a token can pass every hard gate and clear the dynamic score
    # cutoff on liquidity/momentum/holder math alone and still lack real
    # corroboration if most of the independent verification checks below
    # came back unknown rather than positively confirmed. See
    # conviction_scorer.compute_confidence() docstring for the exact
    # True/False/None semantics — None (genuinely unverifiable) is
    # excluded from the count entirely rather than penalized, consistent
    # with the "unknown -> don't penalize" convention used everywhere
    # else in this pipeline.
    confirmations = {
        "smart_money_or_whale_present": True if (smart_money or whale_holders) else None,
        "lp_lock_verified_safe": (
            real_lp_lock_pct >= LP_LOCK_WARN_BELOW if real_lp_lock_pct is not None else None
        ),
        "deployer_history_clean": (
            (deployer_history.get("prior_launches", 0) or 0) < SERIAL_DEPLOYER_WARN_AT
            if deployer_history is not None else None
        ),
        "price_agrees": price_check.get("agrees"),
        "no_funding_cluster": (
            (funding_clusters.get("largest_cluster_size", 0) or 0) < FUNDING_MIN_CLUSTER_SIZE
            if funding_clusters is not None else None
        ),
        # Signal Engine re-evaluation: two new checks sourced from the
        # now-reliable holder intelligence layer (domain/intelligence/
        # holders.py's multi-RPC-failover get_holder_analysis), which
        # weren't independently represented in the confidence gate before
        # even though scoring.py already consumes them. "unavailable_
        # early_token" stays None here (genuinely unknown, not a fail) —
        # same unknown-doesn't-penalize convention as every other check.
        "holder_data_verified": (
            True if holder_analysis.get("holder_analysis_status") == "ok" else None
        ),
        "dev_holding_confirmed_low": (
            holder_analysis.get("dev_holding_pct") < DEV_HOLDING_WARN_PCT
            if holder_analysis.get("dev_holding_pct") is not None else None
        ),
    }
    confidence = compute_confidence(pump["final_score"], confirmations)

    # Combined signal-readiness decision (Signal Quality & Alert
    # Qualification Upgrade) -- replaces the old two-independent-hard-
    # gates stack (qualify_candidate() alone, then compute_confidence()
    # ["meets_bar"] alone as a second, unrelated veto) with one decision
    # that lets a candidate genuinely strong on ONE of {score, confidence}
    # compensate for a small, bounded shortfall on the OTHER. See
    # qualification.evaluate_signal_readiness() docstring for the exact
    # rule; hard risk gates (hard_reject_reasons / evaluate_verified_red_
    # flags / passes_mc_liquidity_gate, all already evaluated above) are
    # untouched and remain an absolute veto regardless of this decision.
    readiness = evaluate_signal_readiness(
        pump["final_score"], dynamic_cutoff, confidence, MIN_CONFIDENCE_SCORE,
    )
    if not readiness.ready:
        logger.info(
            f"Rejected {contract[:8]}: signal readiness — {readiness.reason} "
            f"(final_score={pump['final_score']:.1f} dynamic_cutoff={dynamic_cutoff:.1f} "
            f"confidence_score={confidence['confidence_score']} "
            f"confirmed={confidence['confirmed_count']}/{confidence['checked_count']})"
        )
        return None

    logger.info(
        f"Signal readiness: symbol={data.get('symbol', contract[:6])} "
        f"tier={readiness.tier} reason={readiness.reason} "
        f"final_score={pump['final_score']:.1f} dynamic_cutoff={dynamic_cutoff:.1f} "
        f"confidence_score={confidence['confidence_score']} "
        f"confirmed={confidence['confirmed_count']}/{confidence['checked_count']}"
    )
    if stats is not None:
        stats.qualified += 1

    return {
        "contract": contract,
        "data": data,
        "pump": pump,
        "security_data": sec,
        "holder_count": holders,
        "holder_analysis": holder_analysis,
        "smart_money": smart_money,
        "whale_holders": whale_holders,
        "real_lp_lock_pct": real_lp_lock_pct,
        "funding_clusters": funding_clusters,
        "deployer_history": deployer_history,
        "price_check": price_check,
        "verified_warnings": verified["warnings"],
        "confidence": confidence,
    }


async def scan_for_pump_candidates(min_score: int = MIN_SCORE_TO_ALERT, max_results: int = 5):
    mints = await fetch_pump_fun_launches(limit=MAX_CANDIDATES_TO_SCAN)

    # Re-validation (Production Validation Policy): fold in any contracts
    # that were rejected in a previous cycle solely because a mandatory
    # check's data was temporarily unavailable (API failure, rate limit,
    # timeout, RPC error). Each one gets a complete, fresh pass through
    # analyze_candidate() below — never a shortcut — so it only ever
    # becomes a signal once every mandatory validation actually succeeds.
    pending_contracts = get_pending_contracts(limit=MAX_REVALIDATIONS_PER_CYCLE)
    if pending_contracts:
        already_queued = set(mints)
        for pending_contract in pending_contracts:
            if pending_contract not in already_queued:
                mints.append(pending_contract)
                already_queued.add(pending_contract)

    # Purely observational funnel counters for this cycle — see
    # PipelineStats docstring. `discovered` is the full candidate set
    # handed to this scan (fresh discovery + folded-in revalidations);
    # every stage below is a strict narrowing of it, never a widening,
    # and nothing here changes which candidates qualify or how they score.
    stats = PipelineStats(discovered=len(mints))

    # --- Strict, deterministic pre-enrichment quality gate pass ---
    # Applied here, BEFORE any candidate reaches analyze_candidate()'s
    # security/holder-enrichment chain, using the exact same
    # _pre_enrichment_quality_gate() function analyze_candidate() also
    # applies internally (single source of truth — the gate logic is
    # invoked one step earlier, never duplicated or reimplemented).
    # This buys two things, without loosening a single existing
    # threshold and without touching the Score engine, raw scores, the
    # dynamic cutoff, qualification logic, trading logic, or provider
    # priority:
    #   1. An explicit, top-level rejected_by_quality_gate count and
    #      log line, distinct from discovered/enriched/scored/
    #      qualified, for real production funnel visibility.
    #   2. Gate survivors are re-ORDERED (never re-scored, never
    #      re-thresholded) by _pre_filter_priority() so that within
    #      this cycle's max_results budget, the candidates with the
    #      strongest structural signals — young PumpSwap tokens,
    #      healthy liquidity/MC structure, meaningful volume/activity,
    #      a verifiable profile — get first claim on the expensive
    #      Vybe/Birdeye/Solana Tracker enrichment chain, instead of
    #      whichever contract happened to appear first in the raw
    #      discovery feed. analyze_candidate() still independently
    #      re-applies the identical gate before enriching (defensive,
    #      idempotent, cheap — DexScreener's own 15s cache absorbs the
    #      repeat lookup), so this is never a shortcut around it.
    ordered_mints = []
    for mint in mints:
        data = await get_token_card_info(mint)
        if not data:
            # No market data at all — analyze_candidate() would also
            # bail here (`if not data: return None`) without touching
            # any funnel counter, so this isn't a quality-gate
            # rejection either; it just can't be prioritized.
            ordered_mints.append((mint, 0.0))
            continue

        gate_ok, gate_reason = _pre_enrichment_quality_gate(mint, data)
        if not gate_ok:
            stats.rejected_by_quality_gate += 1
            logger.info(f"[QualityGate] Rejected {mint[:8]} pre-enrichment: {gate_reason}")
            continue

        ordered_mints.append((mint, _pre_filter_priority(data)))

    ordered_mints.sort(key=lambda pair: pair[1], reverse=True)
    if len(ordered_mints) >= 2:
        # Observational only — proves the quality-first re-ordering is
        # actually taking effect, without changing which candidates are
        # analyzed or how. Never read by any gate, scorer, or trading
        # decision.
        order_preview = ", ".join(f"{m[:8]}({p:.1f})" for m, p in ordered_mints[:5])
        logger.info(f"[QualityGate] Enrichment order this cycle (priority desc): {order_preview}")

    results = []

    for mint, _priority in ordered_mints:
        candidate = await analyze_candidate(mint, stats=stats)
        if candidate and candidate["pump"]["score"] >= min_score:
            # MC/Liquidity/LP-ratio quality is already enforced inside
            # analyze_candidate() (scoring.passes_mc_liquidity_gate,
            # single source of truth) — a candidate reaching here has
            # already cleared it, so no second check is needed.
            results.append(candidate)

        await asyncio.sleep(0.8)

        if len(results) >= max_results:
            break

    results.sort(key=lambda x: x["pump"]["score"], reverse=True)
    logger.info(
        "Pipeline funnel: discovered=%d rejected_by_quality_gate=%d pre_filtered=%d "
        "enriched=%d scored=%d qualified=%d returned=%d (min_score=%s, max_results=%s)",
        stats.discovered, stats.rejected_by_quality_gate, stats.pre_filtered,
        stats.enriched, stats.scored, stats.qualified, len(results), min_score, max_results,
    )
    logger.info(f"Qualified Pump.fun candidates this cycle: {len(results)}")
    return results


def _narrative_tag(d: dict) -> str:
    tags = [n for n in classify_token(d.get("name", "") or "", d.get("symbol", "") or "") if n != "OTHER"]
    return "/".join(tags[:2]) if tags else ""


def _security_line(sec: dict) -> str:
    """One-line, verified-only security summary. Never guesses a status
    that GoPlus didn't actually return."""
    if not sec:
        return "🔒 Security: <b>N/A (no scanner data)</b>"

    def flag(key: str, label: str) -> str:
        val = str(sec.get(key)).lower()
        if val in ("1", "true", "yes", "enabled"):
            return f"🔴 {label}"
        if val in ("0", "false", "no", "disabled"):
            return f"✅ {label} clean"
        return f"⚪ {label} unknown"

    parts = [flag("mintable", "Mint"), flag("freezable", "Freeze")]
    return "🔒 " + " · ".join(parts)


def _build_pump_card_text(candidate: dict) -> str:
    d = candidate["data"]
    pump = candidate.get("pump") or {}
    sec = candidate.get("security_data") or {}
    holder_analysis = candidate.get("holder_analysis") or {}
    contract = candidate["contract"]

    score = pump.get("score", 0)
    tier = pump.get("tier") or pump.get("verdict", "N/A")
    reasons = pump.get("reasons") or []
    reasons_line = " · ".join(reasons) if reasons else "Cleared all quality gates"
    breakdown = pump.get("breakdown") or {}

    buys = _to_float(d.get("txns_1h_buys"))
    sells = _to_float(d.get("txns_1h_sells"))
    total_tx = buys + sells
    buy_pct = (buys / total_tx * 100) if total_tx > 0 else 0.0
    sell_pct = (100 - buy_pct) if total_tx > 0 else 0.0

    # Real holder count, in priority order — each source is only used if
    # the one before it is genuinely missing (None), never to overwrite a
    # real value:
    #   1. Helius-based total_holders (get_holder_analysis) — most precise
    #   2. Helius get_holder_count() (candidate["holder_count"]) — same
    #      source, used when the full analysis wasn't available but the
    #      plain count still was
    #   3. GoPlus's own holder_count — fetched unconditionally as part of
    #      security checks for every candidate, so it's there even when
    #      HELIUS_API_KEY is missing/rate-limited. Previously never read,
    #      which meant a real number sitting in `sec` was shown as "N/A".
    holders_count = holder_analysis.get("total_holders")
    if holders_count is None:
        holders_count = candidate.get("holder_count")
    if holders_count is None:
        gp_holders = sec.get("holder_count")
        holders_count = gp_holders if gp_holders not in (None, "") else None
    holders_count = holders_count if holders_count is not None else "N/A"

    bundle_wallets = holder_analysis.get("bundle_wallet_count") or 0
    bundle_pct = holder_analysis.get("bundle_pct")
    dev_pct = holder_analysis.get("dev_holding_pct")

    smart_money = candidate.get("smart_money") or []
    whale_holders = candidate.get("whale_holders") or []

    def _smart_money_line() -> str:
        if not smart_money:
            return "🧠 Smart Money: <b>None detected among top holders</b>"
        names = [html.escape(w.get("handle") or w.get("label") or "Unnamed") for w in smart_money[:3]]
        more = f" +{len(smart_money) - 3} more" if len(smart_money) > 3 else ""
        return f"🧠 Smart Money: <b>{len(smart_money)} wallet(s)</b> — {', '.join(names)}{more}"

    def _whale_line() -> str:
        if not whale_holders:
            return "🐋 Tracked Whales: <b>None detected among top holders</b>"
        names = [html.escape(", ".join(w.get("labels", []))) for w in whale_holders[:3]]
        more = f" +{len(whale_holders) - 3} more" if len(whale_holders) > 3 else ""
        return f"🐋 Tracked Whales: <b>{len(whale_holders)} wallet(s)</b> — {', '.join(names)}{more}"

    def _real_lp_lock_line():
        """Verified LP burn/lock %, straight from Raydium — distinct from
        the behavioral estimate shown in the Liq line above. Omitted
        entirely (not shown as 0%) when the pool isn't on Raydium or the
        check couldn't be completed."""
        pct = candidate.get("real_lp_lock_pct")
        if pct is None:
            return None
        return f"🔥 LP Burned/Locked (verified): <b>{pct:.0f}%</b>"

    def _funding_cluster_line():
        fc = candidate.get("funding_clusters") or {}
        if not fc.get("traced"):
            return None  # couldn't verify — omit, don't imply "clean"
        largest = fc.get("largest_cluster_size", 0)
        if largest >= FUNDING_MIN_CLUSTER_SIZE:
            return f"🚨 Funding Cluster: <b>{largest} top holders share a funding wallet</b>"
        return f"🔗 Funding Clusters: <b>None found</b> ({fc['traced']} wallets traced)"

    def _deployer_history_line():
        dh = candidate.get("deployer_history")
        if dh is None:
            return None  # couldn't verify — omit, don't imply a clean 0
        prior = dh.get("prior_launches", 0)
        if prior > 0:
            return f"⚠️ Deployer History: <b>{prior} prior token launch(es) found</b>"
        return "✅ Deployer History: <b>No prior launches found</b> (verified)"

    def _price_mismatch_line():
        """Only ever shown when Jupiter and DexScreener BOTH returned a
        price and they genuinely disagree — silence otherwise, this is a
        data-integrity warning, not a routine field."""
        pc = candidate.get("price_check") or {}
        if pc.get("agrees") is False:
            return (
                f"🚨 Price Data Mismatch: DexScreener vs Jupiter differ by "
                f"<b>{pc.get('mismatch_pct')}%</b> — verify manually before acting"
            )
        return None

    def _trend_arrow(pct) -> str:
        try:
            v = float(pct)
        except (TypeError, ValueError):
            return "⚪"
        if v > 0:
            return "🟢▲"
        if v < 0:
            return "🔴▼"
        return "⚪"

    def _momentum_line() -> str:
        points = []
        for label, key in (("5m", "price_change_5m"), ("1h", "price_change_1h"),
                            ("6h", "price_change_6h"), ("24h", "price_change_24h")):
            raw = d.get(key)
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            points.append(f"{label} {_trend_arrow(v)}{v:+.0f}%")
        if not points:
            return None
        return "📊 Momentum: " + " · ".join(points)

    def _volume_growth_line() -> str:
        """Real, computed growth ratio: current 1h volume vs. this
        token's own 24h average hourly volume. >1.0x means volume is
        accelerating; never a fabricated 'growth score'."""
        vol_1h = _to_float(d.get("volume_1h"))
        vol_24h = _to_float(d.get("volume_24h"))
        if vol_24h <= 0:
            return None
        avg_hourly = vol_24h / 24
        if avg_hourly <= 0:
            return None
        growth = vol_1h / avg_hourly
        tag = "🔥 accelerating" if growth >= 1.5 else ("🟢 steady" if growth >= 0.8 else "🟡 cooling")
        return f"📈 Volume Growth: <b>{growth:.1f}×</b> vs 24h avg ({tag})"

    def _score_breakdown_line() -> str:
        if not breakdown:
            return None
        extra = ""
        if breakdown.get("smart_money_whale_bonus"):
            extra += f" · Smart$/Whale +{breakdown.get('smart_money_whale_bonus', 0)}"
        if breakdown.get("narrative_social_multiplier"):
            extra += f" · Narrative +{breakdown.get('narrative_social_multiplier', 0)}"
        if breakdown.get("graduation_bonus"):
            extra += f" · Grad.Prob +{breakdown.get('graduation_bonus', 0)}"
        return (
            "🧮 Liq " + f"{breakdown.get('liquidity_lp_integrity', 0)}/25" +
            " · Holders " + f"{breakdown.get('holder_distribution', 0)}/25" +
            " · Momentum " + f"{breakdown.get('momentum_quality', 0)}/30" +
            " · Wallets " + f"{breakdown.get('wallet_deployer_behavior', 0)}/20" +
            extra
        )

    def _pump_probability_line() -> str:
        """Signal Engine re-evaluation — general "Pump Probability"
        heuristic (see scoring._estimate_pump_probability), distinct
        from the Pump.fun-bonding-curve-specific graduation heuristic
        shown in the score breakdown line. Explicitly labeled as a
        heuristic, not a calibrated prediction."""
        prob = breakdown.get("pump_probability")
        if prob is None:
            return None
        tag = "🔥 high" if prob >= 70 else ("🟡 moderate" if prob >= 40 else "🔵 low")
        return f"🚀 Pump Probability: <b>{prob:.0f}%</b> ({tag}, heuristic)"

    def _confidence_line() -> str:
        """Signal Intelligence upgrade — Phase 6 confidence scoring.
        Only shown when analyze_candidate() actually computed one (all
        live pipeline candidates do; this stays optional so any other
        caller of _build_pump_card_text that hasn't been updated to pass
        a `confidence` key doesn't break)."""
        conf = candidate.get("confidence")
        if not conf:
            return None
        return (
            f"🎯 Confidence: <b>{conf['confidence_score']:.0f}/100</b> "
            f"({conf['confirmed_count']}/{conf['checked_count']} checks confirmed)"
        )


    fake_vol_risk = estimate_fake_volume_ratio(d) * 100
    wash_risk = estimate_wash_trading_risk(d) * 100
    sniper_risk = estimate_sniper_wallet_risk(d, candidate.get("holder_count")) * 100
    lock_conf = estimate_liquidity_lock_score(d, contract) * 100

    narrative = _narrative_tag(d)
    narrative_line = f"🧵 <b>{html.escape(narrative)}</b>" if narrative else None

    dex = d.get("dex")
    dex_line = f" · 🏦 {html.escape(str(dex))}" if dex and dex != "Unknown" else ""

    pair_url = d.get("pair_url") or f"https://dexscreener.com/solana/{contract}"

    twitter_url = d.get("twitter_url")
    telegram_url = d.get("telegram_url")
    website_url = d.get("website_url")

    link_parts = [f'<a href="{pair_url}">📊 Chart</a>']
    if twitter_url:
        link_parts.append(f'<a href="{html.escape(twitter_url)}">🐦 X</a>')
    if telegram_url:
        link_parts.append(f'<a href="{html.escape(telegram_url)}">💬 TG</a>')
    if website_url:
        link_parts.append(f'<a href="{html.escape(website_url)}">🌐 Site</a>')
    links_line = " · ".join(link_parts)

    # ── Block 1: Hero — name, tier, age, security, links. This is the
    # "3-second scan" block, mirroring the reference card's top section:
    # everything a person needs to decide whether to keep reading.
    hero_block = [
        f"🔥 <b>{html.escape(d.get('name', 'Unknown'))}</b> (${html.escape(str(d.get('symbol', '???')))})"
        f"  ·  <b>{tier}</b>",
        _price_mismatch_line(),
        f"🕒 Age: <b>{format_age(d.get('pair_created'))}</b>{dex_line}"
        f"  ·  {_security_line(sec)}",
        narrative_line,
        links_line,
    ]

    # ── Block 2: Market stats — MC/Liq/Vol/Buy-Sell/Fake-Vol/Holders,
    # the same numbers a trader checks first on any scanner.
    market_block = [
        f"💰 MC: <b>{_fmt_usd(effective_market_cap(d) or None)}</b>"
        f"  ·  💧 Liq: <b>{_fmt_usd(d.get('liquidity'))}</b> (lock {lock_conf:.0f}%)",
        f"📈 Vol 1h: <b>{_fmt_usd(d.get('volume_1h'))}</b>"
        f"  ·  ⚖️ {buy_pct:.0f}%/{sell_pct:.0f}% ({int(total_tx)} txns)",
        _volume_growth_line(),
        f"⚠️ Fake-Vol: <b>{fake_vol_risk:.0f}%</b>"
        f"  ·  Wash: <b>{wash_risk:.0f}%</b>"
        f"  ·  Sniper: <b>{sniper_risk:.0f}%</b>",
        f"👥 Holders: <b>{holders_count}</b>",
    ]

    # ── Block 3: Distribution/bundle — who actually holds supply. This
    # is the block the reference card gives the most visual weight to,
    # since it's the fastest tell for an obvious rug setup.
    distribution_block = [
        f"🎯 Top1: <b>{_fmt_pct(holder_analysis.get('top_holder_pct'))}</b>"
        f"  ·  Top10: <b>{_fmt_pct(holder_analysis.get('top10_pct'))}</b>",
        f"🛠 Dev Holding: <b>{_fmt_pct(dev_pct)}</b>",
        (
            f"📦 Bundled: <b>{bundle_wallets} wallets</b> ({_fmt_pct(bundle_pct)} of supply)"
            if bundle_wallets else "📦 Bundled: <b>None detected</b>"
        ),
        _funding_cluster_line(),
    ]

    # ── Block 4: Intelligence deep-dive — everything that took real
    # cross-referencing to compute (confidence scoring, smart money/whale
    # presence, deployer history, verified LP lock, momentum, score
    # breakdown). Kept together and clearly labeled so a quick reader can
    # skip it, while a careful reader still gets every signal your
    # pipeline actually checked — nothing from the original card is lost.
    intelligence_block = [
        "🧠 <b>Signal Intelligence</b>",
        f"AlphaPulse Score: <b>{score}/100</b> — {tier}",
        _confidence_line(),
        _pump_probability_line(),
        _score_breakdown_line(),
        _momentum_line(),
        _smart_money_line(),
        _whale_line(),
        _real_lp_lock_line(),
        _deployer_history_line(),
    ]

    footer_block = [
        f"📋 Why: {html.escape(reasons_line)}",
        f"<code>{contract}</code>",
    ]

    def _render_block(block_lines) -> str:
        return "\n".join(l for l in block_lines if l)

    sections = [
        _render_block(hero_block),
        _render_block(market_block),
        _render_block(distribution_block),
        _render_block(intelligence_block),
        _render_block(footer_block),
    ]

    divider = "\n━━━━━━━━━━━━━━━━━━━━━\n"
    text = divider.join(s for s in sections if s)
    return text


async def send_pump_card(bot, chat_id, candidate):
    d = candidate["data"]
    text = _build_pump_card_text(candidate)

    kb = build_token_actions_keyboard(
        candidate["contract"],
        d.get("pair_url"),
        website_url=d.get("website_url") or None,
        twitter_url=d.get("twitter_url") or None,
        telegram_url=d.get("telegram_url") or None,
    )
    try:
        if d.get("image_url"):
            return await bot.send_photo(chat_id, d["image_url"], caption=text, reply_markup=kb)
        return await bot.send_message(chat_id, text, reply_markup=kb)
    except Exception as e:
        logger.warning(f"send_pump_card failed for {chat_id}: {e}")
        return None


async def subscribe_all_users_to_pump_alerts() -> int:
    """
    Auto-subscribes every existing user to pump alerts (no manual opt-in
    required). Only inserts subscriptions for users who don't already have
    one — anyone who explicitly disabled alerts via /pump_alerts_off keeps
    their choice respected, since this never touches existing rows.
    Safe to run on every boot.
    """
    from models.user import User

    async with async_session() as session:
        existing_res = await session.execute(select(PumpAlertSubscription.user_id))
        already_subscribed = set(existing_res.scalars().all())

        all_users_res = await session.execute(select(User.telegram_id))
        all_user_ids = set(all_users_res.scalars().all())

        missing = all_user_ids - already_subscribed
        for uid in missing:
            session.add(PumpAlertSubscription(user_id=uid, enabled=True))

        if missing:
            await session.commit()

    if missing:
        logger.info(f"✅ Auto-subscribed {len(missing)} user(s) to pump alerts")

    return len(missing)


async def set_pump_subscription(user_id: int, enabled: bool) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(PumpAlertSubscription).where(PumpAlertSubscription.user_id == user_id)
        )
        sub = result.scalar_one_or_none()
        if sub:
            sub.enabled = enabled
        else:
            session.add(PumpAlertSubscription(user_id=user_id, enabled=enabled))
        await session.commit()


async def get_pump_subscription_status(user_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(PumpAlertSubscription).where(PumpAlertSubscription.user_id == user_id)
        )
        sub = result.scalar_one_or_none()
    return bool(sub and sub.enabled)


async def get_pump_subscribers():
    db_user_ids = []
    async with async_session() as session:
        res = await session.execute(select(PumpAlertSubscription.user_id).where(PumpAlertSubscription.enabled == True))
        db_user_ids = res.scalars().all()

    channel_ids = _load_channel_ids()

    merged = []
    seen = set()
    for item in list(db_user_ids) + list(channel_ids):
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return merged


async def migrate_pump_alert_schema():
    """Adds the re-arm tracking columns to pump_alerted_tokens if missing."""
    try:
        from infra.db.session import engine
        async with engine.begin() as conn:
            await conn.execute(text(
                "ALTER TABLE pump_alerted_tokens ADD COLUMN IF NOT EXISTS times_alerted INTEGER DEFAULT 1"
            ))
            await conn.execute(text(
                "ALTER TABLE pump_alerted_tokens ADD COLUMN IF NOT EXISTS last_alert_ath_multiple FLOAT"
            ))
            # Signal-history / cooldown-system columns (internal use only,
            # never exposed to users — see was_already_alerted/mark_alerted
            # below).
            await conn.execute(text(
                "ALTER TABLE pump_alerted_tokens ADD COLUMN IF NOT EXISTS token_name VARCHAR"
            ))
            await conn.execute(text(
                "ALTER TABLE pump_alerted_tokens ADD COLUMN IF NOT EXISTS first_alerted_at TIMESTAMP"
            ))
            await conn.execute(text(
                "ALTER TABLE pump_alerted_tokens ADD COLUMN IF NOT EXISTS cooldown_expires_at TIMESTAMP"
            ))
    except Exception as e:
        logger.warning(f"Pump alert schema migration skipped: {e}")


async def _signal_rearm_eligible(contract: str) -> bool:
    """
    True if a previously-alerted contract is allowed to be reconsidered
    for a fresh signal — the two structural conditions from the Signal
    Lifecycle spec (Blueprint Problem 4), neither of which is a fixed
    percentage magic number applied uniformly to every token:

    A. Genuinely NEW ATH — signal.ath_multiple is strictly above
       whatever ath_multiple was already in effect the last time this
       contract was actually alerted (PumpAlertedToken.
       last_alert_ath_multiple). This is what stops ordinary price
       noise / chopping near an old high from re-firing the same
       "signal" over and over — it only re-arms once the token has
       genuinely pumped past its own previous high.

    B. Genuine correction — signal.current_multiple sits below
       signal.ath_multiple at all (i.e. the token has actually pulled
       back from its own peak, whatever that peak was). Meeting this
       condition does NOT alert anything by itself and does NOT use a
       fixed retrace ratio to decide whether the pullback was "enough".
       It only reopens the contract to scan_for_pump_candidates, where
       analyze_candidate's full existing pipeline — hard_reject_
       reasons, conviction scoring, and the live quota_governor dynamic
       cutoff, all with fresh volume/momentum/security data — has to
       independently rate it a genuine high-confidence setup all over
       again before any alert can fire. That full re-scoring pass is
       the "existing AlphaPulse AI Intelligence" decision the spec
       calls for; this function never substitutes its own scoring for
       it.
    """
    from models.signal_token import SignalToken

    async with async_session() as session:
        res = await session.execute(select(SignalToken).where(SignalToken.contract == contract))
        signal = res.scalar_one_or_none()

        alerted_res = await session.execute(
            select(PumpAlertedToken).where(PumpAlertedToken.contract == contract)
        )
        alerted_row = alerted_res.scalar_one_or_none()

    if not signal:
        return False

    ath = signal.ath_multiple or 1.0
    current = signal.current_multiple or 1.0

    last_alert_ath = (alerted_row.last_alert_ath_multiple if alerted_row else None) or 0.0
    is_new_high = ath > last_alert_ath * 1.01  # 1% buffer so float noise can't count as "new"
    if is_new_high:
        return True  # Condition A

    has_corrected = current < ath  # Condition B — any genuine pullback off its own ATH
    return has_corrected


async def was_already_alerted(contract) -> bool:
    """
    Duplicate-signal gate.

    Two independent checks, both must pass for a contract to be eligible:

    1. Cooldown (new, mandatory, non-bypassable): if this contract was
       alerted within the last SIGNAL_COOLDOWN_HOURS, it is blocked
       outright — no exceptions, regardless of price/volume/market-cap/
       smart-wallet/AI-score/trending changes in the meantime. This is
       checked first and short-circuits everything else.

    2. Structural re-arm logic (Signal Lifecycle, Blueprint Problem 4):
       once the cooldown has expired, a contract that has never been
       alerted passes straight through. A contract that HAS been
       alerted before is blocked UNLESS it has made a genuinely new ATH
       beyond its last alerted high, or has corrected off its own ATH
       (see _signal_rearm_eligible) — in which case it's allowed
       through here, and only fires a new alert if it then also clears
       every normal quality/score gate (including the cooldown above)
       again with fresh volume and momentum.
    """
    async with async_session() as session:
        res = await session.execute(select(PumpAlertedToken).where(PumpAlertedToken.contract == contract))
        row = res.scalar_one_or_none()

    if row is None:
        return False

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if row.cooldown_expires_at and now < row.cooldown_expires_at:
        return True

    return not await _signal_rearm_eligible(contract)


async def mark_alerted(contract, symbol, score, name=None):
    from models.signal_token import SignalToken

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cooldown_expires_at = now + timedelta(hours=SIGNAL_COOLDOWN_HOURS)

    async with async_session() as session:
        signal_res = await session.execute(select(SignalToken).where(SignalToken.contract == contract))
        signal = signal_res.scalar_one_or_none()
        current_ath = (signal.ath_multiple if signal else None) or 1.0

        res = await session.execute(select(PumpAlertedToken).where(PumpAlertedToken.contract == contract))
        row = res.scalar_one_or_none()
        if row:
            row.symbol = symbol
            row.pump_score = score
            if name:
                row.token_name = name
            row.alerted_at = now
            row.times_alerted = (row.times_alerted or 1) + 1
            row.last_alert_ath_multiple = current_ath
            row.cooldown_expires_at = cooldown_expires_at
            if not row.first_alerted_at:
                row.first_alerted_at = now
        else:
            session.add(PumpAlertedToken(
                contract=contract, symbol=symbol, pump_score=score,
                token_name=name,
                last_alert_ath_multiple=current_ath,
                first_alerted_at=now,
                cooldown_expires_at=cooldown_expires_at,
            ))
        await session.commit()


def format_usd(value) -> str:
    try:
        num = float(value)
        if abs(num) >= 1000:
            return f"${num:,.2f}"
        return f"${num:.2f}"
    except Exception:
        return "N/A"


SMART_AUTOBUY_SCORE_THRESHOLD = 62.0


def calculate_potential_score(candidate: dict) -> float:
    """
    Internal token "potential score" (0-100) used to drive default (no
    custom-filter) auto-buy selection — an advanced, multi-indicator
    scoring model built to weigh a candidate the way an experienced
    memecoin trader would, rather than a coin-flip:

      - base alert score (liquidity/volume/narrative/security gates the
        candidate already passed to reach this point)
      - holder base size (community adoption)
      - volume acceleration relative to liquidity (real momentum vs. a
        thin/illiquid pool that any small trade can move)
      - buy-vs-sell pressure (a smart-money-inflow proxy: aggressive net
        buying vs. distribution)
      - momentum trend CONSISTENCY: 1h and 6h price action agreeing on
        direction is a stronger continuation signal than a single-window
        spike that could just be noise or a single large buy
      - dev/insider holding risk-adjustment: a high dev wallet balance is
        downweighted, since a large dev bag is one of the most common
        precursors to a rug/dump regardless of how good the other numbers
        look
    """
    d = candidate.get("data", {}) or {}
    pump = candidate.get("pump", {}) or {}
    holder_analysis = candidate.get("holder_analysis") or {}

    base_score = _to_float(pump.get("score"))

    holders = holder_analysis.get("total_holders")
    if holders is None:
        holders = candidate.get("holder_count")
    holder_component = min(_to_float(holders) / 200.0, 1.0) * 100

    liq = _to_float(d.get("liquidity"))
    vol_1h = _to_float(d.get("volume_1h"))
    vol_ratio = (vol_1h / liq) if liq > 0 else 0.0
    volume_component = min(vol_ratio / 2.0, 1.0) * 100

    buys = _to_float(d.get("txns_1h_buys"))
    sells = _to_float(d.get("txns_1h_sells"))
    total_tx = buys + sells
    smart_money_component = (buys / total_tx * 100) if total_tx > 0 else 50.0

    # --- Momentum trend consistency: does 6h action agree with 1h? ---
    price_change_1h = _to_float(d.get("price_change_1h"))
    price_change_6h = _to_float(d.get("price_change_6h"))
    if price_change_1h > 0 and price_change_6h > 0:
        trend_component = 100.0  # sustained uptrend across both windows
    elif price_change_1h > 0 and price_change_6h <= 0:
        trend_component = 55.0  # fresh move, no longer-window confirmation yet
    elif price_change_1h <= 0 and price_change_6h > 0:
        trend_component = 40.0  # cooling off after an earlier run
    else:
        trend_component = 15.0  # trending down on both windows

    blended = (
        base_score * 0.40
        + holder_component * 0.15
        + volume_component * 0.15
        + smart_money_component * 0.15
        + trend_component * 0.15
    )

    # --- Dev/insider holding risk-adjustment ---
    dev_holding_pct = _to_float(holder_analysis.get("dev_holding_pct"))
    if dev_holding_pct >= 15:
        blended *= 0.70
    elif dev_holding_pct >= 8:
        blended *= 0.88

    return max(0.0, min(blended, 100.0))


def _passes_smart_selection(potential_score: float) -> bool:
    """
    Deterministic trader-style gate used for default (no-filter) auto-buy:
    a candidate is bought only if its blended potential score (see
    calculate_potential_score) clears SMART_AUTOBUY_SCORE_THRESHOLD. This
    replaces the previous weighted-random coin-flip — the same score now
    always resolves to the same decision, exactly like a trader running a
    consistent checklist, instead of leaving strong signals to chance.
    """
    return potential_score >= SMART_AUTOBUY_SCORE_THRESHOLD


def candidate_matches_filters(candidate: dict, filters) -> tuple[bool, str | None]:
    """
    Checks a candidate signal against a user's saved PaperAutoBuyFilter
    thresholds. Any threshold left as None is treated as unconstrained.
    Returns (matches, rejection_reason).
    """
    d = candidate.get("data", {}) or {}
    holder_analysis = candidate.get("holder_analysis") or {}

    market_cap = effective_market_cap(d)
    liquidity = _to_float(d.get("liquidity"))

    holders = holder_analysis.get("total_holders")
    if holders is None:
        holders = candidate.get("holder_count")
    holders = _to_float(holders)

    bundle_pct = _to_float(holder_analysis.get("bundle_pct"))
    dev_holding_pct = _to_float(holder_analysis.get("dev_holding_pct"))
    age_hours = _age_hours(d.get("pair_created"))

    if filters.min_market_cap is not None and market_cap < filters.min_market_cap:
        return False, "market cap below your filter"
    if filters.max_market_cap is not None and market_cap > filters.max_market_cap:
        return False, "market cap above your filter"
    if filters.min_holders is not None and holders < filters.min_holders:
        return False, "holder count below your filter"
    if filters.min_liquidity_usd is not None and liquidity < filters.min_liquidity_usd:
        return False, "liquidity below your filter"
    if filters.max_bundle_pct is not None and bundle_pct > filters.max_bundle_pct:
        return False, "bundle % above your filter"
    if filters.max_dev_holding_pct is not None and dev_holding_pct > filters.max_dev_holding_pct:
        return False, "dev holding % above your filter"
    if filters.min_age_hours is not None and age_hours < filters.min_age_hours:
        return False, "token younger than your filter"
    if filters.max_age_hours is not None and age_hours > filters.max_age_hours:
        return False, "token older than your filter"

    return True, None


async def auto_buy_for_new_signal(bot, candidate):
    """
    Executes a paper buy for every user who has Auto Buy enabled, using
    each user's own saved buy amount / TP / SL settings.

    Selection logic per user:
    - If the user has active custom filters (market cap range, holder
      count, liquidity, bundle %, dev holding %, token age, etc.) and
      those filters are enabled, the buy only fires when this signal
      matches ALL of them.
    - If the user has no filters set (or has paused them), the bot falls
      back to a deterministic multi-indicator scoring model (holder
      growth, volume acceleration, buy-pressure/smart-money inflow,
      momentum trend consistency, and dev-holding risk) — see
      calculate_potential_score / _passes_smart_selection.
    """
    d = candidate["data"]
    contract = candidate["contract"]
    name = d.get("name", "Unknown")
    symbol = d.get("symbol", "???")
    price = _to_float(d.get("price"))

    if price <= 0:
        return

    try:
        autobuy_settings = await get_autobuy_settings()
    except Exception as e:
        logger.error(f"Auto-buy settings fetch failed: {e}")
        return

    if not autobuy_settings:
        return

    try:
        filters_map = await get_filters_map([s.user_id for s in autobuy_settings])
    except Exception as e:
        logger.error(f"Auto-buy filters fetch failed: {e}")
        filters_map = {}

    potential_score = calculate_potential_score(candidate)

    for settings in autobuy_settings:
        try:
            user_filters = filters_map.get(settings.user_id)
            trigger_note = None

            if user_filters and user_filters.enabled and user_filters.has_active_filters():
                matches, reject_reason = candidate_matches_filters(candidate, user_filters)
                if not matches:
                    continue
                trigger_note = "🎯 Matched your custom Auto-Buy filters"
            else:
                if not _passes_smart_selection(potential_score):
                    continue
                trigger_note = f"🧠 Smart auto-pick (potential score: {potential_score:.0f}/100)"

            result = await execute_paper_buy(
                user_id=settings.user_id,
                contract=contract,
                name=name,
                symbol=symbol,
                current_price=price,
                is_auto=True,
            )

            if result.get("ok") and settings.notifications_enabled:
                text = (
                    "🤖 <b>Auto Buy Executed</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📛 {result.get('name', 'Unknown')} (${result.get('symbol', '???')})\n"
                    f"💰 Entry: ${result.get('entry_price', 0):.8f}\n"
                    f"📊 Invested: {format_usd(result.get('usd_invested', 0))}\n"
                    f"🪙 Tokens: {result.get('token_quantity', 0):.4f}\n"
                    f"🎯 TP: {settings.take_profit_pct}% | SL: {settings.stop_loss_pct}%\n"
                    f"{trigger_note}\n\n"
                    f"<code>{contract}</code>"
                )
                try:
                    await bot.send_message(settings.user_id, text)
                except Exception as e:
                    logger.warning(f"Auto-buy notification failed for {settings.user_id}: {e}")

        except Exception as e:
            logger.error(f"Auto-buy failed for user {settings.user_id}: {e}")


# How many still-undelivered signals we check for milestone eligibility
# each cycle. Cheap (one price lookup each), so this can comfortably
# scan a much larger pool than we'd ever actually force-send -- a dead
# token sitting ahead of an eligible one in the same batch must never
# starve the eligible one of being checked.
MAX_REDELIVERY_SCAN_PER_CYCLE = 50

# How many of THOSE (milestone-eligible) signals actually get force-sent
# per cycle -- the expensive step: a real alert to every subscriber,
# plus auto-buy. Any eligible signal beyond this cap is simply picked
# up on the next cycle.
MAX_REDELIVERIES_PER_CYCLE = 5


async def redeliver_undelivered_signal_alerts(bot):
    """
    Targeted fix (2026-08-22, follow-up to commit 61cac5e): the original
    version of this function force-sent the initial Signal Alert for
    EVERY signal with alert_delivered == False, unconditionally. That
    was too broad -- most undelivered signals are simply dead / never
    took off (low MC, no liquidity, no volume), and redelivering their
    Signal Alert served no purpose while still spending a Telegram send
    + an auto-buy attempt on them.

    The actual invariant we need is narrower, and is already fully
    described by SignalMilestoneGate's own comment in
    domain/signals/signal_tracker.py::signal_lifecycle_loop: a milestone
    (Quote) alert must never fire before the initial Signal Alert has
    been delivered. So a forced redelivery is only ever justified for a
    signal that has ACTUALLY become milestone-eligible right now -- i.e.
    signal_lifecycle_loop would compute a non-empty crossed_milestones
    for it this very cycle, if only its alert_delivered gate weren't
    blocking that computation.

    This reuses that exact eligibility check -- _milestones_crossed()
    and MIN_24H_VOLUME_FOR_QUOTE_ALERT, both imported directly from
    signal_tracker.py, not re-derived or approximated -- against a live
    price pull, for every undelivered signal:

        milestone-eligible right now?
            NO  -> leave it alone entirely (no send, no state change;
                   it's just a dead/inactive undelivered signal)
            YES -> force-send the original Signal Alert (not a milestone
                   card). Once delivered, message_ids_json/alert_delivered
                   flip exactly as on first delivery, so on
                   signal_lifecycle_loop's very next pass the
                   SignalMilestoneGate is unblocked and the milestone(s)
                   it just earned fire normally through the existing,
                   untouched send_milestone_alert() path -- and
                   auto_buy_for_new_signal() runs for it, identically to
                   any other freshly-delivered signal.

    No scoring, discovery, filter, risk-gate, trading, or auto-buy logic
    is touched by this function -- it only decides whether a stuck
    signal's original alert gets force-sent.

    NOTE: analyze_candidate() is intentionally NOT reused here -- it
    unconditionally self-rejects via was_already_alerted() for any
    contract that already passed through mark_alerted() (which happens
    on the very first attempt regardless of delivery outcome), so it can
    never return a candidate for exactly the rows this function needs to
    retry. The redelivery card is instead built from the SignalToken
    row's own persisted entry/holder snapshot (never re-scored, so a
    signal's entry price/MC and buy-eligibility criteria can't shift on
    redelivery), refreshed with a live DexScreener pull for current
    price/links/momentum so the card itself isn't stale.
    """
    async with async_session() as session:
        res = await session.execute(
            select(SignalToken)
            .where(SignalToken.status == "active", SignalToken.alert_delivered == False)  # noqa: E712
            .limit(MAX_REDELIVERY_SCAN_PER_CYCLE)
        )
        pending = res.scalars().all()

    if not pending:
        return

    subscribers = await get_pump_subscribers()
    if not subscribers:
        return

    sent_count = 0

    for s in pending:
        if sent_count >= MAX_REDELIVERIES_PER_CYCLE:
            break

        try:
            data = await get_token_card_info(s.contract)
            if not data:
                logger.warning(
                    f"[Redelivery] No live market data for {s.contract[:8]} -- retrying next cycle"
                )
                continue

            # --- Milestone-eligibility gate: the ONLY reason to force a
            # redelivery. Reuses signal_lifecycle_loop's exact logic
            # (same helper, same constant, same gain formula) so this
            # can never drift out of sync with what actually triggers a
            # Quote Alert. A signal that hasn't earned a milestone right
            # now is left completely untouched -- no send, no DB write.
            cur_mc = _to_float(data.get("market_cap")) or _to_float(data.get("fdv"))
            if not s.entry_market_cap or s.entry_market_cap <= 0 or cur_mc <= 0:
                continue
            gain = cur_mc / s.entry_market_cap

            vol_24h = _to_float(data.get("volume_24h"))
            is_currently_trading = vol_24h >= MIN_24H_VOLUME_FOR_QUOTE_ALERT
            if not is_currently_trading:
                continue

            crossed = _milestones_crossed(s.highest_alerted_multiple or 1.0, gain)
            if not crossed:
                continue
            # --- end eligibility gate ---

            candidate = {
                "contract": s.contract,
                "data": data,
                "pump": {
                    "score": int(s.entry_score or 0),
                    "tier": "Signal Alert (redelivered)",
                    "reasons": [
                        f"Milestone-eligible ({crossed[-1][1]}) but initial Signal "
                        "Alert never delivered -- force-sent so the earned "
                        "milestone alert can follow"
                    ],
                    "breakdown": json.loads(s.entry_breakdown_json) if s.entry_breakdown_json else {},
                },
                "holder_analysis": {
                    "total_holders": s.total_holders,
                    "top_holder_pct": s.top_holder_pct,
                    "top10_pct": s.top10_holder_pct,
                    "dev_holding_pct": s.dev_holding_pct,
                    "bundle_wallet_count": s.bundle_wallet_count,
                    "bundle_pct": s.bundle_pct,
                },
            }

            msg_ids = {}
            delivery_failures = 0
            for chat_id in subscribers:
                try:
                    msg = await send_pump_card(bot, chat_id, candidate)
                    if msg and hasattr(msg, "message_id"):
                        msg_ids[str(chat_id)] = msg.message_id
                except Exception as e:
                    delivery_failures += 1
                    logger.warning(
                        f"[Redelivery] send failed for chat {chat_id} / {s.contract[:8]}: {e}"
                    )
                await asyncio.sleep(0.1)

            if not msg_ids:
                logger.warning(
                    f"[Redelivery] {s.contract[:8]}: still 0/{len(subscribers)} delivered "
                    f"({delivery_failures} failed) -- will retry next cycle"
                )
                continue

            await update_signal_message_ids(s.contract, msg_ids)
            await mark_signal_alert_delivered(s.contract)
            # Persist that this Signal Alert was delivered via retry, not on
            # the first attempt -- lets the real-wallet auto-buy "signal
            # source" setting (New / Redelivered / Both) tell them apart.
            async with async_session() as _redelivery_flag_session:
                await _redelivery_flag_session.execute(
                    text("UPDATE signal_tokens SET was_redelivered = TRUE WHERE contract = :contract"),
                    {"contract": s.contract},
                )
                await _redelivery_flag_session.commit()
            sent_count += 1
            logger.info(
                f"[Redelivery] {s.contract[:8]}: delivered to {len(msg_ids)}/{len(subscribers)} "
                f"subscriber(s) after crossing {crossed[-1][1]} -- Signal Alert confirmed, "
                "milestone(s) + auto-buy unblocked for the next lifecycle pass"
            )

            try:
                await auto_buy_for_new_signal(bot, candidate)
            except Exception as e:
                logger.error(f"[Redelivery] Auto-buy failed (non-fatal) for {s.contract[:8]}: {e}")

        except Exception as e:
            logger.error(f"[Redelivery] error for signal {s.contract[:8]}: {e}")

        await asyncio.sleep(0.5)


async def pump_radar_loop(bot, interval_seconds: int = 60):
    logger.info("🔥 Pump.fun ONLY Radar Active")

    try:
        await migrate_pump_alert_schema()
    except Exception as e:
        logger.error(f"Pump alert schema migration failed (non-fatal): {e}")

    while True:
        try:
            try:
                await maybe_adjust_cutoff()
            except Exception as e:
                logger.warning(f"Quota governor cutoff adjustment failed (non-fatal): {e}")

            subscribers = await get_pump_subscribers()
            if not subscribers:
                await asyncio.sleep(20)
                continue

            try:
                await redeliver_undelivered_signal_alerts(bot)
            except Exception as e:
                logger.error(f"Signal Alert redelivery failed (non-fatal): {e}")

            if not await has_quota_remaining():
                # Daily hard cap reached (Blueprint 3.4 DAILY_MAX) — no
                # further alerts today, however strong a later candidate
                # scores. Resets automatically at UTC midnight since the
                # count is derived from alerted_at timestamps.
                logger.info("Daily alert cap reached — holding until next UTC day")
                await asyncio.sleep(interval_seconds)
                continue

            candidates = await scan_for_pump_candidates(
                min_score=MIN_SCORE_TO_ALERT,
                max_results=MAX_ALERTS_PER_CYCLE
            )

            if not candidates:
                logger.info("No qualified Pump.fun candidates this cycle")
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

                await create_signal_from_candidate(candidate)

                # These are auxiliary features — a failure must NEVER
                # prevent the actual alert below from being sent.
                try:
                    await bump_signal_count_and_maybe_broadcast(bot)
                except Exception as e:
                    logger.error(f"Signal counter/broadcast failed (non-fatal): {e}")

                # Production incident fix (see SIGNAL_ENGINE_REEVALUATION.md):
                # auto_buy_for_new_signal() used to be called here, right
                # after create_signal_from_candidate() and BEFORE any
                # delivery attempt below -- so real/paper capital could
                # move on a token even if every subscriber send then
                # failed. It has been moved below the delivery loop and is
                # now gated on delivery actually being confirmed. No
                # hard-reject, scoring, holder/bundle, or risk-gate logic
                # is touched -- this only adds a stricter, additional
                # condition (confirmed alert delivery) before a signal
                # becomes buy-eligible.

                msg_ids = {}
                delivery_failures = 0
                for chat_id in subscribers:
                    try:
                        msg = await send_pump_card(bot, chat_id, candidate)
                        if msg and hasattr(msg, "message_id"):
                            msg_ids[str(chat_id)] = msg.message_id
                    except Exception as e:
                        # A single subscriber failing to receive the card
                        # (blocked bot, chat deleted, transient Telegram
                        # error, etc.) must never abort delivery to the
                        # rest of the subscriber list, and must never skip
                        # mark_alerted() below — either of those previously
                        # caused was_already_alerted() to stay False and
                        # the exact same signal to be re-sent (duplicated)
                        # to subscribers who'd already gotten it, on the
                        # very next scan cycle.
                        delivery_failures += 1
                        logger.warning(f"Pump card delivery failed for chat {chat_id}: {e}")
                    await asyncio.sleep(0.1)

                if msg_ids:
                    await update_signal_message_ids(mint, msg_ids)

                if delivery_failures:
                    logger.warning(
                        f"Pump alert for {mint[:8]}: delivered to "
                        f"{len(msg_ids)}/{len(subscribers)} subscribers "
                        f"({delivery_failures} failed) — marking alerted "
                        "regardless so it is not duplicated next cycle."
                    )

                await mark_alerted(
                    mint,
                    candidate["data"].get("symbol"),
                    candidate["pump"]["score"],
                    name=candidate["data"].get("name"),
                )

                if msg_ids:
                    # At least one subscriber genuinely received the
                    # Signal Alert card this cycle -- only now may either
                    # auto-buy path treat this signal as buy-eligible.
                    try:
                        await mark_signal_alert_delivered(mint)
                    except Exception as e:
                        logger.error(f"mark_signal_alert_delivered failed (non-fatal): {e}")

                    try:
                        await auto_buy_for_new_signal(bot, candidate)
                    except Exception as e:
                        logger.error(f"Auto-buy for signal failed (non-fatal): {e}")
                else:
                    logger.warning(
                        f"Pump alert for {mint[:8]}: delivered to 0/"
                        f"{len(subscribers)} subscribers -- skipping auto-buy "
                        "for this signal (no confirmed Signal Alert delivery)."
                    )

                alerts_sent += 1
                break

            if alerts_sent:
                logger.info(f"Sent {alerts_sent} Pump.fun alert(s) to {len(subscribers)} subscriber(s)")

        except Exception as e:
            logger.error(f"Pump Radar Error: {e}")

        await asyncio.sleep(interval_seconds)
