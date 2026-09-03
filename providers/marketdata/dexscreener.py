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


async def get_token_card_info(contract_address: str) -> dict | None:
    """
    Fetch richer token info for the automatic contract scanner.

    Uses DexScreener free API.
    Selects the Solana pair with the highest liquidity.
    """
    url = f"{DEXSCREENER_API}/tokens/{contract_address}"

    try:
        data = await get_json(url, cache_ttl_seconds=15, timeout_seconds=10)
        if data is None:
            return None

        pairs = data.get("pairs") or []
        if not pairs:
            return None

        solana_pairs = [
            pair for pair in pairs
            if pair.get("chainId") == "solana"
        ]

        usable_pairs = solana_pairs or pairs

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
