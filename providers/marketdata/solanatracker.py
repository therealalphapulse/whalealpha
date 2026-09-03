"""providers/marketdata/solanatracker.py

Solana Tracker liquidity lookup -- used exclusively by the discovery
layer (domain/signals/_radar_discovery_adapter.py) as a fallback source
for liquidity when DexScreener reports it as unavailable. That is the
normal, expected case for pre-migration Pump.fun bonding-curve pairs:
DexScreener has no conventional AMM pool to report a liquidity.usd
figure for (confirmed empirically -- every dexId="pumpfun" pair returns
liquidity=None from DexScreener own API, while dexId="pumpswap" pairs,
i.e. post-migration, always have it). Solana Tracker natively indexes
Pump.fun bonding-curve reserves and reports a real, provider-observed
liquidityUsd figure for the same pools.

This module does NOT become a second discovery source and does NOT
replace DexScreener for anything else: dex id, market cap, and pair age
still come exclusively from providers.marketdata.dexscreener
get_token_card_info(). This is a narrow, single-field lookup, gated
behind SOLANA_TRACKER_API_KEY being configured.

Uses Solana Tracker public Search API (GET /search), not the
single-token /tokens/{mint} endpoint: /tokens/{mint} does not reliably
carry a top-level liquidity figure for bonding-curve pairs across all
response shapes, whereas /search SearchToken result exposes
liquidityUsd directly per token (confirmed against the documented
OpenAPI schema at docs.solanatracker.io/data-api/search/token-search).
The mint is passed as query (Search supports exact address lookups),
market narrows the search server-side when given, and every returned
row own mint (and market, when scoping is requested) is checked
again client-side before its liquidityUsd is trusted -- so a fuzzy or
promoted match for a different token can never be silently accepted as
this mint liquidity.

Config: config/settings.py DISCOVERY_LIQUIDITY_FALLBACK_ENABLED (default
True) lets this fallback be turned off without a code change.

Provider resilience (2026-08-28): both functions below route through
providers.marketdata._resilience.get_json(provider_name="solana_tracker"),
which opts them into the shared per-provider circuit breaker in
providers.marketdata._provider_circuit_breaker. A persistently unhealthy
Solana Tracker (403 / out of credits / auth failure / repeated connection
failures) stops being called for a cooldown period instead of being hit on
every single candidate, and resumes automatically once a probe request
succeeds. This does not change either function's return contract: a
provider that is skipped by the breaker still returns None, exactly like
any other "provider unavailable" outcome these functions already return —
callers do not need to know the breaker exists.
"""

from __future__ import annotations

import os

from providers.marketdata._resilience import get_json

_SEARCH_ENDPOINT = "https://data.solanatracker.io/search"


def _to_float_or_none(value) -> float | None:
    """Same explicit-rejection convention as the discovery adapter:
    None/unparseable returns None, never 0.0."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def get_pool_liquidity_usd(mint: str, *, market: str | None = None) -> float | None:
    """Fetch a token current pool liquidity in USD from Solana Tracker
    Search API.

    When market is given (e.g. "pumpfun"), it is sent as a server-side
    search filter AND re-checked against each returned row own market
    field -- this is what keeps the fallback scoped to the exact same dex
    the discovery filter is targeting. Every candidate row is also
    re-checked against the exact requested mint (Search can return
    fuzzy/promoted rows alongside the exact match). If more than one
    matching row remains, the highest-liquidity one is used (same "pick
    highest liquidity" convention as
    providers.marketdata.dexscreener.get_token_card_info()).

    Returns None -- never 0.0 -- when the provider is not configured, the
    request fails, or no matching row reports a usable liquidityUsd
    figure. This preserves the discovery layer existing "unknown
    liquidity must never pass a min<=x<=max check" guarantee.
    """
    api_key = os.getenv("SOLANA_TRACKER_API_KEY")
    if not api_key:
        return None

    params: dict = {"query": mint, "limit": 20}
    if market is not None:
        params["market"] = market

    data = await get_json(
        _SEARCH_ENDPOINT,
        params=params,
        headers={"x-api-key": api_key},
        cache_ttl_seconds=15,
        timeout_seconds=10.0,
        provider_name="solana_tracker",
    )
    if not isinstance(data, dict):
        return None

    results = data.get("data")
    if not isinstance(results, list):
        return None

    best: float | None = None
    for entry in results:
        if not isinstance(entry, dict):
            continue
        if (entry.get("mint") or "").strip() != mint:
            continue
        if market is not None and (entry.get("market") or "").strip().lower() != market:
            continue
        usd = _to_float_or_none(entry.get("liquidityUsd"))
        if usd is None:
            continue
        if best is None or usd > best:
            best = usd

    return best


# ---------------------------------------------------------------------
# Bundle-risk lookup -- authoritative replacement for this codebase's own
# +/-15% balance-similarity wallet clustering (see
# domain/intelligence/holders.py BUNDLE_BALANCE_TOLERANCE /
# _cluster_bundles(), and domain.intelligence.holders._resolve_bundle_risk()
# for how the two are combined: this is tried first, the local clustering
# heuristic is the fallback ONLY when this returns None). Uses Solana
# Tracker single-token endpoint (GET /tokens/{mint}), which is the
# provider-documented location for the risk object (risk.bundlers.*),
# unlike get_pool_liquidity_usd() above which deliberately avoids that
# endpoint for the unrelated liquidity figure -- these are two different,
# independently-verified fields on two different Solana Tracker endpoints.
#
# Config: config/settings.py BUNDLE_RISK_SOLANA_TRACKER_ENABLED (default
# True) lets this be turned off without a code change; also silently inert
# (falls straight through to the local fallback) without
# SOLANA_TRACKER_API_KEY configured, same as get_pool_liquidity_usd().
# ---------------------------------------------------------------------
_TOKEN_ENDPOINT = "https://data.solanatracker.io/tokens/{mint}"


async def get_bundle_risk_pct(mint: str) -> tuple[float | None, int | None]:
    """Fetch Solana Tracker's authoritative bundler-risk figures for a
    token: risk.bundlers.totalPercentage and risk.bundlers.count from
    GET /tokens/{mint}.

    Returns (total_percentage, wallet_count). Both are None -- never
    0.0 / 0 -- when the provider is not configured, the request fails,
    the response has no risk/bundlers object, or totalPercentage isn't a
    parseable number. This is the same explicit-rejection convention as
    get_pool_liquidity_usd() above: a genuine "0% bundled" reading from
    Solana Tracker is a real result and is returned as 0.0, never
    confused with "unavailable".
    """
    api_key = os.getenv("SOLANA_TRACKER_API_KEY")
    if not api_key:
        return None, None

    data = await get_json(
        _TOKEN_ENDPOINT.format(mint=mint),
        headers={"x-api-key": api_key},
        cache_ttl_seconds=30,
        timeout_seconds=10.0,
        provider_name="solana_tracker",
    )
    if not isinstance(data, dict):
        return None, None

    risk = data.get("risk")
    if not isinstance(risk, dict):
        return None, None

    bundlers = risk.get("bundlers")
    if not isinstance(bundlers, dict):
        return None, None

    pct = _to_float_or_none(bundlers.get("totalPercentage"))

    count = bundlers.get("count")
    try:
        count = int(count) if count is not None else None
    except (TypeError, ValueError):
        count = None

    return pct, count
