"""Tests for integrations/price_feed.py, using httpx.MockTransport so no real
network call is made. Exercises caching, the Jupiter-shaped response parsing,
and graceful handling of an unresolvable mint.
"""

from __future__ import annotations

import httpx
import pytest

from whale_alpha.integrations import price_feed

MINT_A = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINT_B = "So11111111111111111111111111111111111111112"


class _Env:
    """Minimal stand-in for whale_alpha.config.Env — only the fields
    price_feed.py actually reads."""

    PRICE_FEED_API_BASE = None
    PRICE_FEED_API_KEY = None
    PRICE_CACHE_TTL_SECONDS = 15


@pytest.fixture(autouse=True)
def _reset_cache():
    # The module-level cache is a singleton; clear it between tests so one
    # test's cached price doesn't leak into another.
    price_feed._cache = price_feed._PriceCache()
    yield


async def test_fetches_and_returns_prices_for_known_mints():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {MINT_A: {"price": "1.23"}, MINT_B: {"price": "150.0"}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    prices = await price_feed.get_prices_usd(client, _Env(), [MINT_A, MINT_B])
    assert prices[MINT_A] == 1.23
    assert prices[MINT_B] == 150.0


async def test_omits_unresolvable_mints_rather_than_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {MINT_A: {"price": "1.23"}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    prices = await price_feed.get_prices_usd(client, _Env(), [MINT_A, MINT_B])
    assert prices == {MINT_A: 1.23}
    assert MINT_B not in prices


async def test_second_call_within_ttl_does_not_hit_the_network_again():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"data": {MINT_A: {"price": "1.0"}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    env = _Env()
    first = await price_feed.get_price_usd(client, env, MINT_A)
    second = await price_feed.get_price_usd(client, env, MINT_A)
    assert first == second == 1.0
    assert call_count == 1


async def test_provider_error_response_returns_empty_rather_than_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    prices = await price_feed.get_prices_usd(client, _Env(), [MINT_A])
    assert prices == {}
