import logging

logger = logging.getLogger("AlphaPulse.RiskEngine")


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return default


def _is_on(value) -> bool:
    return str(value).lower() in {"1", "true", "yes", "enabled"}


# ---------------------------------------------------------------------
# Verified-data red flags (post-enrichment gate)
#
# These checks run on the *real* (non-estimated) data pulled from
# services/lp_lock_checker.py, services/funding_graph.py,
# services/deployer_history.py and services/jupiter_price.py.
#
# Previously this data was fetched by pump_radar.py for every candidate
# that survived hard_reject_reasons()/score_candidate(), but was only
# ever used to decorate the alert card text — a token could show
# "🚨 Funding Cluster: 5 top holders share a funding wallet" or a
# verified-unlocked LP right on the card and still be sent. This closes
# that gap: verified evidence of danger now blocks the send, the same
# way the estimated/inferred signals already do in hard_reject_reasons().
#
# Each source is treated as "unknown -> don't penalize" when it returns
# None/empty (couldn't verify), per the "never fabricate a positive or
# negative from an absence" convention already used throughout this
# codebase (lp_lock_checker.py, deployer_history.py docstrings).
# ---------------------------------------------------------------------
# --- Verified on-chain safety thresholds ---
# v4 fix (Bible §12, audit finding: "dead configuration"): v3 had TWO
# separate LP-lock thresholds that were supposed to be the same gate —
# a hardcoded hardcoded constant here (LP_LOCK_REJECT_BELOW, actually
# enforced) and a documented "mandatory, non-bypassable" env-configurable
# setting in config/settings.py (SIGNAL_MIN_LOCKED_LIQUIDITY_PCT) that was
# never imported or referenced anywhere and therefore enforced nothing.
# This consolidates to one source of truth: the settings value now IS
# the live threshold. This does NOT touch the `if real_lp_lock_pct is not
# None:` guard below — that guard is the fix for a *different*, earlier
# incident (a prior hard-reject-on-None version of this gate silently
# rejected nearly every candidate; see pump_radar.py's history comment).
# Only the number itself now has one real source instead of two.
from config.settings import SIGNAL_MIN_LOCKED_LIQUIDITY_PCT

LP_LOCK_REJECT_BELOW = SIGNAL_MIN_LOCKED_LIQUIDITY_PCT  # verified burn/lock % below this -> reject
LP_LOCK_WARN_BELOW = 80.0       # below this but above reject line -> warning
SERIAL_DEPLOYER_REJECT_AT = 5   # prior launches from this dev wallet -> reject
SERIAL_DEPLOYER_WARN_AT = 2
PRICE_MISMATCH_REJECT_PCT = 15.0  # matches jupiter_price.MISMATCH_THRESHOLD_PCT


def evaluate_verified_red_flags(
    real_lp_lock_pct: float | None,
    funding_clusters: dict | None,
    deployer_history: dict | None,
    price_check: dict | None,
    funding_cluster_min_size: int = 3,
) -> dict:
    """
    Final gate evaluated after the real (verified) enrichment lookups
    complete, in addition to the earlier estimate-based
    hard_reject_reasons() gate in conviction_scorer.py. Returns:
        {"reject": bool, "reasons": [...], "warnings": [...]}
    """
    reasons: list[str] = []
    warnings: list[str] = []

    if real_lp_lock_pct is not None:
        if real_lp_lock_pct < LP_LOCK_REJECT_BELOW:
            reasons.append(
                f"Verified LP burn/lock only {real_lp_lock_pct:.0f}% — rug risk"
            )
        elif real_lp_lock_pct < LP_LOCK_WARN_BELOW:
            warnings.append(f"LP burn/lock verified at {real_lp_lock_pct:.0f}%")

    fc = funding_clusters or {}
    largest_cluster = fc.get("largest_cluster_size", 0) or 0
    if largest_cluster >= funding_cluster_min_size:
        reasons.append(
            f"Funding-graph cluster: {largest_cluster} top holders share a single "
            "funding wallet — coordinated/insider control"
        )

    if deployer_history is not None:
        prior = deployer_history.get("prior_launches", 0) or 0
        if prior >= SERIAL_DEPLOYER_REJECT_AT:
            reasons.append(
                f"Deployer wallet has {prior} prior token launches — serial "
                "launch/farm pattern"
            )
        elif prior >= SERIAL_DEPLOYER_WARN_AT:
            warnings.append(f"Deployer wallet has {prior} prior token launches")

    pc = price_check or {}
    if pc.get("agrees") is False:
        mismatch = pc.get("mismatch_pct")
        if mismatch is not None and abs(mismatch) >= PRICE_MISMATCH_REJECT_PCT:
            reasons.append(
                f"DexScreener/Jupiter price mismatch ({mismatch:.1f}%) — possible "
                "stale or manipulated pair"
            )

    return {"reject": bool(reasons), "reasons": reasons, "warnings": warnings}


def estimate_fake_volume_ratio(data: dict) -> float:
    liq = _to_float(data.get("liquidity"))
    vol_1h = _to_float(data.get("volume_1h"))
    buys = _to_float(data.get("txns_1h_buys"))
    sells = _to_float(data.get("txns_1h_sells"))

    if liq <= 0 or vol_1h <= 0:
        return 0.0

    ratio = vol_1h / liq
    total_tx = buys + sells

    risk = 0.0

    if ratio >= 8:
        risk += 0.55
    elif ratio >= 5:
        risk += 0.35
    elif ratio >= 3:
        risk += 0.2

    if total_tx >= 100:
        buy_ratio = buys / total_tx if total_tx > 0 else 0.5
        if 0.47 <= buy_ratio <= 0.53:
            risk += 0.2

    return min(risk, 1.0)


def estimate_wash_trading_risk(data: dict) -> float:
    liq = _to_float(data.get("liquidity"))
    vol_1h = _to_float(data.get("volume_1h"))
    buys = _to_float(data.get("txns_1h_buys"))
    sells = _to_float(data.get("txns_1h_sells"))
    total_tx = buys + sells

    if total_tx < 40 or liq <= 0:
        return 0.0

    buy_ratio = buys / total_tx if total_tx > 0 else 0.5
    vol_liq = vol_1h / liq if liq > 0 else 0

    risk = 0.0

    if 0.48 <= buy_ratio <= 0.52:
        risk += 0.35

    if total_tx >= 150:
        risk += 0.2

    if vol_liq >= 5:
        risk += 0.25

    return min(risk, 1.0)


def estimate_sniper_wallet_risk(data: dict, holders: int | None) -> float:
    buys = _to_float(data.get("txns_1h_buys"))
    sells = _to_float(data.get("txns_1h_sells"))
    total_tx = buys + sells

    if total_tx <= 0 or not holders:
        return 0.0

    tx_to_holder = total_tx / max(holders, 1)

    risk = 0.0
    if tx_to_holder >= 4:
        risk += 0.45
    elif tx_to_holder >= 2.5:
        risk += 0.25
    elif tx_to_holder >= 1.5:
        risk += 0.1

    return min(risk, 1.0)


def estimate_bundle_wallet_risk(sec: dict | None) -> float:
    if not sec:
        return 0.0

    top_holder = _to_float(sec.get("top_holder_percent"))
    top_10 = _to_float(sec.get("top_10_holder_percent"))

    risk = 0.0

    if top_holder >= 25:
        risk += 0.45
    elif top_holder >= 18:
        risk += 0.25

    if top_10 >= 60:
        risk += 0.35
    elif top_10 >= 45:
        risk += 0.15

    return min(risk, 1.0)


def estimate_liquidity_lock_score(data: dict, contract: str) -> float:
    liq = _to_float(data.get("liquidity"))

    score = 0.25 if contract.lower().endswith("pump") else 0.15

    if liq >= 30000:
        score += 0.5
    elif liq >= 15000:
        score += 0.35
    elif liq >= 8000:
        score += 0.2

    return min(score, 1.0)


def evaluate_risk(data: dict, sec: dict | None, holders: int | None, contract: str) -> dict:
    """
    Revised risk gate:
    - still hard-reject obvious rugs
    - but no longer kills very fresh tokens for low early holders
    """
    reasons = []
    warnings = []

    liq = _to_float(data.get("liquidity"))
    mc = _to_float(data.get("market_cap")) or _to_float(data.get("fdv"))
    age_ms = data.get("pair_created")
    age_hours = 0.0
    if age_ms:
        try:
            import time
            age_hours = (time.time() - (int(age_ms) / 1000)) / 3600
        except Exception:
            age_hours = 0.0

    honeypot = _is_on(sec.get("is_honeypot")) if sec else False
    mintable = _is_on(sec.get("mintable")) if sec else False
    freezable = _is_on(sec.get("freezable")) if sec else False

    dev_pct = _to_float(sec.get("creator_percent")) if sec else 0.0
    top_holder_pct = _to_float(sec.get("top_holder_percent")) if sec else 0.0
    top10_pct = _to_float(sec.get("top_10_holder_percent")) if sec else 0.0

    fake_volume_ratio = estimate_fake_volume_ratio(data)
    wash_risk = estimate_wash_trading_risk(data)
    sniper_risk = estimate_sniper_wallet_risk(data, holders)
    bundle_risk = estimate_bundle_wallet_risk(sec)
    liquidity_lock_score = estimate_liquidity_lock_score(data, contract)

    # ---------------- HARD REJECTS ----------------
    if honeypot:
        reasons.append("Honeypot detected")
    if liq < 2500:
        reasons.append("Liquidity too low")
    if mc <= 0:
        reasons.append("Invalid market cap")
    if dev_pct >= 25:
        reasons.append(f"Developer allocation too high ({dev_pct:.1f}%)")
    if top10_pct >= 75:
        reasons.append(f"Top 10 holders too concentrated ({top10_pct:.1f}%)")
    if top_holder_pct >= 35:
        reasons.append(f"Single top holder too large ({top_holder_pct:.1f}%)")
    if fake_volume_ratio >= 0.65:
        reasons.append("High fake volume probability")
    if wash_risk >= 0.65:
        reasons.append("High wash trading probability")
    if bundle_risk >= 0.75:
        reasons.append("High bundled wallet concentration")
    if sniper_risk >= 0.80:
        reasons.append("High sniper wallet probability")

    # Only reject low holders if token is already older
    if holders is not None and holders < 25 and age_hours > 6:
        reasons.append("Too few holders for an older token")

    if reasons:
        return {
            "approved": False,
            "rug_probability": 90.0,
            "risk_score": 90.0,
            "reasons": reasons,
            "warnings": warnings,
            "metrics": {
                "fake_volume_ratio": round(fake_volume_ratio * 100, 1),
                "wash_trading_risk": round(wash_risk * 100, 1),
                "sniper_risk": round(sniper_risk * 100, 1),
                "bundle_risk": round(bundle_risk * 100, 1),
                "dev_pct": round(dev_pct, 2),
                "top10_pct": round(top10_pct, 2),
                "top_holder_pct": round(top_holder_pct, 2),
                "liquidity_lock_score": round(liquidity_lock_score * 100, 1),
            }
        }

    # ---------------- WARNINGS / PENALTIES ----------------
    penalty = 0.0

    # early low-holder tokens get warning, not rejection
    if holders is not None:
        if holders < 20 and age_hours <= 1:
            penalty += 6
            warnings.append(f"Very early low holders ({holders})")
        elif holders < 40 and age_hours <= 2:
            penalty += 4
            warnings.append(f"Low holder base ({holders})")
        elif holders < 60 and age_hours <= 4:
            penalty += 2

    if dev_pct >= 12:
        penalty += 10
        warnings.append(f"Elevated dev allocation ({dev_pct:.1f}%)")
    elif dev_pct >= 8:
        penalty += 5

    if top10_pct >= 55:
        penalty += 10
        warnings.append(f"Top 10 concentration high ({top10_pct:.1f}%)")
    elif top10_pct >= 40:
        penalty += 5

    if top_holder_pct >= 18:
        penalty += 8
        warnings.append(f"Top holder concentration ({top_holder_pct:.1f}%)")
    elif top_holder_pct >= 12:
        penalty += 4

    if fake_volume_ratio >= 0.35:
        penalty += 10
        warnings.append("Possible fake volume")
    elif fake_volume_ratio >= 0.20:
        penalty += 5

    if wash_risk >= 0.35:
        penalty += 10
        warnings.append("Possible wash trading")
    elif wash_risk >= 0.20:
        penalty += 5

    if sniper_risk >= 0.45:
        penalty += 8
        warnings.append("Potential sniper wallet activity")
    elif sniper_risk >= 0.25:
        penalty += 4

    if bundle_risk >= 0.45:
        penalty += 8
        warnings.append("Potential bundled wallets")
    elif bundle_risk >= 0.25:
        penalty += 4

    if liquidity_lock_score <= 0.25:
        penalty += 10
        warnings.append("Weak liquidity lock confidence")
    elif liquidity_lock_score <= 0.45:
        penalty += 5

    if mintable:
        penalty += 8
        warnings.append("Mint authority active")
    if freezable:
        penalty += 6
        warnings.append("Freeze authority active")

    risk_score = min(12.0 + penalty, 100.0)

    return {
        "approved": risk_score < 60.0,
        "rug_probability": round(risk_score, 1),
        "risk_score": round(risk_score, 1),
        "reasons": reasons,
        "warnings": warnings,
        "metrics": {
            "fake_volume_ratio": round(fake_volume_ratio * 100, 1),
            "wash_trading_risk": round(wash_risk * 100, 1),
            "sniper_risk": round(sniper_risk * 100, 1),
            "bundle_risk": round(bundle_risk * 100, 1),
            "dev_pct": round(dev_pct, 2),
            "top10_pct": round(top10_pct, 2),
            "top_holder_pct": round(top_holder_pct, 2),
            "liquidity_lock_score": round(liquidity_lock_score * 100, 1),
        }
    }
