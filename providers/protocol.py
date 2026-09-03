"""
providers/protocol.py

NEW in v4 (Architecture Bible §6 — Provider Abstraction Architecture).

Defines the single interface every outbound provider — RPC and market-data
alike — implements. Before v4 there were two unrelated provider layers:
`multi_rpc_manager` (queue, cache, circuit breaker, retry) for RPC, and four
standalone modules (dexscreener/geckoterminal/coingecko/goplus) that each
opened a fresh aiohttp.ClientSession per call with no cache, no retry, no
shared rate limiter (verified in the audit, §4).

This module does not reimplement resilience logic. It formalizes the
contract so both provider families can sit behind the same Provider Gateway
(`providers/rpc/multi_rpc_manager.py`, logic unchanged per the Bible's
non-negotiable preservation list) instead of each provider family inventing
its own transport pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class ProviderKind(str, Enum):
    RPC = "rpc"
    MARKET_DATA = "market_data"


class CircuitState(str, Enum):
    CLOSED = "closed"       # healthy, requests flow normally
    HALF_OPEN = "half_open"  # probing after a trip
    OPEN = "open"            # tripped, requests short-circuited


@dataclass
class ProviderHealth:
    name: str
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    last_error: str | None = None
    success_rate_last_5m: float | None = None


@dataclass
class ProviderResult:
    provider: str
    method: str
    data: Any
    cache_hit: bool = False
    attempt: int = 1
    latency_ms: float | None = None
    meta: dict = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    """
    Every adapter behind the Provider Gateway implements this shape.

    `fetch` is intentionally generic (method + params) rather than one
    method per data type — this lets the Gateway apply queueing, caching,
    circuit-breaking, and dedup uniformly regardless of whether the call
    is an RPC method or a market-data endpoint, exactly the way
    `multi_rpc_manager` already does today for RPC calls only.
    """

    name: str
    kind: ProviderKind

    async def fetch(self, method: str, params: dict | None = None) -> ProviderResult: ...

    def health(self) -> ProviderHealth: ...
