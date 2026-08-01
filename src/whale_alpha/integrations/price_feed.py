"""USD price feed — fixes the `/ 1` placeholder flagged in engines/auto_trading.py
and powers the entry-zone calculation (engines/signal.py) and the
percent-increase price-alert engine (engines/price_alerts.py).

ASSUMPTION (flagged explicitly, same spirit as the repo's other
TODO(integration) notes): with no `PRICE_FEED_API_BASE` configured, this
defaults to Jupiter's public Price API v2 (`https://api.jup.ag/price/v2`),
which returns `{"data": {"<mint>": {"price": "<decimal string>"}}}` for a
comma-separated list of mint addresses. This is a free, keyless, rate-limited
endpoint — fine for development and light production load, but you should
point `PRICE_FEED_API_BASE` at a paid feed (Birdeye, CoinGecko Pro, your own
aggregator) before relying on this for real trade sizing at scale. If you set
`PRICE_FEED_API_BASE`, this module assumes the same request/response shape
(`GET {base}?ids=mint1,mint2` -> `{"data": {mint: {"price": ...}}}`) and sends
`PRICE_FEED_API_KEY` as a Bearer token — adjust `_fetch_prices` if your
provider's contract differs.

Prices are cached in-process for `env.PRICE_CACHE_TTL_SECONDS` (default 15s)
per mint, keyed off a single shared cache so bursts of callers (scheduler,
auto-trading, manual trade sizing, price alerts) within the same tick don't
each fire a separate HTTP request.
"""

from __future__ import annotations

import time

import httpx

from whale_alpha.config import Env
from whale_alpha.utils.logger import child_logger

log = child_logger("priceFeed")

SOL_MINT = "So11111111111111111111111111111111111111112"

_JUPITER_PRICE_API_DEFAULT = "https://api.jup.ag/price/v2"


class PriceFeedError(Exception):
    pass


class _PriceCache:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, float]] = {}  # mint -> (price, fetched_at_monotonic)

    def get(self, mint: str, ttl_seconds: float) -> float | None:
        entry = self._entries.get(mint)
        if entry is None:
            return None
        price, fetched_at = entry
        if time.monotonic() - fetched_at > ttl_seconds:
            return None
        return price

    def set(self, mint: str, price: float) -> None:
        self._entries[mint] = (price, time.monotonic())


# Module-level singleton, same pattern as engines/monitor.py's event_buffer.
_cache = _PriceCache()


async def get_prices_usd(
    client: httpx.AsyncClient, env: Env, mints: list[str]
) -> dict[str, float]:
    """Returns {mint: usd_price} for every mint that was resolvable.

    Mints that fail to resolve (delisted/no liquidity/provider error) are
    simply omitted rather than raising, so a single bad mint in a batch
    doesn't block pricing the rest — callers should treat a missing key as
    "price unknown" and handle that explicitly (skip the trade, skip the
    alert) rather than assuming 0 or defaulting silently.
    """
    result: dict[str, float] = {}
    to_fetch: list[str] = []

    ttl = env.PRICE_CACHE_TTL_SECONDS
    for mint in dict.fromkeys(mints):  # de-dupe, preserve order
        cached = _cache.get(mint, ttl)
        if cached is not None:
            result[mint] = cached
        else:
            to_fetch.append(mint)

    if to_fetch:
        try:
            fetched = await _fetch_prices(client, env, to_fetch)
        except Exception as err:  # noqa: BLE001
            log.error("Price feed request failed", err=str(err), mints=to_fetch)
            fetched = {}
        for mint, price in fetched.items():
            _cache.set(mint, price)
            result[mint] = price

    return result


async def get_price_usd(client: httpx.AsyncClient, env: Env, mint: str) -> float | None:
    prices = await get_prices_usd(client, env, [mint])
    return prices.get(mint)


async def get_sol_price_usd(client: httpx.AsyncClient, env: Env) -> float | None:
    return await get_price_usd(client, env, SOL_MINT)


async def _fetch_prices(client: httpx.AsyncClient, env: Env, mints: list[str]) -> dict[str, float]:
    base = env.PRICE_FEED_API_BASE or _JUPITER_PRICE_API_DEFAULT
    headers = {}
    if env.PRICE_FEED_API_KEY:
        headers["Authorization"] = f"Bearer {env.PRICE_FEED_API_KEY}"

    res = await client.get(base, params={"ids": ",".join(mints)}, headers=headers)
    if res.status_code >= 400:
        raise PriceFeedError(f"Price feed request failed: {res.status_code} {res.text}")

    body = res.json()
    data = body.get("data", body)  # tolerate a provider that returns the map directly

    out: dict[str, float] = {}
    for mint in mints:
        entry = data.get(mint)
        if not entry:
            continue
        raw_price = entry.get("price") if isinstance(entry, dict) else entry
        if raw_price is None:
            continue
        try:
            out[mint] = float(raw_price)
        except (TypeError, ValueError):
            log.warning("Unparseable price value from feed", mint=mint, raw=raw_price)

    return out
