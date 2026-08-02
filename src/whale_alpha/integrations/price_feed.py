"""USD price feed — fixes the `/ 1` placeholder flagged in engines/auto_trading.py
and powers the entry-zone calculation (engines/signal.py) and the
percent-increase price-alert engine (engines/price_alerts.py).

--- UPDATED: Jupiter Price API V2 -> V3 (2026-08-02) ---
This module originally defaulted to Jupiter's Price API V2
(`https://api.jup.ag/price/v2`), which was free and keyless. Jupiter has
since fully deprecated V2 — both the paid host (`api.jup.ag/price/v2`) and
the free `lite-api.jup.ag` host it was migrated to were sunset (the latter
on 31 Jan 2026) — so every request against the old default now 404s and
every price-dependent feature (auto-trading, entry zones, price alerts)
silently no-ops. See the "SOL/USD price unavailable" warnings in
engines/scheduler.py and engines/price_alerts.py if this ever regresses.

ASSUMPTION (flagged explicitly, same spirit as the repo's other
TODO(integration) notes): this now defaults to Jupiter's Price API **V3**
(`https://api.jup.ag/price/v3?ids=mint1,mint2`), which returns a flat map
`{"<mint>": {"usdPrice": <float>, "liquidity": ..., "blockId": ...,
"decimals": ..., "createdAt": ..., "priceChange24h": ...}, ...}` — no
`"data"` wrapper, and the price field is `usdPrice` (a number), not `price`
(a decimal string) like V2. Unlike V2, **V3 requires an API key** sent as
the `x-api-key` header (not `Authorization: Bearer`) — get one free at
https://portal.jup.ag (a free tier exists; see their pricing page). Without
`PRICE_FEED_API_KEY` set, expect 401s here, same practical effect as the old
404s. If you set `PRICE_FEED_API_BASE` to point at a different provider
entirely (Birdeye, CoinGecko Pro, your own aggregator), this module still
tries both `usdPrice` and `price` field names and both the wrapped and
unwrapped response shape for compatibility — adjust `_fetch_prices` further
if your provider's contract differs from both.

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

_JUPITER_PRICE_API_DEFAULT = "https://api.jup.ag/price/v3"


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
        # V3 uses x-api-key, not the old Authorization: Bearer scheme V2 used.
        headers["x-api-key"] = env.PRICE_FEED_API_KEY

    res = await client.get(base, params={"ids": ",".join(mints)}, headers=headers)
    if res.status_code >= 400:
        raise PriceFeedError(f"Price feed request failed: {res.status_code} {res.text}")

    body = res.json()
    data = body.get("data", body)  # tolerate a provider that wraps in {"data": ...} (old V2 shape)

    out: dict[str, float] = {}
    for mint in mints:
        entry = data.get(mint)
        if not entry:
            continue
        # V3 uses "usdPrice"; fall back to V2's "price" for a custom/non-Jupiter provider.
        raw_price = entry.get("usdPrice", entry.get("price")) if isinstance(entry, dict) else entry
        if raw_price is None:
            continue
        try:
            out[mint] = float(raw_price)
        except (TypeError, ValueError):
            log.warning("Unparseable price value from feed", mint=mint, raw=raw_price)

    return out
