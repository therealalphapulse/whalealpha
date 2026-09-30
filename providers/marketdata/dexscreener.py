import asyncio

from config.settings import DEXSCREENER_API, DEXSCREENER_ROOT_API
from providers.marketdata._resilience import get_json


async def get_token_info(contract_address: str) -> dict | None:
    """Fetch token info from DexScreener by contract address.

    v4: now cached (20s TTL), retried, and timeout-bounded via the shared
    resilience helper — previously this opened a fresh, uncached,
    unretried session per call (audit §4/§7)."""
    url = f"{DEXSCREENER_API}/tokens/{contract_address}"
    data = await get_json(url, cache_ttl_seconds=20)
    if data is None:
        return None

    pairs = data.get("pairs", [])
    if not pairs:
        return None

    # Use the first pair (highest liquidity usually)
    pair = pairs[0]

    # True age: oldest pool's pairCreatedAt across ALL pairs for
    # this contract (see get_token_card_info for the full
    # explanation) — not just whichever pair happens to be first.
    oldest_created_at = None
    for p in pairs:
        created = p.get("pairCreatedAt")
        if not created:
            continue
        try:
            created = int(created)
        except (TypeError, ValueError):
            continue
        if oldest_created_at is None or created < oldest_created_at:
            oldest_created_at = created

    return {
        "name": pair.get("baseToken", {}).get("name", "Unknown"),
        "symbol": pair.get("baseToken", {}).get("symbol", "???"),
        "price": pair.get("priceUsd", "N/A"),
        "price_change_5m": pair.get("priceChange", {}).get("m5", "N/A"),
        "price_change_1h": pair.get("priceChange", {}).get("h1", "N/A"),
        "price_change_24h": pair.get("priceChange", {}).get("h24", "N/A"),
        "volume_24h": pair.get("volume", {}).get("h24", "N/A"),
        "liquidity": pair.get("liquidity", {}).get("usd", "N/A"),
        "market_cap": pair.get("marketCap", "N/A"),
        "fdv": pair.get("fdv", "N/A"),
        "pair_created": oldest_created_at if oldest_created_at is not None else pair.get("pairCreatedAt", "N/A"),
        "dex": pair.get("dexId", "Unknown"),
        "pair_url": pair.get("url", ""),
        "contract": contract_address,
    }


async def get_trending_tokens() -> list[dict]:
    """Fetch trending Solana tokens from DexScreener.

    v4: cached (20s TTL) and retried via the shared resilience helper."""
    url = f"{DEXSCREENER_API}/search?q=solana"
    data = await get_json(url, cache_ttl_seconds=20)
    if data is None:
        return []

    pairs = data.get("pairs", [])[:10]  # Top 10
    results = []
    for pair in pairs:
        results.append({
            "name": pair.get("baseToken", {}).get("name", "Unknown"),
            "symbol": pair.get("baseToken", {}).get("symbol", "???"),
            "price": pair.get("priceUsd", "N/A"),
            "price_change_24h": pair.get("priceChange", {}).get("h24", "N/A"),
            "volume_24h": pair.get("volume", {}).get("h24", "N/A"),
            "liquidity": pair.get("liquidity", {}).get("usd", "N/A"),
            "contract": pair.get("baseToken", {}).get("address", ""),
        })
    return results


async def get_market_overview() -> dict | None:
    """Fetch general Solana DEX activity from DexScreener.

    v4: cached (20s TTL) and retried via the shared resilience helper."""
    url = f"{DEXSCREENER_API}/search?q=SOL"
    data = await get_json(url, cache_ttl_seconds=20)
    if data is None:
        return None

    pairs = data.get("pairs", [])[:20]

    total_volume = 0
    total_liquidity = 0
    gainers = 0
    losers = 0

    for pair in pairs:
        vol = pair.get("volume", {}).get("h24", 0) or 0
        liq = pair.get("liquidity", {}).get("usd", 0) or 0
        change = pair.get("priceChange", {}).get("h24", 0) or 0

        total_volume += vol
        total_liquidity += liq

        if change > 0:
            gainers += 1
        elif change < 0:
            losers += 1

    sentiment = "🟢 Bullish" if gainers > losers else "🔴 Bearish" if losers > gainers else "⚪ Neutral"

    return {
        "total_volume": total_volume,
        "total_liquidity": total_liquidity,
        "gainers": gainers,
        "losers": losers,
        "sentiment": sentiment,
        "pairs_scanned": len(pairs),
    }
def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


async def get_token_card_info(contract_address: str, chain_id: str = "solana", cache_ttl_seconds: int = 15) -> dict | None:
    """
    Fetch richer token info for the automatic contract scanner.

    Uses DexScreener free API.
    Selects the Solana pair with the highest liquidity.

    cache_ttl_seconds defaults to 15 (unchanged) for every existing
    caller (discovery/scoring). Position-monitoring callers
    (position_manager.py, real_exit_engine.py) pass
    config.settings.POSITION_PRICE_CACHE_TTL_SECONDS explicitly so exit
    monitoring can tick faster than 15s without changing the default
    for everyone else.
    """
    url = f"{DEXSCREENER_API}/tokens/{contract_address}"

    try:
        data = await get_json(url, cache_ttl_seconds=cache_ttl_seconds, timeout_seconds=10)
        if data is None:
            return None

        pairs = data.get("pairs") or []
        if not pairs:
            return None

        chain_pairs = [
            pair for pair in pairs
            if pair.get("chainId") == chain_id
        ]

        usable_pairs = chain_pairs or pairs

        pair = max(
            usable_pairs,
            key=lambda p: _to_float((p.get("liquidity") or {}).get("usd", 0))
        )

        # --- True token age fix ---
        # A token can have multiple pools (e.g. its original Pump.fun
        # bonding-curve pair, then a new pair created later on migration
        # to Raydium/another DEX). The pool picked above is whichever has
        # the highest CURRENT liquidity, which after a migration is almost
        # always the newer pair — so using ITS pairCreatedAt reported the
        # token as minutes/hours old right after migration, even though it
        # actually launched days/weeks earlier. That mismatched
        # DexScreener's own token-level age (earliest pool). Fix: age is
        # taken from the OLDEST pairCreatedAt across every pool for this
        # contract, independent of which pool is used for price/liquidity.
        oldest_created_at = None
        for p in usable_pairs:
            created = p.get("pairCreatedAt")
            if not created:
                continue
            try:
                created = int(created)
            except (TypeError, ValueError):
                continue
            if oldest_created_at is None or created < oldest_created_at:
                oldest_created_at = created

        base_token = pair.get("baseToken") or {}
        liquidity = pair.get("liquidity") or {}
        volume = pair.get("volume") or {}
        price_change = pair.get("priceChange") or {}
        txns = pair.get("txns") or {}
        info = pair.get("info") or {}

        websites = info.get("websites") or []
        socials = info.get("socials") or []

        website_url = ""
        twitter_url = ""
        telegram_url = ""

        if websites and isinstance(websites, list):
            first_site = websites[0]
            if isinstance(first_site, dict):
                website_url = first_site.get("url", "")

        if socials and isinstance(socials, list):
            for social in socials:
                if not isinstance(social, dict):
                    continue

                social_type = (social.get("type") or "").lower()
                social_url = social.get("url", "")

                if social_type in ["twitter", "x"]:
                    twitter_url = social_url
                elif social_type == "telegram":
                    telegram_url = social_url

        h1_txns = txns.get("h1") or {}
        h24_txns = txns.get("h24") or {}

        return {
            "name": base_token.get("name", "Unknown"),
            "symbol": base_token.get("symbol", "???"),
            "contract": contract_address,

            "price": pair.get("priceUsd", "N/A"),
            "market_cap": pair.get("marketCap", "N/A"),
            "fdv": pair.get("fdv", "N/A"),
            "liquidity": liquidity.get("usd", "N/A"),

            "volume_1h": volume.get("h1", "N/A"),
            "volume_24h": volume.get("h24", "N/A"),

            "price_change_5m": price_change.get("m5", "N/A"),
            "price_change_1h": price_change.get("h1", "N/A"),
            "price_change_6h": price_change.get("h6", "N/A"),
            "price_change_24h": price_change.get("h24", "N/A"),

            "txns_1h_buys": h1_txns.get("buys", "N/A"),
            "txns_1h_sells": h1_txns.get("sells", "N/A"),
            "txns_24h_buys": h24_txns.get("buys", "N/A"),
            "txns_24h_sells": h24_txns.get("sells", "N/A"),

            "pair_created": oldest_created_at if oldest_created_at is not None else pair.get("pairCreatedAt", "N/A"),
            "dex": pair.get("dexId", "Unknown"),
            "pair_url": pair.get("url", ""),
            "pool_address": pair.get("pairAddress", ""),

            "image_url": info.get("imageUrl", ""),
            "website_url": website_url,
            "twitter_url": twitter_url,
            "telegram_url": telegram_url,
        }

    except Exception:
        return None


async def get_prices_batch(
    contract_addresses: list[str], chain_id: str, cache_ttl_seconds: int = 3,
) -> dict[str, float]:
    """Batched USD price lookup for exit-monitoring loops with many open
    positions (domain/trading/auto_trade/exit_engine.py). One DexScreener
    call per <=30 contracts via the /tokens/v1/{chainId}/{addr1,addr2,...}
    endpoint, instead of one call per position -- so a monitor tick's
    DexScreener call count depends on how many DISTINCT tokens are held,
    not on the tick interval or position count beyond that. Verified live
    against DexScreener on 2026-09-28: this endpoint returns a bare list
    of pair objects (not {"pairs": [...]}), unlike get_token_card_info's
    /latest/dex/tokens/{addr} endpoint -- handled below.

    Returns {contract_address (as given, NOT lowercased): price_usd}.
    A contract with no matching chain_id pair, or that fails to fetch,
    is simply absent from the result -- callers must treat a missing key
    as "unknown this tick", never as zero, matching get_json's "None =
    unknown, not zero" convention.
    """
    unique = list(dict.fromkeys(contract_addresses))  # de-dupe, preserve order
    if not unique:
        return {}

    CHUNK = 30  # DexScreener's documented max addresses per batch call
    chunks = [unique[i:i + CHUNK] for i in range(0, len(unique), CHUNK)]

    async def _fetch_chunk(addrs: list[str]) -> dict[str, float]:
        url = f"{DEXSCREENER_ROOT_API}/tokens/v1/{chain_id}/{','.join(addrs)}"
        try:
            data = await get_json(url, cache_ttl_seconds=cache_ttl_seconds, timeout_seconds=10)
        except Exception:
            return {}
        if not isinstance(data, list):
            return {}

        by_addr_best: dict[str, dict] = {}
        for pair in data:
            base = (pair.get("baseToken") or {}).get("address")
            if not base:
                continue
            match = next((a for a in addrs if a.lower() == base.lower()), None)
            if not match:
                continue
            liq = _to_float((pair.get("liquidity") or {}).get("usd", 0))
            if match not in by_addr_best or liq > _to_float((by_addr_best[match].get("liquidity") or {}).get("usd", 0)):
                by_addr_best[match] = pair

        out: dict[str, float] = {}
        for addr, pair in by_addr_best.items():
            price = _to_float(pair.get("priceUsd"), default=0.0)
            if price > 0:
                out[addr] = price
        return out

    results = await asyncio.gather(*(_fetch_chunk(c) for c in chunks), return_exceptions=True)
    merged: dict[str, float] = {}
    for r in results:
        if isinstance(r, dict):
            merged.update(r)
    return merged


async def get_latest_token_profiles() -> list[dict]:
    """Fetch DexScreener's latest token-profiles feed (public, documented
    v1 endpoint: GET /token-profiles/latest/v1).

    v4 discovery upgrade: every entry in this feed has, by construction,
    a real DexScreener project profile — this is the authoritative
    source for the discovery layer's "profile required" filter, rather
    than a heuristic like "an image_url is present" (which the feed's
    own `icon` field would not reliably distinguish from a placeholder).

    Each entry has the shape: {"url", "chainId", "tokenAddress", "icon",
    "header", "description", "links"}. Cached 30s — this is a shared,
    global feed (not scoped to one token), so it is fetched at most once
    per discovery cycle regardless of how many candidates are checked.
    """
    url = f"{DEXSCREENER_ROOT_API}/token-profiles/latest/v1"
    data = await get_json(url, cache_ttl_seconds=30, timeout_seconds=10)
    return data if isinstance(data, list) else []


async def get_latest_boosted_tokens() -> list[dict]:
    """Fetch DexScreener's latest token-boosts feed (public, documented
    v1 endpoint: GET /token-boosts/latest/v1).

    Same JSON shape as get_latest_token_profiles(). NOT treated as
    equivalent to "has a profile" by the discovery adapter — a boost is
    a paid promotion, not a verified profile — so this is only used as
    a discovery source when DISCOVERY_PROFILE_REQUIRED is disabled (see
    config/settings.py and domain/signals/_radar_discovery_adapter.py).
    """
    url = f"{DEXSCREENER_ROOT_API}/token-boosts/latest/v1"
    data = await get_json(url, cache_ttl_seconds=30, timeout_seconds=10)
    return data if isinstance(data, list) else []


def _pair_to_card(pair: dict, contract_address: str) -> dict:
    """Shared DexScreener pair -> card-shaped dict mapping, factored out
    of get_token_card_info() so search_pairs_by_chain() (Discovery Engine
    B) produces snapshots in the exact same shape without duplicating
    field-mapping logic."""
    base_token = pair.get("baseToken") or {}
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    price_change = pair.get("priceChange") or {}
    txns = pair.get("txns") or {}
    info = pair.get("info") or {}

    websites = info.get("websites") or []
    socials = info.get("socials") or []

    website_url = ""
    twitter_url = ""
    telegram_url = ""

    if websites and isinstance(websites, list):
        first_site = websites[0]
        if isinstance(first_site, dict):
            website_url = first_site.get("url", "")

    if socials and isinstance(socials, list):
        for social in socials:
            if not isinstance(social, dict):
                continue
            social_type = (social.get("type") or "").lower()
            social_url = social.get("url", "")
            if social_type in ["twitter", "x"]:
                twitter_url = social_url
            elif social_type == "telegram":
                telegram_url = social_url

    h1_txns = txns.get("h1") or {}
    h24_txns = txns.get("h24") or {}

    return {
        "name": base_token.get("name", "Unknown"),
        "symbol": base_token.get("symbol", "???"),
        "contract": contract_address,
        "chain": pair.get("chainId", "unknown"),

        "price": pair.get("priceUsd", "N/A"),
        "market_cap": pair.get("marketCap", "N/A"),
        "fdv": pair.get("fdv", "N/A"),
        "liquidity": liquidity.get("usd", "N/A"),

        "volume_1h": volume.get("h1", "N/A"),
        "volume_24h": volume.get("h24", "N/A"),

        "price_change_5m": price_change.get("m5", "N/A"),
        "price_change_1h": price_change.get("h1", "N/A"),
        "price_change_6h": price_change.get("h6", "N/A"),
        "price_change_24h": price_change.get("h24", "N/A"),

        "txns_1h_buys": h1_txns.get("buys", "N/A"),
        "txns_1h_sells": h1_txns.get("sells", "N/A"),
        "txns_24h_buys": h24_txns.get("buys", "N/A"),
        "txns_24h_sells": h24_txns.get("sells", "N/A"),

        "pair_created": pair.get("pairCreatedAt", "N/A"),
        "dex": pair.get("dexId", "Unknown"),
        "pair_url": pair.get("url", ""),
        "pool_address": pair.get("pairAddress", ""),

        "image_url": info.get("imageUrl", ""),
        "website_url": website_url,
        "twitter_url": twitter_url,
        "telegram_url": telegram_url,
    }


async def search_pairs_by_chain(chain_id: str, query: str, limit: int = 50) -> list[dict]:
    """
    Discovery Engine B (Robinhood Chain) building block: DexScreener's
    public /search endpoint, filtered client-side to `chain_id` and
    normalized to the same card shape as get_token_card_info().

    NOTE: DexScreener's public API has no "list every pair on chain X"
    endpoint -- /search requires a query term. `query` is caller-
    supplied (config.settings.ROBINHOOD_CHAIN_ID by default) so this is
    an approximation of "every Robinhood Chain pair", not an exhaustive
    chain scan. Combined with get_latest_token_profiles()/
    get_latest_boosted_tokens() (both true full-feed, chain-filterable
    endpoints) in the discovery layer for better coverage.

    Cached 20s via the shared resilience helper, same as every other
    DexScreener call in this module.
    """
    url = f"{DEXSCREENER_API}/search?q={query}"
    data = await get_json(url, cache_ttl_seconds=20, timeout_seconds=10)
    if not data:
        return []

    pairs = data.get("pairs") or []
    results = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if (pair.get("chainId") or "").strip().lower() != chain_id.strip().lower():
            continue
        contract = ((pair.get("baseToken") or {}).get("address") or "").strip()
        if not contract:
            continue
        results.append(_pair_to_card(pair, contract))
        if len(results) >= limit:
            break
    return results
