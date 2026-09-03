"""
Conviction scoring engine — Blueprint Section 1.3 / 2.2.

Replaces the old binary pass/fail `calculate_pump_fun_score` gate in
pump_radar.py with a weighted 0-100 conviction score across four
categories (Liquidity & LP Integrity, Holder Distribution, Momentum
Quality, Wallet/Deployer Behavior), plus a narrative/social multiplier
that can only ever help a token that already scored well on-chain —
never substitute for on-chain quality (Blueprint 1.2 "bottom line").

Hard gates (Section 2.2) are evaluated first and are non-negotiable:
a token that fails any of them is rejected before it ever reaches the
scoring math below, regardless of how good its other numbers look.

This module is intentionally self-contained (no DB/IO) so it stays
cheap to call for every scan-cycle candidate and is easy to unit test /
backtest (Section 3.1) in isolation.
"""

import logging
import time

from domain.intelligence.narrative_scanner import classify_token
from domain.intelligence.risk_engine import (
    estimate_fake_volume_ratio,
    estimate_wash_trading_risk,
    estimate_sniper_wallet_risk,
    estimate_liquidity_lock_score,
)

logger = logging.getLogger("AlphaPulse.ConvictionScorer")

# ---------------------------------------------------------------------
# Cutoffs (Blueprint 1.3 "Cutoffs")
# ---------------------------------------------------------------------
SEND_CUTOFF = 80.0          # score >= this (pre-multiplier) always sendable
HARD_FLOOR_CUTOFF = 65.0    # score < this is never sendable, any multiplier
MAX_MULTIPLIER = 10.0       # combined ceiling on ALL bonus categories together
                             # (narrative/social + smart-money/whale + graduation
                             # heuristic) — see the cap applied in score_candidate()
                             # below. Each bonus category is individually
                             # "additive-only, can't rescue a weak token" by its
                             # own docstring, but nothing previously stopped them
                             # stacking together past that intent (up to +19 was
                             # possible pre-fix: 6 narrative + 8 smart/whale + 5
                             # graduation), which could carry a barely-eligible
                             # base_score of 65 all the way to a final_score of 84
                             # on hype alone, with zero extra on-chain quality.
                             # That directly undermines the "more conviction"
                             # requirement, so the combined total is now hard-
                             # capped here.

# ---------------------------------------------------------------------
# Mandatory Market Cap / Liquidity / LP-ratio quality gate.
#
# SINGLE SOURCE OF TRUTH — this used to be three separate, disagreeing
# checks scattered across the pipeline:
#   1. pump_radar.analyze_candidate()'s pre-filter (MIN_LIQUIDITY_USD,
#      MIN_MARKET_CAP/MAX_MARKET_CAP, a bare `liq >= mc` reject)
#   2. hard_reject_reasons()'s own looser `liq/mc < 0.02` ratio check
#      (equivalent to allowing MC up to 50x liquidity)
#   3. pump_radar._passes_mc_lp_safety_filter(), applied AFTER the full
#      expensive pipeline (security, holders, LP lock, funding graph,
#      deployer history) had already run, requiring MC >= 2x liquidity
#      (a MUCH stricter bar than #2's 50x)
#
# Having three different definitions of "healthy MC/liquidity" is
# exactly why tuning "quality" in one place didn't reliably change
# outcomes, and why a candidate could survive the entire expensive
# pipeline only to be thrown away at the very last step for a reason
# that could have been checked for free before any API calls were
# made. This function is now the only place that decides MC/liquidity
# eligibility, called once, early, in analyze_candidate() before any
# paid/rate-limited lookups happen. MC and LIQ quality is mandatory —
# never bypassed, never softened by the quota governor's score cutoff.
# ---------------------------------------------------------------------
MIN_LIQUIDITY_USD = 5000
MIN_MARKET_CAP = 8000
MAX_MARKET_CAP = 1500000
MC_LP_MIN_RATIO = 2.0   # MC must be at least this many times liquidity —
                         # rejects tight/near-equal MC-vs-LP spreads
                         # (e.g. MC $61.4K vs LP $48.3K, ~1.27x) in favor
                         # of a wide, healthy gap (e.g. MC $163.5K vs LP
                         # $38.1K, ~4.29x). A tight spread means a small
                         # sell can move price disproportionately and/or
                         # the liquidity itself looks manufactured to
                         # match the mcap rather than genuinely deep.


def passes_mc_liquidity_gate(data: dict) -> tuple[bool, str | None]:
    """
    Mandatory MC/Liquidity/LP-ratio quality gate. Returns (True, None)
    if the candidate clears every part of it, or (False, reason) for
    the first failing check. Cheap (no IO) — always call this before
    any paid/rate-limited lookup (GoPlus, Helius, Raydium, etc.).

    Uses effective_market_cap() as the ONLY source for "mc" so this
    gate, the alert card, and candidate_matches_filters() can never
    read a different market-cap figure than the one that was actually
    checked here.
    """
    liq = _to_float(data.get("liquidity"))
    mc = effective_market_cap(data)

    contract_tag = str(data.get("contract") or data.get("address") or "?")[:8]

    if liq < MIN_LIQUIDITY_USD:
        reason = f"liquidity ${liq:,.0f} below required ${MIN_LIQUIDITY_USD:,.0f}"
        logger.info(f"[MC/Liq Gate] {contract_tag}: REJECT mc=${mc:,.0f} liq=${liq:,.0f} — {reason}")
        return False, reason
    if mc < MIN_MARKET_CAP:
        reason = f"market cap ${mc:,.0f} below required ${MIN_MARKET_CAP:,.0f}"
        logger.info(f"[MC/Liq Gate] {contract_tag}: REJECT mc=${mc:,.0f} liq=${liq:,.0f} — {reason}")
        return False, reason
    if mc > MAX_MARKET_CAP:
        reason = f"market cap ${mc:,.0f} above ceiling ${MAX_MARKET_CAP:,.0f}"
        logger.info(f"[MC/Liq Gate] {contract_tag}: REJECT mc=${mc:,.0f} liq=${liq:,.0f} — {reason}")
        return False, reason
    if mc <= liq:
        reason = f"market cap (${mc:,.0f}) does not exceed liquidity (${liq:,.0f})"
        logger.info(f"[MC/Liq Gate] {contract_tag}: REJECT mc=${mc:,.0f} liq=${liq:,.0f} — {reason}")
        return False, reason
    ratio = mc / liq
    if ratio < MC_LP_MIN_RATIO:
        reason = f"MC/liquidity ratio {ratio:.2f}x below required {MC_LP_MIN_RATIO:.1f}x"
        logger.info(f"[MC/Liq Gate] {contract_tag}: REJECT mc=${mc:,.0f} liq=${liq:,.0f} ratio={ratio:.2f}x — {reason}")
        return False, reason

    logger.info(f"[MC/Liq Gate] {contract_tag}: PASS mc=${mc:,.0f} liq=${liq:,.0f} ratio={ratio:.2f}x")
    return True, None

# --- Signal Intelligence upgrade: exit-liquidity / dev-holding gate ---
DEV_HOLDING_REJECT_PCT = 15.0   # deployer wallet holding this % or more -> hard reject

# ---------------------------------------------------------------------
# Phase 3.1 — Sybil / bundle forensic tiers.
#
# domain.intelligence.holders._cluster_bundles() groups wallets with
# near-identical balances ("balance similarity"). That clustering is
# genuinely useful EVIDENCE that a set of wallets may have been
# provisioned by the same actor, but it is NOT proof of coordinated/
# Sybil ownership on its own — many unrelated retail buyers/sniper bots
# independently choosing the same round buy size produce an identical
# surface signal. Treating bundle_pct >= a single moderate threshold as
# an automatic hard reject (the old behavior here) conflated the two.
#
# This module already has a second, independent, harder-to-fake
# coordination signal one stage further down the pipeline: shared first-
# funder wallets, traced by domain.intelligence.funding_graph and
# enforced in risk_engine.evaluate_verified_red_flags() (see
# pump_radar.analyze_candidate() — that funding-cluster check is the
# "confirmed manipulation" gate, run after this one, once real funding
# data is actually available). That gate is untouched by this change.
#
# What changes here is scoped to THIS early, estimate-only gate:
#   - BUNDLE_MODERATE_PCT: balance-similarity evidence alone, below this
#     never contributes a hard-reject reason. It still feeds
#     _score_holder_distribution()'s point penalty (evidence, scored —
#     not proof, rejected).
#   - BUNDLE_SEVERE_PCT: concentration this extreme is an unacceptable
#     dump/rug risk in its own right, regardless of whether coordination
#     is ever proven — this is a genuine hard safety condition, not a
#     Sybil-proof claim, so it stays a hard reject.
#   - When the holder snapshot itself was truncated
#     (holder_analysis["holder_data_truncated"]), the severe threshold is
#     raised rather than trusted at face value, since we can't fully rule
#     out the truncated snapshot skewing the clustering input even though
#     truncation is documented to only drop long-tail dust wallets.
# ---------------------------------------------------------------------
BUNDLE_MODERATE_PCT = 40.0           # evidentiary only — scored, never auto-rejects alone
BUNDLE_SEVERE_PCT = 70.0             # concentration this extreme hard-rejects on its own
BUNDLE_SEVERE_PCT_TRUNCATED = 85.0   # stricter bar when the holder snapshot was truncated


def evaluate_sybil_bundle_risk(holder_analysis: dict) -> dict:
    """
    Structured Sybil/bundle assessment from balance-similarity clustering
    alone (see module docstring above for the full picture, including the
    separate funding-relationship-based confirmation gate downstream).

    Returns a dict separating the distinct concerns instead of collapsing
    them into a single boolean:
        {
            "concentration_pct": float,      # bundle_pct, i.e. share of
                                              # tracked supply in balance-
                                              # similarity clusters
            "balance_similarity_evidence": bool,  # >= BUNDLE_MODERATE_PCT
            "holder_data_truncated": bool,
            "coordination_confidence": "none" | "moderate" | "severe",
            "hard_reject": bool,             # True only for the severe,
                                              # risk-regardless-of-proof case
            "reasons": [str, ...],
        }
    """
    raw_bundle_pct = holder_analysis.get("bundle_pct")
    truncated = bool(holder_analysis.get("holder_data_truncated"))
    severe_threshold = BUNDLE_SEVERE_PCT_TRUNCATED if truncated else BUNDLE_SEVERE_PCT

    if raw_bundle_pct is None:
        # No bundle evidence was ever produced for this token (RPC/provider
        # failure, early-token gap, or every holder provider degraded) --
        # not the same thing as a resolved "0% bundled" reading. Treating
        # missing evidence as 0 would silently manufacture a clean bundle
        # profile and a pass through the severe-bundle hard-reject gate for
        # exactly the opaque/unverifiable tokens that need scrutiny most.
        # Stay neutral: no evidence, no hard-reject, no clean credit.
        return {
            "concentration_pct": None,
            "balance_similarity_evidence": False,
            "holder_data_truncated": truncated,
            "coordination_confidence": "unknown",
            "hard_reject": False,
            "reasons": [],
        }

    bundle_pct = _to_float(raw_bundle_pct)
    balance_similarity_evidence = bundle_pct >= BUNDLE_MODERATE_PCT
    hard_reject = bundle_pct >= severe_threshold

    if hard_reject:
        confidence = "severe"
    elif balance_similarity_evidence:
        confidence = "moderate"
    else:
        confidence = "none"

    reasons: list[str] = []
    if hard_reject:
        reasons.append(
            f"Balance-similarity cluster controls {bundle_pct:.1f}% of supply "
            f"— concentration risk regardless of coordination proof"
            + (" (truncated holder snapshot)" if truncated else "")
        )

    return {
        "concentration_pct": bundle_pct,
        "balance_similarity_evidence": balance_similarity_evidence,
        "holder_data_truncated": truncated,
        "coordination_confidence": confidence,
        "hard_reject": hard_reject,
        "reasons": reasons,
    }
DEV_HOLDING_WARN_PCT = 8.0      # below reject line but still elevated -> scoring penalty
# Signal Engine re-evaluation: previously dev_holding_pct only ever
# subtracted points (>= WARN_PCT) or did nothing — there was no credit
# for a *confirmed* clean wallet, even though get_holder_analysis() now
# reliably resolves a real dev_pct (including a genuine 0%) whenever
# dev_address is known, instead of the old None-heavy/estimate-heavy
# picture. "Unknown dev wallet" and "verified negligible dev wallet"
# were being scored identically (both = 0 extra points), which throws
# away real signal now that it's actually available. See
# _score_wallet_behavior().
DEV_HOLDING_CONFIRMED_CLEAN_PCT = 2.0
# Phase 2 calibration: this used to be reused directly as the bonus
# point value too (`points += DEV_HOLDING_CONFIRMED_CLEAN_PCT`), which
# coincidentally worked only because both happened to be 2.0 — a
# threshold-in-percent and a reward-in-points are different units and
# should never have been the same constant, since changing one would
# have silently changed the other. Split out explicitly. Bumped
# 2.0 -> 3.0: a confirmed (not estimated) near-zero dev wallet is now
# reliable Phase-1 holder data, the same upgrade in trust that justified
# giving it any credit at all — it deserves a bit more than the bare
# minimum nudge it had before.
DEV_HOLDING_CLEAN_BONUS = 3.0

# --- Signal Intelligence upgrade: entry-timing / already-extended penalty ---
# A token whose 1h price change is already this large is more likely a
# late entry (chasing an existing move) than a signal "before major
# momentum begins" — discount momentum credit rather than reject outright,
# since a genuinely strong token can still continue from here.
ALREADY_EXTENDED_1H_PCT = 150.0

# --- Signal Intelligence upgrade: Smart Money / Whale conviction bonus ---
# Additive-only, same rule as the narrative multiplier below: can only add
# to an already-qualifying on-chain score, never rescue a weak one.
#
# Phase 2 calibration: these are real, verified on-chain positions (a
# tracked wallet's actual current holding, per the same Helius snapshot
# used for the rest of the card) — the strongest evidence category this
# bonus band has, per Blueprint "prefer strong on-chain evidence over
# short-term hype". Raised relative to the narrative bonus below
# (SMART_MONEY 5.0->6.0, WHALE 3.0->4.0) to make that preference actually
# show up in the numbers, not just the docstrings. MAX_MULTIPLIER (the
# combined ceiling across all bonus categories) is intentionally left
# unchanged — this only shifts which evidence wins the limited room
# inside that ceiling, it doesn't grow the ceiling itself.
SMART_MONEY_MAX_BONUS = 6.0
WHALE_MAX_BONUS = 4.0

# --- Signal Intelligence upgrade: confidence scoring ---
# `confidence_score` (see compute_confidence()) measures how trustworthy
# the collected EVIDENCE is — never how bullish the token looks. Prior to
# Phase 2 this blended 65% of the final conviction score into the number,
# which meant a token with a strong score but almost no resolved
# verification checks (LP lock unknown, deployer history unknown, no
# price cross-check, etc.) could still show a deceptively high
# "confidence" on score alone. Phase 2 removes the score component
# entirely — see compute_confidence() below for the evidence-only
# formula (agreement-quality + evidence-breadth, no bullishness term).
MIN_CONFIDENCE_SCORE = 55.0
# Phase 2 calibration: re-derived for the new evidence-only 0-100 scale
# (old value of 70 was calibrated against a scale that started at
# final_score * 0.65, i.e. ~45-55 points "for free" before a single
# check had even resolved — not a like-for-like bar anymore). 55 on the
# new scale requires roughly 5-6 of 7 possible checks to both resolve
# AND confirm positive (see compute_confidence() docstring for worked
# examples); this is meant to be re-tuned against real outcome data once
# a backtesting harness exists (Phase 3), not treated as final.
TOTAL_POSSIBLE_CHECKS = 7   # must track len(confirmations) built in
                             # pump_radar.analyze_candidate() — the fixed
                             # pool of independent checks this pass can
                             # ever resolve (smart_money_or_whale, lp_lock,
                             # deployer_history, price_agrees,
                             # no_funding_cluster, holder_data_verified,
                             # dev_holding_confirmed_low). Used only for
                             # the evidence-breadth term below, not to
                             # gate anything by itself.
# Signal Engine re-evaluation: raised from 2 -> 3. When this gate was
# designed only 5 confirmation checks existed (smart_money_or_whale,
# lp_lock, deployer_history, price_agrees, no_funding_cluster), so
# requiring 2 was "at least a couple of independent things agree" out
# of a small pool. Now-reliable holder intelligence adds two more
# genuinely independent checks (holder_data_verified,
# dev_holding_confirmed_low — see pump_radar.analyze_candidate's
# `confirmations` dict), so the same "couple of things agree" bar
# should scale with the pool of things that CAN agree, not stay fixed.
# Still safe for thin-data candidates: `required_confirmations` in
# compute_confidence() is capped at `checked_count`, so a brand-new
# token with only 1-2 resolvable checks is still held to "all of the
# checks that could run", never an impossible fixed floor.
MIN_CONFIRMATIONS_REQUIRED = 3

# Weighting inside the new evidence-only confidence formula (Phase 2):
# 70% "of the checks that resolved, how many came back positively
# confirmed" (evidence QUALITY), 30% "how much of the full available
# check pool actually resolved at all" (evidence BREADTH — a token
# verified by 6/7 checks is more trustworthy than one verified by 2/2,
# even though both could show 100% agreement). Neither term touches the
# conviction score.
CONFIDENCE_QUALITY_WEIGHT = 0.70
CONFIDENCE_BREADTH_WEIGHT = 0.30

# ---------------------------------------------------------------------
# Signal Tiers (Signal Engine re-evaluation)
#
# Keyed on `final_score` (base_score + capped bonuses), consistent with
# every existing numeric gate in this module:
#   - REJECT floor (<65) == HARD_FLOOR_CUTOFF, the absolute score floor
#     below which `eligible` is False and a candidate is never scored
#     as sendable, full stop.
#   - SUB-FLOOR (65-69) == candidates that cleared HARD_FLOOR_CUTOFF
#     (eligible=True, so they count toward quota.record_qualifying_
#     candidate()'s daily-supply signal) but sit below DYNAMIC_FLOOR
#     (70) — quota.maybe_adjust_cutoff() never lowers the live cutoff
#     past 70, so this band is tracked but never actually alerted.
#   - MARGINAL (70-79) == only ever alerted when the quota governor has
#     lowered today's live cutoff into this band because qualifying
#     supply has been thin for LOOKBACK_DAYS running (quota.py). This is
#     the system explicitly trading a LITTLE selectivity for volume on a
#     quiet day, never the reverse.
#   - WATCHLIST (80-84) == quota.DEFAULT_CUTOFF, the standard "always
#     sendable, no quota exception needed" bar.
#   - HIGH CONVICTION / ELITE / LEGENDARY (85+) == meaningfully above
#     the standard bar. Given the observed production base_score range
#     (~45-77 pre-bonus) plus a bonus band capped at MAX_MULTIPLIER
#     (10), reaching 90+ requires both an unusually strong on-chain
#     base AND real corroboration (smart money/whale, narrative,
#     graduation heuristic) stacking on top of it — these tiers are
#     meant to be rare by construction, not a target to grade-inflate
#     toward. See CHANGELOG/deliverable notes for the score-weighting
#     changes that determined this range is now achievable honestly
#     (real holder data replacing neutral placeholders) rather than by
#     loosening any threshold.
# ---------------------------------------------------------------------
TIER_LABELS = (
    (95, "🌟 LEGENDARY"),
    (90, "💎 ELITE"),
    (85, "🟢 HIGH CONVICTION"),
    (80, "🟡 WATCHLIST"),
    (70, "🔵 MARGINAL (quota-gated)"),
    (65, "⚪ SUB-FLOOR (tracked only)"),
    (0, "❌ REJECT"),
)


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return default


def effective_market_cap(data: dict) -> float:
    """
    Single source of truth for "which market-cap figure counts" — used
    by passes_mc_liquidity_gate(), the alert-card renderer, and
    candidate_matches_filters() alike, so there is exactly one place
    that decides this instead of three call sites each doing their own
    market_cap-or-fdv fallback (which can silently disagree when a
    field is a falsy-looking non-empty string like "0" or "N/A").

    DexScreener can report `market_cap` (circulating) and `fdv` (fully
    diluted) as genuinely different numbers for a freshly-migrated
    Pump.fun/PumpSwap pool, before its circulating-supply figure has
    caught up with the new AMM pool. Rather than trusting whichever
    field happens to be nonzero first, when BOTH are present and parse
    to a nonzero number we take the smaller (more conservative) of the
    two, so a temporarily-inflated field can never mask a lower, more
    accurate one. When only one is available, use it.
    """
    mc = _to_float(data.get("market_cap"))
    fdv = _to_float(data.get("fdv"))
    if mc > 0 and fdv > 0:
        return min(mc, fdv)
    return mc or fdv


def _is_on(value) -> bool:
    return str(value).lower() in {"1", "true", "yes", "enabled"}


def _age_hours(pair_created_ms) -> float:
    try:
        return (time.time() - (int(pair_created_ms) / 1000)) / 3600
    except Exception:
        return 9999.0


def _tier_for(score: float) -> str:
    for threshold, label in TIER_LABELS:
        if score >= threshold:
            return label
    return TIER_LABELS[-1][1]


# ---------------------------------------------------------------------
# 2.2 — Automatic hard rejects, regardless of volume/hype
# ---------------------------------------------------------------------
def hard_reject_reasons(
    data: dict,
    sec: dict | None,
    holder_analysis: dict | None,
    contract: str,
) -> list[str]:
    """
    Returns a list of hard-reject reasons. A non-empty list means the
    candidate must never be scored or sent, no matter how strong the
    rest of its profile is (Blueprint Section 2.2).
    """
    reasons: list[str] = []
    sec = sec or {}
    holder_analysis = holder_analysis or {}

    if _is_on(sec.get("is_honeypot")):
        reasons.append("Honeypot")
    if _is_on(sec.get("cannot_sell_all")) or _is_on(sec.get("cannot_buy")):
        reasons.append("Cannot sell/buy (honeypot-like)")
    if _is_on(sec.get("is_blacklisted")):
        reasons.append("Blacklisted")
    if _is_on(sec.get("hidden_owner")):
        reasons.append("Hidden owner")

    # Active, un-renounced mint/freeze authority — treated as a hard
    # reject by default (Blueprint 2.2 notes a verified-team exception
    # exists in principle, but this codebase has no disclosed-vesting
    # verification source, so we don't special-case it here).
    if _is_on(sec.get("mintable")):
        reasons.append("Mint authority not renounced")
    if _is_on(sec.get("freezable")):
        reasons.append("Freeze authority active")

    top_holder_pct = _to_float(
        holder_analysis.get("top_holder_pct")
        if holder_analysis.get("top_holder_pct") is not None
        else sec.get("top_holder_percent")
    )
    if top_holder_pct >= 30:
        reasons.append(f"Single wallet holds {top_holder_pct:.1f}% outside LP")

    # Phase 3.1: balance-similarity clustering is evidence, not proof, of
    # Sybil/bundle coordination — see evaluate_sybil_bundle_risk() above
    # for the full tiering. This only ever contributes a reason at the
    # severe tier; moderate concentration is left to
    # _score_holder_distribution()'s point penalty and to the funding-
    # relationship-based confirmation gate later in the pipeline
    # (risk_engine.evaluate_verified_red_flags).
    sybil_evidence = evaluate_sybil_bundle_risk(holder_analysis)
    reasons.extend(sybil_evidence["reasons"])

    # Exit-liquidity setup: the deployer/creator wallet itself holding a
    # large share is a distinct risk from general top-holder concentration
    # above — it specifically means whoever controls the contract (mint/
    # freeze authority, upgrade rights) is also sitting on a stack large
    # enough to dump into the liquidity retail buyers are providing. Uses
    # the real per-wallet balance from get_holder_analysis() (dev_address
    # resolved from GoPlus's creator_address), not an estimate — only
    # evaluated when that wallet was actually identified.
    dev_holding_pct = holder_analysis.get("dev_holding_pct")
    if dev_holding_pct is not None and dev_holding_pct >= DEV_HOLDING_REJECT_PCT:
        reasons.append(f"Deployer wallet holds {dev_holding_pct:.1f}% — exit-liquidity risk")

    # NOTE: the actual MC/liquidity/LP-ratio standard lives in
    # passes_mc_liquidity_gate() above and is enforced early in
    # pump_radar.analyze_candidate(), before this function ever runs —
    # a candidate that reaches hard_reject_reasons() has already
    # cleared it. This is just a defensive last-resort catch for a
    # caller that invokes hard_reject_reasons() directly (e.g. a test)
    # without going through that gate first; it intentionally mirrors
    # the SAME standard rather than a separate, looser one, so there is
    # only ever one definition of "healthy MC/liquidity" in this
    # codebase.
    mc_liq_ok, mc_liq_reason = passes_mc_liquidity_gate(data)
    if not mc_liq_ok:
        reasons.append(mc_liq_reason)

    return reasons


# ---------------------------------------------------------------------
# 1.3 — Category scoring
# ---------------------------------------------------------------------
def _score_liquidity_integrity(data: dict, contract: str) -> tuple[float, list[str]]:
    notes = []
    liq = _to_float(data.get("liquidity"))
    mc = effective_market_cap(data)

    lock_score = estimate_liquidity_lock_score(data, contract)  # 0..1
    points = lock_score * 15.0
    if lock_score >= 0.75:
        notes.append("Strong liquidity/lock profile")
    elif lock_score >= 0.4:
        # Explainability fix: this band was previously silent (points
        # awarded, no note) — every category should say why it scored
        # what it scored, not just the top band. Softer language on
        # purpose: this is partial credit, not a clean pass.
        notes.append("Moderate liquidity/lock profile")

    if mc > 0:
        liq_mc_ratio = (liq / mc) * 100
        if liq_mc_ratio >= 8:
            points += 10
            notes.append("Healthy liq/mcap ratio")
        elif liq_mc_ratio >= 4:
            points += 10 * ((liq_mc_ratio - 4) / 4)
            notes.append("Improving liq/mcap ratio")
        # below 4% -> 0 additional points, consistent with Blueprint 1.3

    return min(points, 25.0), notes


def _score_holder_distribution(holder_analysis: dict, sec: dict, age_hours: float) -> tuple[float, list[str]]:
    notes = []
    points = 0.0

    if holder_analysis.get("holder_analysis_status") == "unavailable_early_token":
        # RPC succeeded but this Pump.fun mint is too new to expose holder
        # accounts yet (services/holders.py). Scored on GoPlus-fallback
        # concentration data where available, not hard-rejected for it.
        notes.append("Holder data unavailable (early Pump.fun token) — partial scoring")

    # Bug fix (2026-08-15): missing concentration data used to fall
    # through _to_float(None) -> 0.0, which this scorer then read as
    # "0% concentration" and rewarded with the FULL 10 points — i.e. an
    # unverified token was scored as if it were provably the safest
    # possible token on this dimension. That silently inflated scores
    # (and therefore risk) for exactly the candidates we know the least
    # about, which is backwards. Missing data must now be neutral (0
    # points, no claim either way) instead of defaulting to "best case".
    top1_raw = holder_analysis.get("top_holder_pct")
    if top1_raw is None:
        top1_raw = sec.get("top_holder_percent")
    if top1_raw is not None:
        top1 = _to_float(top1_raw)
        if top1 < 10:
            points += 10
            notes.append("Low top-holder concentration")
        elif top1 < 20:
            points += 10 * ((20 - top1) / 10)
            notes.append("Moderate top-holder concentration")
    else:
        notes.append("Top-holder concentration unverified — no points awarded")

    top10_raw = holder_analysis.get("top10_pct")
    if top10_raw is None:
        top10_raw = sec.get("top_10_holder_percent")
    if top10_raw is not None:
        top10 = _to_float(top10_raw)
        if top10 < 25:
            points += 10
            notes.append("Well-distributed top-10 holders")
        elif top10 < 40:
            points += 10 * ((40 - top10) / 15)
            notes.append("Improving top-10 distribution")
    else:
        notes.append("Top-10 concentration unverified — no points awarded")

    # Holder growth rate proxy: without a stored historical snapshot per
    # candidate, we approximate "growing steadily" with holders-per-hour
    # velocity for young tokens. This is a documented approximation, not
    # a literal 15-minute-bucketed growth series (Section 1.1 #7) — the
    # backtesting harness in Section 3.1 is where this should be
    # validated/retuned once historical candidate logs exist.
    holders = _to_float(holder_analysis.get("total_holders"))
    if holders > 0 and age_hours > 0:
        velocity = holders / max(age_hours, 0.25)
        # Phase 2 calibration: this was a hard two-step function (0 / 3 /
        # 5 points at the 15 and 40 holders/hr cliffs), which is exactly
        # the kind of threshold that produces artificial score clustering
        # — a token at velocity=39 and one at velocity=14 score identically
        # to a token at velocity=0, and a token at 41 scores identically to
        # one at 1000. Replaced with a continuous sqrt-scaled curve over
        # the same 0-40/hr range and the same 0-5 point cap, so growth
        # quality is reflected smoothly instead of only at two cliff
        # edges. sqrt (rather than linear) still gives most of the credit
        # for reaching a solid pace early, with diminishing extra credit
        # after that, instead of a flat reward/no-reward split.
        velocity_score = min(1.0, (velocity / 40.0) ** 0.5) * 5.0
        points += velocity_score
        if velocity >= 40:
            notes.append("Fast holder growth")
        elif velocity >= 15:
            notes.append("Stable holder growth")
        elif velocity > 0:
            notes.append("Early holder growth")

    return min(points, 25.0), notes


def _score_momentum_quality(data: dict, holder_analysis: dict) -> tuple[float, list[str]]:
    notes = []
    points = 0.0

    liq = _to_float(data.get("liquidity"))
    mc = effective_market_cap(data)
    vol_1h = _to_float(data.get("volume_1h"))
    buys = _to_float(data.get("txns_1h_buys"))
    sells = _to_float(data.get("txns_1h_sells"))
    total_tx = buys + sells
    buy_ratio = (buys / total_tx) if total_tx > 0 else 0.5

    if total_tx >= 6:
        if buy_ratio >= 0.60:
            points += 12
            notes.append("Sustained buy pressure")
        elif buy_ratio >= 0.50:
            points += 12 * ((buy_ratio - 0.50) / 0.10)
            notes.append("Buy pressure trending positive")

    if mc > 0:
        vol_mc_ratio = vol_1h / mc
        if 0.3 <= vol_mc_ratio <= 2.0:
            points += 8
            notes.append("Healthy volume/mcap band")
        elif vol_mc_ratio > 2.0:
            # Phase 2 calibration: this used to drop straight to 0 the
            # instant the ratio crossed 2.0x — a token at 2.01x scored
            # identically to one with almost no trading at all, even
            # though "too hot" and "no volume" are opposite conditions.
            # The actual manufactured-volume risk is already handled
            # below as its own dedicated multiplicative penalty
            # (estimate_fake_volume_ratio / estimate_wash_trading_risk,
            # which can zero out the whole momentum category on real
            # evidence of wash trading) — so this base-credit cliff was
            # double-penalizing "hot" volume once via the cliff and again
            # via the wash-risk multiplier whenever wash risk was also
            # elevated, while a token that was merely hot but NOT wash-
            # flagged got needlessly zeroed here for no evidenced reason.
            # Replaced with a gentle decay back toward 0 as the ratio
            # keeps climbing, so a token at 2.1x still reads as "still
            # plausibly healthy, slightly over the ideal band" rather
            # than "identical to zero volume", while the actual fraud
            # signal continues to live in the dedicated risk multiplier.
            decay = max(0.0, 1.0 - ((vol_mc_ratio - 2.0) / 3.0))
            points += 8 * decay
            if decay > 0:
                notes.append("Elevated but plausible volume/mcap ratio")
        elif vol_mc_ratio > 0:
            points += 8 * (vol_mc_ratio / 0.3)
            notes.append("Building volume/mcap ratio")

    # Price structure proxy: a move backed by a longer-window uptrend
    # (6h/24h agreeing with 1h) reads as "base before breakout" rather
    # than an isolated vertical wick (Section 1.1 #10). A true
    # base-detection heuristic needs OHLC candles this codebase doesn't
    # currently pull — this is the closest available proxy from the
    # existing DexScreener fields.
    p1h = _to_float(data.get("price_change_1h"))
    p6h = _to_float(data.get("price_change_6h"))
    p24h = _to_float(data.get("price_change_24h"))
    if p1h > 0 and p6h > 0 and p24h > 0:
        points += 10
        notes.append("Base-building move, not a single wick")
    elif p1h > 0 and (p6h > 0 or p24h > 0):
        points += 6
        notes.append("Strong momentum, partial trend agreement")
    elif p1h > 0:
        points += 2
        notes.append("Early momentum, unconfirmed by longer trend")

    # Signal Quality & Alert Qualification Upgrade: velocity/acceleration.
    # The trend-agreement block just above only checks DIRECTION (are
    # 1h/6h/24h all positive), which treats a token that has been rising
    # at a steady background pace for a day identically to one whose move
    # is genuinely speeding up right now -- both can show identical
    # price_change_1h/6h/24h signs. Comparing each window's own per-hour
    # RATE (not just its sign) distinguishes acceleration from a merely-
    # continuing trend: a 1h pace outrunning the 6h pace, which in turn
    # outruns the 24h pace, is real evidence the move is intensifying,
    # not just persisting. Additive-only (never subtracts) and bounded,
    # consistent with every other bonus in this category -- a token with
    # no acceleration signal simply doesn't earn this credit, it isn't
    # penalized for its absence.
    if p1h > 0:
        rate_1h = p1h
        rate_6h = p6h / 6.0
        rate_24h = p24h / 24.0
        if rate_1h > rate_6h > rate_24h and rate_6h > 0:
            points += 6
            notes.append("Accelerating momentum (1h pace outrunning 6h/24h pace)")
        elif rate_1h > rate_6h and rate_6h > 0:
            points += 3
            notes.append("Momentum accelerating vs 6h pace")
        elif rate_6h <= 0 and rate_1h > 0:
            # Trend just turned positive this hour after a flat/negative
            # 6h window -- an early inflection, not yet a proven multi-
            # window acceleration, so a smaller nudge than the cases above.
            points += 1.5
            notes.append("Early inflection vs 6h pace")

    # Entry-timing / already-extended penalty: a 1h move this large means
    # the "before major momentum begins" window has likely already
    # passed — the token is more likely to be chased/late than a fresh
    # setup, even though the raw buy-pressure and volume numbers above
    # can look identical to a genuinely early move. This discounts the
    # price-structure credit just awarded above rather than the whole
    # category, since strong liquidity/wallet/holder quality can still
    # justify a smaller position on a continuing move.
    if p1h >= ALREADY_EXTENDED_1H_PCT:
        points *= 0.5
        notes.append(f"Already extended (+{p1h:.0f}% in 1h) — late-entry risk")

    # Manufactured-momentum penalty (Section 1.4) — suppresses this
    # category heavily even if raw volume/txn numbers look great.
    fake_vol_risk = estimate_fake_volume_ratio(data)
    wash_risk = estimate_wash_trading_risk(data)
    manufactured = max(fake_vol_risk, wash_risk)
    if manufactured > 0:
        points *= max(0.0, 1.0 - manufactured)
        if manufactured >= 0.35:
            notes.append("Momentum partially discounted (wash/fake-volume risk)")

    return min(max(points, 0.0), 30.0), notes


def _score_wallet_behavior(data: dict, sec: dict, holder_analysis: dict, holders: int | None, contract: str) -> tuple[float, list[str]]:
    notes = []
    points = 0.0

    # Bundle detection needs real holder accounts to cluster wallets by
    # balance — when holder_analysis_status is "unavailable_early_token"
    # (services/holders.py), bundle_pct is a placeholder 0.0, not a
    # genuine "no cluster found" result. Awarding the full 10 points here
    # would score every early Pump.fun token as provably bundle-free when
    # we actually have zero visibility into it — the same "unknown
    # treated as passing" problem this scoring model explicitly avoids
    # elsewhere (see sniper_risk note below). Stay neutral (0 points, no
    # note) instead of rewarding missing data.
    holder_data_is_real = holder_analysis.get("holder_analysis_status") == "ok"
    # Fixed: this used to be a deny-list (!= "unavailable_early_token"), which
    # silently treated ANY other non-"ok" status -- including
    # "unavailable_provider_degraded" (pump_radar's non-blocking holder
    # fallback when every holder provider fails) -- as real, verified data.
    # An allow-list is the forward-compatible fix: only "ok" (a genuine,
    # fully-resolved holder snapshot) earns bundle-clean credit or the
    # "verified" bonus below.
    if holder_data_is_real:
        bundle_pct = _to_float(holder_analysis.get("bundle_pct"))
        if bundle_pct < 10:
            points += 10
            notes.append("No meaningful bundle cluster")
        elif bundle_pct < 40:
            points += 10 * ((40 - bundle_pct) / 30)
            notes.append("Partial bundle exposure")

    # No live "insider sold in last 30m" feed is wired up in this
    # codebase (would need a streamed tx-history subscription). Sniper
    # concentration (tx-to-holder ratio) is used as the closest proxy
    # available today and is scored as a penalty rather than a reject,
    # matching the "unknown -> neutral" principle from Section 1.3.
    sniper_risk = estimate_sniper_wallet_risk(data, holders)
    points += 5 * max(0.0, 1.0 - sniper_risk)
    if sniper_risk >= 0.45:
        notes.append("Elevated sniper-wallet activity")

    # Exit-liquidity soft penalty: dev holding is below the
    # DEV_HOLDING_REJECT_PCT hard-reject line (see hard_reject_reasons)
    # but still elevated enough to discount conviction rather than pass
    # silently.
    dev_holding_pct = holder_analysis.get("dev_holding_pct")
    if dev_holding_pct is not None and dev_holding_pct >= DEV_HOLDING_WARN_PCT:
        points -= 3.0
        notes.append(f"Elevated deployer holding ({dev_holding_pct:.1f}%)")
    elif dev_holding_pct is not None and dev_holding_pct < DEV_HOLDING_CONFIRMED_CLEAN_PCT:
        # Signal Engine re-evaluation: this used to be scored identically
        # to "we don't know the dev wallet" (both = 0 extra points).
        # get_holder_analysis() now reliably resolves a real dev_pct
        # whenever dev_address is known, including a genuine 0% (dev
        # wallet fully cashed out or never held a meaningful stack) —
        # that's real, verified signal, not a guess, so it earns real
        # credit instead of being thrown away. Bounded low (max +2) so
        # it can nudge a candidate, never single-handedly carry one.
        points += DEV_HOLDING_CLEAN_BONUS
        notes.append("Low developer risk (verified negligible dev holding)")

    # Deployer prior-launch history (services/deployer_history.py) is
    # deliberately NOT looked up here — it's an expensive Helius call
    # only run once per candidate, after this cheap pre-score pass, and
    # is enforced separately as a hard reject/warning gate in
    # pump_radar.py via risk_engine.evaluate_verified_red_flags().
    #
    # Signal Engine re-evaluation: this used to be an UNCONDITIONAL +2.5
    # given to every candidate regardless of data quality — not "neutral
    # (0)" as the old comment intended, but a flat bonus baked into every
    # score, which is grade inflation with no signal behind it (it was
    # silently 2.5 of the 65-point HARD_FLOOR_CUTOFF on every single
    # candidate, real-data or not). Now that get_holder_analysis() is
    # reliable, only award it when this candidate actually has a real,
    # non-placeholder holder snapshot to show for it — i.e. "we have
    # genuine wallet-behavior visibility into this token" is itself a
    # small, honest, explainable positive, whereas a brand-new token
    # scored on placeholder data has not earned it.
    if holder_data_is_real:
        points += 2.5
        notes.append("Verified wallet/holder data (not early-token placeholder)")

    return min(max(points, 0.0), 20.0), notes


def _narrative_social_multiplier(data: dict, base_score: float) -> tuple[float, list[str]]:
    """
    ±10 bonus, additive on top of an already-qualifying on-chain score —
    never a substitute for it (Blueprint 1.2, 1.3). Only evaluated when
    the base score is already reasonably close to qualifying, so social
    buzz can never be the thing that drags a weak token into contention.

    NOTE: KOL-buy corroboration (the other half of the blueprint's
    multiplier — "narrative hot AND a tracked KOL wallet bought in the
    last hour") isn't wired in yet: services/kol_tracker.py tracks KOL
    wallets globally via provider sync, but there's no per-token
    "did a tracked KOL buy *this* contract in the last hour" lookup in
    the existing data model. Wiring that in is Section 5 item #7
    (nice-to-have, low priority) — flagged here rather than faked.
    """
    notes = []
    if base_score < HARD_FLOOR_CUTOFF:
        return 0.0, notes

    name = data.get("name", "") or ""
    symbol = data.get("symbol", "") or ""
    narratives = [n for n in classify_token(name, symbol) if n != "OTHER"]

    if not narratives:
        return 0.0, notes

    # Phase 2 calibration: lowered 6.0 -> 4.0. Of the three bonus
    # categories (narrative, smart-money/whale, graduation heuristic),
    # this is pure text classification against a token's name/symbol —
    # the weakest evidence of the three, since it has zero connection to
    # actual on-chain behavior. Per Blueprint 1.2 "strong on-chain
    # evidence over short-term hype", it should carry the smallest of the
    # three bonus ceilings, not the largest (previously 6.0 vs smart-
    # money's 5.0) or tied for largest.
    bonus = min(4.0, MAX_MULTIPLIER)
    notes.append(f"{'/'.join(narratives[:2])} narrative tailwind")
    return bonus, notes


def _smart_money_whale_bonus(
    base_score: float,
    smart_money: list | None,
    whale_holders: list | None,
) -> tuple[float, list[str]]:
    """
    Smart Money weighting + Whale accumulation, as an additive bonus —
    same non-negotiable rule as _narrative_social_multiplier: only ever
    applied on top of an already-qualifying on-chain score, never able to
    rescue a token that failed on liquidity/holders/momentum/wallet
    quality. Both `smart_money` and `whale_holders` come from
    services/kol_tracker.get_matching_kol_holders() and
    services/whale_tracker.get_matching_tracked_whales(), which only ever
    return wallets that GENUINELY hold the token right now per the same
    Helius snapshot used for the rest of the card — never an estimate or
    a fabricated count, so this bonus reflects real, verified positions.
    """
    notes = []
    if base_score < HARD_FLOOR_CUTOFF:
        return 0.0, notes

    bonus = 0.0
    smart_money = smart_money or []
    whale_holders = whale_holders or []

    if smart_money:
        n = len(smart_money)
        smart_bonus = min(SMART_MONEY_MAX_BONUS, 2.0 + (n - 1) * 1.5)
        bonus += smart_bonus
        notes.append(f"{n} tracked Smart Money wallet{'s' if n != 1 else ''} holding")

    if whale_holders:
        n = len(whale_holders)
        whale_bonus = min(WHALE_MAX_BONUS, 1.5 + (n - 1) * 1.0)
        bonus += whale_bonus
        notes.append(f"{n} tracked whale{'s' if n != 1 else ''} accumulating")

    return min(bonus, SMART_MONEY_MAX_BONUS + WHALE_MAX_BONUS), notes


def _estimate_graduation_probability(data: dict, holder_analysis: dict, contract: str) -> tuple[float, float, list[str]]:
    """
    Pump.fun graduation-probability heuristic. Returns
    (probability_0_to_1, bonus_points, notes).

    IMPORTANT — this is an explicit, documented heuristic proxy, NOT a
    trained/calibrated model. This codebase does not log historical
    graduation outcomes anywhere (no table of "candidate at time T" ->
    "did it graduate to Raydium within N hours"), so there is no data to
    fit or validate a real probability estimate against. Building an
    actual calibrated model is a Testing/Accuracy Report recommendation
    (needs a backtesting harness first) — shipping a confident-looking
    percentage without that would be fabricating precision. What this
    function does instead: combines a few directionally-reasonable,
    already-available signals (progress toward the ~$69k typical
    bonding-curve graduation market cap, holder growth velocity, and
    volume/mcap intensity) into a bounded small bonus, capped low
    (max 5 points) specifically because of that uncertainty.
    """
    notes = []
    mc = effective_market_cap(data)
    if not contract.lower().endswith("pump"):
        return 0.0, 0.0, notes

    # Typical Pump.fun bonding-curve graduation threshold is ~$69k mcap.
    # Progress toward it is the single most direct available proxy.
    GRAD_TARGET_MC = 69000.0
    progress = min(1.0, mc / GRAD_TARGET_MC) if mc > 0 else 0.0

    holders = _to_float(holder_analysis.get("total_holders"))
    age_hours = _age_hours(data.get("pair_created"))
    velocity = (holders / max(age_hours, 0.25)) if holders > 0 and age_hours > 0 else 0.0
    velocity_score = min(1.0, velocity / 40.0)

    vol_1h = _to_float(data.get("volume_1h"))
    vol_mc_score = min(1.0, (vol_1h / mc)) if mc > 0 else 0.0

    probability = round(min(1.0, 0.5 * progress + 0.3 * velocity_score + 0.2 * vol_mc_score), 2)

    # Phase 2 calibration: bonus band lowered (5.0->4.0 / 2.5->2.0). This
    # is explicitly documented above as an unvalidated heuristic proxy,
    # not a trained/calibrated model — it should never carry as much
    # weight as the smart-money/whale bonus, which reflects real,
    # verified wallet positions. Kept as its own distinct, capped-low
    # channel rather than removed, consistent with "do not blindly
    # increase scores": this only re-ranks it below (not equal to)
    # genuine on-chain corroboration.
    bonus = 0.0
    if probability >= 0.6:
        bonus = 4.0
        notes.append(f"Graduation-probability proxy: {probability:.0%} (heuristic, not a calibrated model)")
    elif probability >= 0.35:
        bonus = 2.0

    return probability, bonus, notes


def _estimate_pump_probability(
    base_score: float,
    liq_pts: float,
    momentum_pts: float,
    holder_pts: float,
    smart_whale_bonus: float,
    graduation_probability: float,
) -> tuple[float, list[str]]:
    """
    Signal Engine re-evaluation — "Pump Probability" deliverable.

    Distinct from _estimate_graduation_probability() above, which is a
    narrow Pump.fun bonding-curve-specific heuristic (progress toward the
    ~$69k graduation mcap) and only ever applies to `*pump`-suffixed
    contracts. This is a general 0-100% read on "how likely is this
    candidate to see continued upward momentum from here", built from
    signals already computed elsewhere in this pass:
      - momentum quality (already-verified buy pressure / volume /
        trend-agreement, net of the wash-trading and already-extended
        discounts applied in _score_momentum_quality)
      - liquidity/LP integrity (a pump that can't hold isn't a pump)
      - holder distribution (concentrated supply caps genuine upside)
      - smart-money/whale corroboration (real, verified positions)
      - the Pump.fun graduation heuristic, folded in as ONE input among
        several rather than the whole signal, for contracts it applies to

    Deliberately NOT wired into final_score or the bonus/multiplier
    stack — it's a reporting/explainability field (Blueprint "explainable
    decisions"), not a second, uncapped scoring channel that could
    undermine the MAX_MULTIPLIER cap fix. Like graduation_probability,
    this is an explicit, bounded heuristic proxy over already-available
    inputs, not a trained/calibrated model — labeled as such wherever
    it's shown, per this codebase's existing "don't fabricate precision"
    convention.
    """
    notes = []
    if base_score < HARD_FLOOR_CUTOFF:
        return 0.0, notes

    # Normalize each contributing category to its own share of a 0-1
    # scale before blending, so no single category's raw point cap
    # (25 vs 30 vs 20) silently dominates the blend.
    momentum_norm = min(1.0, momentum_pts / 30.0)
    liq_norm = min(1.0, liq_pts / 25.0)
    holder_norm = min(1.0, holder_pts / 25.0)
    smart_whale_norm = min(1.0, smart_whale_bonus / (SMART_MONEY_MAX_BONUS + WHALE_MAX_BONUS))

    probability = (
        0.40 * momentum_norm
        + 0.20 * liq_norm
        + 0.15 * holder_norm
        + 0.15 * smart_whale_norm
        + 0.10 * graduation_probability
    )
    probability = round(min(1.0, max(0.0, probability)) * 100, 1)

    if probability >= 70:
        notes.append(f"Pump Probability: {probability:.0f}% (heuristic)")

    return probability, notes


def score_candidate(
    data: dict,
    sec: dict | None,
    holder_analysis: dict | None,
    holders: int | None,
    contract: str,
    smart_money: list | None = None,
    whale_holders: list | None = None,
) -> dict:
    """
    Full conviction scoring pass for a candidate that has already
    survived hard_reject_reasons(). Returns a dict shaped to stay
    backward compatible with the old calculate_pump_fun_score() output
    (score / verdict / reasons) so callers elsewhere in pump_radar.py
    (mark_alerted, send_pump_card, calculate_potential_score) don't need
    to change, plus the extra fields the new pipeline needs.

    `smart_money` / `whale_holders` are optional and backward
    compatible — any existing caller that doesn't pass them scores
    exactly as before (bonus simply evaluates to 0). See
    _smart_money_whale_bonus() docstring for what they represent.
    """
    sec = sec or {}
    holder_analysis = holder_analysis or {}
    age_hours = _age_hours(data.get("pair_created"))

    liq_pts, liq_notes = _score_liquidity_integrity(data, contract)
    holder_pts, holder_notes = _score_holder_distribution(holder_analysis, sec, age_hours)
    momentum_pts, momentum_notes = _score_momentum_quality(data, holder_analysis)
    wallet_pts, wallet_notes = _score_wallet_behavior(data, sec, holder_analysis, holders, contract)

    base_score = liq_pts + holder_pts + momentum_pts + wallet_pts
    multiplier, social_notes = _narrative_social_multiplier(data, base_score)
    smart_whale_bonus, smart_whale_notes = _smart_money_whale_bonus(base_score, smart_money, whale_holders)
    grad_probability, grad_bonus, grad_notes = _estimate_graduation_probability(data, holder_analysis, contract)
    grad_bonus = grad_bonus if base_score >= HARD_FLOOR_CUTOFF else 0.0
    pump_probability, pump_prob_notes = _estimate_pump_probability(
        base_score, liq_pts, momentum_pts, holder_pts, smart_whale_bonus, grad_probability,
    )

    # Combined bonus cap (fix: MAX_MULTIPLIER was declared but never
    # actually enforced across categories — narrative/social (max 6),
    # smart-money/whale (max 8), and graduation heuristic (max 5) could
    # previously stack up to +19 total, letting a barely-eligible
    # base_score of 65 reach a final_score of 84 on hype/heuristics
    # alone, well past SEND_CUTOFF, with no extra on-chain quality
    # behind it. Every category's own docstring says "additive-only,
    # can never rescue a weak token" — this is what actually makes that
    # true in aggregate, not just per-category.
    total_bonus = min(multiplier + smart_whale_bonus + grad_bonus, MAX_MULTIPLIER)
    final_score = min(100.0, base_score + total_bonus)

    # Blueprint 1.3 absolute floor: a base_score below HARD_FLOOR_CUTOFF
    # is never sendable, regardless of narrative/social multiplier. This
    # is the ONLY hard eligibility gate left in this function on purpose
    # -- the actual send/no-send bar (which floats between
    # quota_governor.DYNAMIC_FLOOR and DEFAULT_CUTOFF, i.e. 70-80) is
    # applied once by the caller (pump_radar.analyze_candidate) against
    # `final_score`, using the live dynamic cutoff from
    # services/quota_governor.get_current_cutoff(). Do not reintroduce a
    # second fixed >=80 requirement here -- see the note above this
    # block for why that silently breaks the whole Signal Engine.
    eligible = base_score >= HARD_FLOOR_CUTOFF

    all_notes = liq_notes + holder_notes + momentum_notes + wallet_notes + social_notes + smart_whale_notes + grad_notes + pump_prob_notes
    # Most relevant few, for the alert's "why" line (Blueprint 4.3).
    reasons = all_notes[:4] if all_notes else ["Cleared all quality gates"]

    breakdown = {
        "liquidity_lp_integrity": round(liq_pts, 1),
        "holder_distribution": round(holder_pts, 1),
        "momentum_quality": round(momentum_pts, 1),
        "wallet_deployer_behavior": round(wallet_pts, 1),
        "narrative_social_multiplier": round(multiplier, 1),
        "smart_money_whale_bonus": round(smart_whale_bonus, 1),
        "graduation_probability": grad_probability,
        "graduation_bonus": round(grad_bonus, 1),
        "pump_probability": pump_probability,
        "total_bonus_applied": round(total_bonus, 1),
        "total_bonus_capped": (multiplier + smart_whale_bonus + grad_bonus) > MAX_MULTIPLIER,
    }

    return {
        # backward-compatible fields (old callers read pump["score"] etc.)
        "score": round(final_score),
        "verdict": _tier_for(final_score),
        "reasons": reasons,
        # new fields
        "base_score": round(base_score, 1),
        "final_score": round(final_score, 1),
        "multiplier": round(multiplier, 1),
        "tier": _tier_for(final_score),
        "eligible": eligible,
        "breakdown": breakdown,
        "smart_money_count": len(smart_money or []),
        "whale_count": len(whale_holders or []),
        "graduation_probability": grad_probability,
        "pump_probability": pump_probability,
    }


def compute_confidence(final_score: float, confirmations: dict) -> dict:
    """
    Signal Intelligence upgrade — Confidence scoring (separate gate from
    SEND_CUTOFF / the dynamic quota cutoff).

    `confirmations` is a dict of {check_name: bool | None} for every
    independently-verified signal available by the time this is called
    (meant to be invoked in pump_radar.analyze_candidate AFTER the real
    enrichment lookups — LP lock, funding clusters, deployer history,
    price agreement — complete, in addition to smart_money/whale from
    the scoring pass). True = positively confirmed good. False =
    confirmed bad (should already have been hard-rejected upstream by
    hard_reject_reasons()/evaluate_verified_red_flags() if it reaches
    reject-worthy severity — a False surviving to here just means
    "elevated but not reject-tier", e.g. a warning-band LP lock%).
    None = genuinely unknown/unverifiable, and per the "unknown -> don't
    penalize" convention used throughout this codebase, None does not
    count against confidence, it's simply excluded from the denominator.

    Phase 2 calibration — confidence formula rewritten from scratch.
    Previously confidence_score was 65% the token's own conviction
    score + 35% evidence agreement, which meant confidence was mostly
    measuring how BULLISH the token looked, not how trustworthy the
    evidence behind that score was — the opposite of what this gate is
    supposed to do (a token can score well on momentum/liquidity math off
    thin, mostly-unverified data; that should read as LOW confidence,
    not high). `final_score` is accepted for signature/logging
    compatibility but no longer contributes to the number at all.

    New formula blends two evidence-only terms:
      - QUALITY: of the checks that actually resolved, what fraction
        came back positively confirmed (agreement_fraction).
      - BREADTH: how much of the full available check pool
        (TOTAL_POSSIBLE_CHECKS) resolved at all, confirmed or not — a
        token verified by 6 of 7 checks is more trustworthy than one
        "verified" by 2 of 2, even if both show 100% agreement.
    confidence_score = 100 * (QUALITY_WEIGHT * agreement_fraction +
                               BREADTH_WEIGHT * coverage_fraction)

    Worked examples on the new 0-100 scale: 6/7 checks resolved, 5
    confirmed -> agreement 0.83, coverage 0.86 -> ~84 confidence. 3/7
    resolved, all 3 confirmed -> agreement 1.0, coverage 0.43 -> ~83
    confidence (high agreement, but capped by thin coverage — MIN_
    CONFIRMATIONS_REQUIRED's `checked_count`-capped floor is what
    actually stops that thin-data case at meets_bar, see below). 5/7
    resolved, 2 confirmed -> agreement 0.4, coverage 0.71 -> ~49
    confidence (correctly below MIN_CONFIDENCE_SCORE).
    """
    known = {k: v for k, v in confirmations.items() if v is not None}
    confirmed_count = sum(1 for v in known.values() if v is True)
    checked_count = len(known)

    agreement_fraction = (confirmed_count / checked_count) if checked_count else 0.0
    coverage_fraction = min(1.0, checked_count / TOTAL_POSSIBLE_CHECKS) if TOTAL_POSSIBLE_CHECKS else 0.0
    confidence_score = round(
        min(100.0, 100 * (
            CONFIDENCE_QUALITY_WEIGHT * agreement_fraction
            + CONFIDENCE_BREADTH_WEIGHT * coverage_fraction
        )),
        1,
    )

    # Required confirmations can never exceed how many checks were
    # actually resolvable (non-None) this pass. MIN_CONFIRMATIONS_REQUIRED
    # is the bar when enough independent data is available to meaningfully
    # apply "multiple conditions must agree" — but for a brand-new
    # candidate where most checks legitimately come back unverifiable
    # (no Raydium LP yet, no deployer history, no Jupiter route), holding
    # it to a floor higher than the number of checks that could even be
    # evaluated makes the gate impossible to pass regardless of quality.
    # That's the same unknown -> don't-penalize convention already applied
    # to every individual confirmation above; a fixed floor was silently
    # overriding it at the aggregate level. Candidates with fewer
    # resolvable checks still need ALL of them to agree; candidates with
    # more still need the full MIN_CONFIRMATIONS_REQUIRED to agree.
    required_confirmations = min(MIN_CONFIRMATIONS_REQUIRED, checked_count)

    meets_bar = (
        checked_count > 0
        and confidence_score >= MIN_CONFIDENCE_SCORE
        and confirmed_count >= required_confirmations
    )

    return {
        "confidence_score": confidence_score,
        "confirmed_count": confirmed_count,
        "checked_count": checked_count,
        # Signal Quality & Alert Qualification Upgrade: exposed so
        # qualification.evaluate_signal_readiness() can enforce the same
        # "multiple independent things must actually agree" floor without
        # recomputing it (single source of truth — the count and its
        # required-confirmations math live here and only here).
        "required_confirmations": required_confirmations,
        "meets_bar": meets_bar,
    }
