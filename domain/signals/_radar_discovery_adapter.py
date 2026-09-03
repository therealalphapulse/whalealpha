"""DexScreener-first discovery adapter for PumpRadar.

Discovery-only responsibility: seed candidate mints from DexScreener,
validate the exact discovery filters, and return mint addresses to the
existing PumpRadar verification/scoring/signal pipeline. This module does
not score, qualify, or alter downstream signal/trading behavior.

Exact discovery filters:
- chain: Solana
- DEX: Pump.fun or PumpSwap (dexId == "pumpfun" or "pumpswap")
- pair age: < 6 hours
- market cap: $50K-$1M
- liquidity: > $15K

DexScreener's public multi-token feeds are used only as the candidate seed;
each candidate is independently re-fetched with get_token_card_info() before
it is returned. Missing/unparseable discovery data is an explicit rejection.
"""

from __future__ import annotations

import logging
import time

from providers.marketdata.dexscreener import (
    get_latest_boosted_tokens,
    get_latest_token_profiles,
    get_token_card_info,
)

logger = logging.getLogger("AlphaPulse.PumpRadar")

_DISCOVERY_CHAIN = "solana"
_ALLOWED_DEX_IDS = {"pumpfun", "pumpswap"}
_MIN_LIQUIDITY_USD = 15_000.0
_MIN_MARKET_CAP_USD = 50_000.0
_MAX_MARKET_CAP_USD = 1_000_000.0
_MAX_PAIR_AGE_HOURS = 6.0
_MAX_VALIDATIONS_PER_CYCLE = 120


def _to_float_or_none(value) -> float | None:
    if value is None or value == "N/A":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _seed_candidates(feeds: list[list[dict]]) -> list[str]:
    """Return unique Solana token mints from DexScreener discovery feeds."""
    seen: set[str] = set()
    result: list[str] = []
    for feed in feeds:
        for entry in feed:
            if not isinstance(entry, dict):
                continue
            if (entry.get("chainId") or "").strip().lower() != _DISCOVERY_CHAIN:
                continue
            mint = (entry.get("tokenAddress") or "").strip()
            if mint and mint not in seen:
                seen.add(mint)
                result.append(mint)
    return result


async def _validate_candidate(mint: str) -> tuple[str, float] | None:
    """Validate one candidate against the exact DexScreener filters."""
    try:
        info = await get_token_card_info(mint)
    except Exception as exc:
        logger.debug("[FILTER] %s rejected: DexScreener verification error=%s", mint, type(exc).__name__)
        return None

    if not info:
        logger.debug("[FILTER] %s rejected: no DexScreener market data", mint)
        return None

    dex = (info.get("dex") or "").strip().lower()
    if dex not in _ALLOWED_DEX_IDS:
        logger.debug("[FILTER] %s rejected: DEX=%s (required=pumpfun/pumpswap)", mint, dex or "unknown")
        return None

    liquidity = _to_float_or_none(info.get("liquidity"))
    if liquidity is None or liquidity <= _MIN_LIQUIDITY_USD:
        logger.debug("[FILTER] %s rejected: liquidity=%s (required>$15K)", mint, liquidity)
        return None

    market_cap = _to_float_or_none(info.get("market_cap"))
    if market_cap is None or not (_MIN_MARKET_CAP_USD <= market_cap <= _MAX_MARKET_CAP_USD):
        logger.debug("[FILTER] %s rejected: MC=%s (required=$50K-$1M)", mint, market_cap)
        return None

    pair_created = info.get("pair_created")
    try:
        created_seconds = float(pair_created) / 1000.0
    except (TypeError, ValueError):
        logger.debug("[FILTER] %s rejected: pair age unavailable", mint)
        return None

    age_hours = (time.time() - created_seconds) / 3600.0
    if not (0.0 <= age_hours < _MAX_PAIR_AGE_HOURS):
        logger.debug("[FILTER] %s rejected: age=%.2fh (required<6h)", mint, age_hours)
        return None

    logger.info(
        "[DISCOVERY] %s passed filter: MC=$%s LIQ=$%s AGE=%.2fh",
        mint,
        f"{market_cap:,.0f}",
        f"{liquidity:,.0f}",
        age_hours,
    )
    return mint, created_seconds


def install() -> None:
    """Install the DexScreener discovery implementation into PumpRadar."""
    from domain.signals import pump_radar

    original = pump_radar.fetch_pump_fun_launches
    if getattr(original, "_alphapulse_dexscreener_discovery", False):
        return

    async def fetch_pump_fun_launches(limit: int = 30) -> list[str]:
        requested = max(int(limit), 1)

        profiles: list[dict] = []
        boosts: list[dict] = []
        try:
            profiles = await get_latest_token_profiles()
        except Exception as exc:
            logger.warning("[DISCOVERY] DexScreener profiles fetch failed: %s", type(exc).__name__)

        try:
            boosts = await get_latest_boosted_tokens()
        except Exception as exc:
            logger.warning("[DISCOVERY] DexScreener boosts fetch failed: %s", type(exc).__name__)

        seed = _seed_candidates([profiles, boosts])
        to_check = seed[:_MAX_VALIDATIONS_PER_CYCLE]
        validated: list[tuple[str, float]] = []

        for mint in to_check:
            result = await _validate_candidate(mint)
            if result is not None:
                validated.append(result)

        validated.sort(key=lambda item: item[1], reverse=True)
        selected = [mint for mint, _ in validated[:requested]]

        logger.info(
            "[DISCOVERY] DexScreener cycle: fetched=%d solana_candidates=%d checked=%d passed=%d selected=%d "
            "[pumpfun/pumpswap age<6h MC=$50K-$1M LIQ>$15K]",
            len(profiles) + len(boosts),
            len(seed),
            len(to_check),
            len(validated),
            len(selected),
        )
        return selected

    fetch_pump_fun_launches._alphapulse_dexscreener_discovery = True
    pump_radar.fetch_pump_fun_launches = fetch_pump_fun_launches
    logger.info(
        "[PumpRadar] DexScreener discovery installed: Solana, pumpfun/pumpswap, age<6h, MC=$50K-$1M, LIQ>$15K"
    )
