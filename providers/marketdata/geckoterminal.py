from config.settings import GECKOTERMINAL_API
from providers.marketdata._resilience import get_json

EXCLUDED_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT", "USDC.E", "USDT.E"}


def _to_float_or_none(value) -> float | None:
    """Same explicit-rejection convention used by the discovery adapter
    and providers.marketdata.solanatracker: None/unparseable returns
    None, never 0.0, so an unknown liquidity value can never accidentally
    satisfy a min<=x<=max check."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def get_pool_liquidity_usd(mint: str, *, market: str | None = None) -> float | None:
    """Fetch a token's current pool liquidity in USD from GeckoTerminal's
    token-pools endpoint (GET /networks/solana/tokens/{mint}/pools).

    Third-tier liquidity fallback used exclusively by the discovery layer
    (domain/signals/_radar_discovery_adapter.py) for Pump.fun candidates,
    tried only when both DexScreener and Solana Tracker have already
    returned None for the same mint (e.g. Solana Tracker credit/quota
    exhaustion). `market` is accepted for call-signature symmetry with
    providers.marketdata.solanatracker.get_pool_liquidity_usd() but is
    not sent to GeckoTerminal -- its pools-by-token endpoint has no
    dex/market filter; every returned pool already belongs to this exact
    mint via the endpoint path itself, so no cross-pool/cross-token mixup
    is possible.

    Returns None -- never 0.0 -- when the request fails, the response has
    no pools, or no pool reports a usable reserve_in_usd figure. This
    preserves the discovery layer's existing "unknown liquidity must
    never pass a min<=x<=max check" guarantee. When multiple pools exist
    for the mint, the highest reserve_in_usd is used (same "pick highest
    liquidity" convention as the DexScreener and Solana Tracker liquidity
    lookups).
    """
    url = f"{GECKOTERMINAL_API}/networks/solana/tokens/{mint}/pools"
    data = await get_json(
        url,
        headers={"Accept": "application/json"},
        cache_ttl_seconds=15,
        timeout_seconds=10.0,
    )
    if not isinstance(data, dict):
        return None

    pools = data.get("data")
    if not isinstance(pools, list):
        return None

    best: float | None = None
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        attrs = pool.get("attributes")
        if not isinstance(attrs, dict):
            continue
        usd = _to_float_or_none(attrs.get("reserve_in_usd"))
        if usd is None:
            continue
        if best is None or usd > best:
            best = usd

    return best


def _to_float_or_none(value) -> float | None:
    """Same explicit-rejection convention used by the discovery adapter
    and providers.marketdata.solanatracker: None/unparseable returns
    None, never 0.0, so an unknown liquidity value can never accidentally
    satisfy a min<=x<=max check."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def get_pool_liquidity_usd(mint: str, *, market: str | None = None) -> float | None:
    """Fetch a token's current pool liquidity in USD from GeckoTerminal's
    token-pools endpoint (GET /networks/solana/tokens/{mint}/pools).

    Third-tier liquidity fallback used exclusively by the discovery layer
    (domain/signals/_radar_discovery_adapter.py) for Pump.fun candidates,
    tried only when both DexScreener and Solana Tracker have already
    returned None for the same mint (e.g. Solana Tracker credit/quota
    exhaustion). `market` is accepted for call-signature symmetry with
    providers.marketdata.solanatracker.get_pool_liquidity_usd() but is
    not sent to GeckoTerminal -- its pools-by-token endpoint has no
    dex/market filter; every returned pool already belongs to this exact
    mint via the endpoint path itself, so no cross-pool/cross-token mixup
    is possible.

    Returns None -- never 0.0 -- when the request fails, the response has
    no pools, or no pool reports a usable reserve_in_usd figure. This
    preserves the discovery layer's existing "unknown liquidity must
    never pass a min<=x<=max check" guarantee. When multiple pools exist
    for the mint, the highest reserve_in_usd is used (same "pick highest
    liquidity" convention as the DexScreener and Solana Tracker liquidity
    lookups).
    """
    url = f"{GECKOTERMINAL_API}/networks/solana/tokens/{mint}/pools"
    data = await get_json(
        url,
        headers={"Accept": "application/json"},
        cache_ttl_seconds=15,
        timeout_seconds=10.0,
    )
    if not isinstance(data, dict):
        return None

    pools = data.get("data")
    if not isinstance(pools, list):
        return None

    best: float | None = None
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        attrs = pool.get("attributes")
        if not isinstance(attrs, dict):
            continue
        usd = _to_float_or_none(attrs.get("reserve_in_usd"))
        if usd is None:
            continue
        if best is None or usd > best:
            best = usd

    return best


async def get_trending_tokens() -> list[dict]:
    """
    Fetch trending Solana tokens from GeckoTerminal.

    We use trending pools and resolve the base token from the `included` section.
    This avoids returning repeated SOL pools.
    """
    url = f"{GECKOTERMINAL_API}/networks/solana/trending_pools"
    params = {
        "page": 1,
        "include": "base_token"
    }
    headers = {"Accept": "application/json"}

    payload = await get_json(url, params=params, headers=headers, cache_ttl_seconds=30)
    if payload is None:
        return []

    included = payload.get("included", [])
    token_map = {}

    for item in included:
        if item.get("type") != "token":
            continue

        attrs = item.get("attributes", {})
        token_id = item.get("id", "")
        address = attrs.get("address", "")

        if not address and "_" in token_id:
            address = token_id.split("_")[-1]

        token_map[token_id] = {
            "name": attrs.get("name", "Unknown"),
            "symbol": attrs.get("symbol", "???"),
            "address": address,
        }

    results = []
    seen_addresses = set()

    for pool in payload.get("data", []):
        attrs = pool.get("attributes", {})
        relationships = pool.get("relationships", {})

        base_token_rel = relationships.get("base_token", {}).get("data", {})
        base_token_id = base_token_rel.get("id", "")

        base_token = token_map.get(base_token_id, {})
        symbol = (base_token.get("symbol") or "").upper()
        address = base_token.get("address", "")

        if not address:
            continue

        if symbol in EXCLUDED_SYMBOLS:
            continue

        if address in seen_addresses:
            continue

        seen_addresses.add(address)

        price_change = attrs.get("price_change_percentage", {})
        volume_data = attrs.get("volume_usd", {})

        results.append({
            "name": base_token.get("name", "Unknown"),
            "symbol": base_token.get("symbol", "???"),
            "price": attrs.get("base_token_price_usd", "N/A"),
            "price_change_24h": price_change.get("h24", "N/A") if isinstance(price_change, dict) else "N/A",
            "volume_24h": volume_data.get("h24", "N/A") if isinstance(volume_data, dict) else volume_data,
            "liquidity": attrs.get("reserve_in_usd", "N/A"),
            "contract": address,
        })

        if len(results) >= 10:
            break

    return results
