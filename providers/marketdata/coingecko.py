from config.settings import COINGECKO_API
from providers.marketdata._resilience import get_json


async def get_solana_price() -> dict | None:
    """Fetch current Solana (SOL) price and market data.

    v4: cached (30s TTL) and retried via the shared resilience helper."""
    url = f"{COINGECKO_API}/simple/price"
    params = {
        "ids": "solana",
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_24hr_vol": "true",
        "include_market_cap": "true",
    }

    data = await get_json(url, params=params, cache_ttl_seconds=30)
    if data is None:
        return None

    sol = data.get("solana", {})
    return {
        "price": sol.get("usd", "N/A"),
        "change_24h": sol.get("usd_24h_change", 0),
        "volume_24h": sol.get("usd_24h_vol", 0),
        "market_cap": sol.get("usd_market_cap", 0),
    }


async def get_global_market() -> dict | None:
    """Fetch global crypto market data.

    v4: cached (60s TTL) and retried via the shared resilience helper."""
    url = f"{COINGECKO_API}/global"

    data = await get_json(url, cache_ttl_seconds=60)
    if data is None:
        return None

    global_data = data.get("data", {})
    return {
        "total_market_cap": global_data.get("total_market_cap", {}).get("usd", 0),
        "total_volume": global_data.get("total_volume", {}).get("usd", 0),
        "btc_dominance": global_data.get("market_cap_percentage", {}).get("btc", 0),
        "active_cryptos": global_data.get("active_cryptocurrencies", 0),
    }
