import logging

import aiohttp

logger = logging.getLogger("AlphaPulse.LpLockChecker")

RAYDIUM_POOL_INFO_URL = "https://api-v3.raydium.io/pools/info/ids"

# Field names Raydium's v3 pool-info API has exposed for "% of LP supply
# burned/locked" across recent versions. Checked in order; first present
# field wins. If none are present, the lookup is treated as inconclusive
# (returns None) rather than guessing a percentage.
_BURN_FIELD_CANDIDATES = ("burnPercent", "burnPct", "lpBurnPercent", "lockedPercent")


# --------------------------------------------------------------------
# Unverified-lock corroboration (Locked Liquidity Policy, production
# hardening pass).
#
# get_real_lp_lock_pct() above returns None very often for entirely
# legitimate reasons (still on Pump.fun's bonding curve, migrated to a
# DEX Raydium's own API doesn't cover, transient lookup failure). A
# previous version of this codebase treated that None as an automatic
# hard reject, which silently rejected almost every real candidate --
# that regression has already been fixed: risk_engine's
# evaluate_verified_red_flags() correctly treats None as "unknown", not
# "unsafe", and never rejects on that basis alone.
#
# But "never reject on unverifiable lock alone" and "give an
# unverifiable pool a completely free pass" are not the same policy,
# and only the first one is actually required for signal quality. This
# function is the safest middle ground for production: when the lock
# status genuinely can't be confirmed, it requires the token to already
# show comfortable, corroborating evidence of safety from data the
# caller has ALREADY fetched (no new external calls, no fabricated
# numbers) — real liquidity depth well above the Signal Engine's own
# minimum floor, and holder/bundle concentration comfortably under the
# existing hard-reject thresholds (services/conviction_scorer.py,
# untouched by this function). A token that clears those on its own
# doesn't need a lock confirmation to be reasonably safe; a token that
# is ALSO thin on liquidity or already borderline on concentration, and
# on top of that has no way to confirm its liquidity isn't about to be
# pulled, is exactly the "confidently unsafe" case worth catching.
# ---------------------------------------------------------------------

# Comfortably above pump_radar.MIN_LIQUIDITY_USD (5000) -- real depth,
# not just enough to clear the Signal Engine's own floor.
UNVERIFIED_LOCK_MIN_LIQUIDITY_USD = 15000.0
# Comfortably under conviction_scorer.hard_reject_reasons' 30%/40% hard
# ceilings -- corroborating margin, not a new ceiling of its own.
UNVERIFIED_LOCK_MAX_TOP_HOLDER_PCT = 15.0
UNVERIFIED_LOCK_MAX_BUNDLE_PCT = 15.0


def assess_unverified_lock_risk(data: dict, holder_analysis: dict | None) -> tuple[bool, str | None]:
    """
    Only called when get_real_lp_lock_pct() returned None. Returns
    (safe_enough, reason) -- reason is None when safe_enough is True,
    otherwise a short human-readable string for the reject log line.

    safe_enough=True means: proceed exactly as before (real_lp_lock_pct
    stays None and is passed into evaluate_verified_red_flags()
    unchanged -- this function never overrides or fabricates a lock
    percentage). safe_enough=False means: the unverifiable pool also
    lacks any corroborating safety margin, so it's rejected here
    specifically, before the more expensive checks further down the
    pipeline run on a candidate that's confidently too risky to send.
    """
    holder_analysis = holder_analysis or {}

    def _num(value, default=0.0):
        try:
            if value is None:
                return default
            return float(str(value).replace(",", "").replace("$", ""))
        except (ValueError, TypeError):
            return default

    liq = _num(data.get("liquidity"))
    if liq < UNVERIFIED_LOCK_MIN_LIQUIDITY_USD:
        return False, (
            f"unverifiable LP lock + liquidity only ${liq:,.0f} "
            f"(need >= ${UNVERIFIED_LOCK_MIN_LIQUIDITY_USD:,.0f} to corroborate)"
        )

    top_holder_pct = holder_analysis.get("top_holder_pct")
    if top_holder_pct is not None and _num(top_holder_pct) > UNVERIFIED_LOCK_MAX_TOP_HOLDER_PCT:
        return False, (
            f"unverifiable LP lock + top holder {_num(top_holder_pct):.1f}% "
            f"(need <= {UNVERIFIED_LOCK_MAX_TOP_HOLDER_PCT:.0f}% to corroborate)"
        )

    bundle_pct = holder_analysis.get("bundle_pct")
    if bundle_pct is not None and _num(bundle_pct) > UNVERIFIED_LOCK_MAX_BUNDLE_PCT:
        return False, (
            f"unverifiable LP lock + bundle cluster {_num(bundle_pct):.1f}% "
            f"(need <= {UNVERIFIED_LOCK_MAX_BUNDLE_PCT:.0f}% to corroborate)"
        )

    return True, None


async def get_real_lp_lock_pct(pool_address: str) -> float | None:
    """
    Real, verifiable LP burn/lock percentage for a Raydium pool, fetched
    directly from Raydium's own public pool-info API.

    This is NOT the same thing as risk_engine.estimate_liquidity_lock_score(),
    which only *infers* lock confidence from liquidity size and contract
    naming pattern. This function checks Raydium's own records for what
    was actually done with the LP tokens.

    Returns a 0-100 float when Raydium's API returns a recognizable
    burn/lock field for this pool, or None when:
      - the pool isn't on Raydium (e.g. still on Pump.fun's bonding curve,
        or migrated to a different DEX),
      - the API request fails or times out,
      - the response doesn't contain a field this code recognizes
        (Raydium has changed their schema before).

    Callers MUST treat None as "unknown / couldn't verify" and never
    display it as "0% locked" — those are very different facts, and
    showing 0% for an unverifiable pool would be exactly the kind of
    fabricated-looking value this project's card is built to avoid.

    IMPORTANT — NOT YET LIVE-VALIDATED: this was written against Raydium's
    publicly documented v3 pool-info endpoint, but this build environment
    has no network access, so the exact response shape/field name could
    not be confirmed against a live call. Before trusting this in
    production: log one raw response for a token you know is LP-burned
    and a token you know is NOT, and confirm the field name and 0-100 vs
    0-1 scale actually match what's parsed below.
    """
    if not pool_address:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            params = {"ids": pool_address}
            async with session.get(
                RAYDIUM_POOL_INFO_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
    except Exception as e:
        logger.warning(f"LP lock lookup failed for pool {pool_address[:8]}...: {e}")
        return None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not data:
        return None

    if isinstance(data, list):
        pool = data[0] if data else None
    elif isinstance(data, dict):
        pool = data
    else:
        pool = None

    if not pool or not isinstance(pool, dict):
        return None

    for field in _BURN_FIELD_CANDIDATES:
        raw = pool.get(field)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue

    return None
