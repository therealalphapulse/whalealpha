import logging

import aiohttp

logger = logging.getLogger("AlphaPulse.JupiterPrice")

JUPITER_PRICE_API = "https://api.jup.ag/price/v2"

# How far apart the two sources can be before it's flagged as a genuine
# mismatch worth warning about, rather than ordinary cross-source noise
# (different pools/venues can legitimately differ by a couple percent).
MISMATCH_THRESHOLD_PCT = 15.0


async def get_jupiter_price(mint: str) -> float | None:
    """
    Fetches a token's current USD price from Jupiter's public Price API —
    an entirely independent data source from DexScreener (different
    aggregation, different pools weighted). Returns None on any failure;
    never a guessed price.

    NOT YET LIVE-VALIDATED: written against Jupiter's documented Price
    API v2 response shape, but this build environment has no network
    access to confirm against a live call. Verify the response shape for
    one real mint before relying on this.
    """
    if not mint:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            params = {"ids": mint}
            async with session.get(
                JUPITER_PRICE_API,
                params=params,
                timeout=aiohttp.ClientTimeout(total=6),
            ) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
    except Exception as e:
        logger.warning(f"Jupiter price lookup failed for {mint[:8]}...: {e}")
        return None

    entry = (payload.get("data") or {}).get(mint) if isinstance(payload, dict) else None
    if not entry:
        return None

    try:
        return float(entry.get("price"))
    except (TypeError, ValueError):
        return None


async def check_price_agreement(mint: str, dexscreener_price) -> dict:
    """
    Cross-checks DexScreener's price for this mint against Jupiter's.

    Returns:
        {"jupiter_price": float | None, "mismatch_pct": float | None,
         "agrees": bool | None}
    `agrees` is None (not True/False) whenever either source is
    unavailable — an unconfirmed price is not the same as a confirmed
    agreement, so callers must not treat a missing Jupiter price as
    "no mismatch found."
    """
    try:
        dex_price = float(dexscreener_price)
    except (TypeError, ValueError):
        return {"jupiter_price": None, "mismatch_pct": None, "agrees": None}

    jup_price = await get_jupiter_price(mint)
    if jup_price is None or dex_price <= 0:
        return {"jupiter_price": jup_price, "mismatch_pct": None, "agrees": None}

    mismatch_pct = abs(dex_price - jup_price) / dex_price * 100
    return {
        "jupiter_price": jup_price,
        "mismatch_pct": round(mismatch_pct, 1),
        "agrees": mismatch_pct <= MISMATCH_THRESHOLD_PCT,
    }
