"""Expanded Reactivation Radar discovery source.

This module only broadens the DISCOVERY pool. It deliberately reuses the
existing AlphaPulse reactivation activity thresholds and sends every returned
candidate through the unchanged authoritative analyze_candidate() pipeline.
It does not alter scoring, hard rejects, holder requirements, security checks,
confidence, quota, or alert delivery.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from providers.marketdata._resilience import get_json
from domain.signals.enhanced_alert_runtime import (
    REACTIVATION_MIN_AGE_HOURS,
    REACTIVATION_MIN_VOLUME_1H,
    REACTIVATION_MIN_ACCELERATION,
    REACTIVATION_MIN_BUY_RATIO,
    _is_pump_fun,
    _to_float,
    _parse_pool_created_at,
)

logger = logging.getLogger("AlphaPulse.ReactivationExpanded")

# Discovery-only expansion. These do NOT change AlphaPulse qualification.
# More pages increase the chance of finding older tokens that have just
# re-entered the activity distribution; the existing activity thresholds
# remain unchanged.
EXPANDED_PAGES = (1, 2, 3, 4, 5)
EXPANDED_SOURCE_LIMIT = 160
EXPANDED_QUALIFIED_LIMIT = 30


async def fetch_expanded_reactivation_candidates(
    limit: int = EXPANDED_QUALIFIED_LIMIT,
) -> list[str]:
    """Discover a wider set of older Pump.fun candidates.

    We read five pages from each of the existing GeckoTerminal discovery
    views instead of only the first few pages. The exact existing reactivation
    activity thresholds are retained. Crucially, this discovery layer does
    NOT apply the MC/liquidity gate itself: doing so here can discard a token
    because discovery enrichment is stale/missing before the authoritative
    analyze_candidate() call gets a chance to evaluate it with fresh data.

    analyze_candidate() remains the single authoritative qualification path
    and therefore continues to apply the existing MC/liquidity, volume,
    security, holder, hard-gate, score, confidence and quota logic unchanged.
    """
    base_urls = (
        "https://api.geckoterminal.com/api/v2/networks/solana/pools"
        "?include=base_token&sort=h24_volume_usd_desc&page={page}",
        "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
        "?include=base_token&duration=1h&page={page}",
    )

    ranked: dict[str, float] = {}

    for template in base_urls:
        for page in EXPANDED_PAGES:
            url = template.format(page=page)
            try:
                payload = await get_json(url, cache_ttl_seconds=30, timeout_seconds=10)
                if not payload:
                    continue

                token_map: dict[str, str] = {}
                for item in payload.get("included", []) or []:
                    if item.get("type") != "token":
                        continue
                    attrs = item.get("attributes") or {}
                    address = attrs.get("address") or ""
                    item_id = item.get("id") or ""
                    if address:
                        token_map[item_id] = address

                for pool in payload.get("data", []) or []:
                    attrs = pool.get("attributes") or {}
                    base_rel = (
                        pool.get("relationships", {})
                        .get("base_token", {})
                        .get("data", {})
                        .get("id", "")
                    )
                    mint = token_map.get(base_rel, "")
                    if not mint or not _is_pump_fun(mint):
                        continue

                    created_ts = _parse_pool_created_at(attrs.get("pool_created_at"))
                    if created_ts is None:
                        continue
                    age_hours = (
                        datetime.now(timezone.utc).timestamp() - created_ts
                    ) / 3600.0
                    if age_hours < REACTIVATION_MIN_AGE_HOURS:
                        continue

                    volume = attrs.get("volume_usd") or {}
                    vol_1h = _to_float(volume.get("h1"))
                    vol_24h = _to_float(volume.get("h24"))
                    if vol_1h < REACTIVATION_MIN_VOLUME_1H or vol_24h <= 0:
                        continue

                    h1 = (attrs.get("transactions") or {}).get("h1") or {}
                    buys = _to_float(h1.get("buys"))
                    sells = _to_float(h1.get("sells"))
                    total = buys + sells
                    buy_ratio = buys / total if total > 0 else 0.0
                    acceleration = vol_1h / max(vol_24h / 24.0, 1.0)
                    price_change = _to_float(
                        (attrs.get("price_change_percentage") or {}).get("h1")
                    )

                    qualifies = (
                        acceleration >= REACTIVATION_MIN_ACCELERATION
                        or buy_ratio >= REACTIVATION_MIN_BUY_RATIO
                        or price_change >= 3.0
                    )
                    if not qualifies:
                        continue

                    activity_score = min(acceleration, 4.0) * 20.0
                    activity_score += max(
                        0.0, min((buy_ratio - 0.50) * 100.0, 25.0)
                    )
                    activity_score += max(0.0, min(price_change, 25.0))
                    ranked[mint] = max(ranked.get(mint, 0.0), activity_score)

            except Exception as exc:
                # One discovery page failing must not stop the other pages or
                # the existing fresh-token scanner.
                logger.warning(
                    "Expanded reactivation source page failed (non-fatal): page=%s error=%s",
                    page,
                    exc,
                )

    ordered = sorted(ranked, key=ranked.get, reverse=True)[:EXPANDED_SOURCE_LIMIT]
    eligible = ordered[: min(limit, EXPANDED_QUALIFIED_LIMIT)]

    logger.info(
        "🔎 Reactivation Radar EXPANDED: %d older Pump.fun candidates shortlisted "
        "(%d discovery candidates ranked across %d pages/source; authoritative "
        "qualification remains unchanged)",
        len(eligible),
        len(ordered),
        len(EXPANDED_PAGES),
    )
    return eligible
