"""
providers/marketdata/_provider_circuit_breaker.py

Surgical fix for the Ankr / Solana Tracker provider-resilience gap (see
AlphaPulse Provider Resilience task, 2026-08-28): Solana Tracker (both the
liquidity/bundle-risk lookups in providers.marketdata.solanatracker and the
paginated holder fallback in domain.intelligence._solana_tracker_holder_fallback)
had no memory of past failures. A 403 (out of credits), an auth failure, or a
connection failure on token N did not stop the exact same request from being
retried on token N+1, N+2, ... forever -- burning a network round-trip (and a
few seconds of latency) on every single candidate for as long as the outage
lasted, with no automatic recovery signal once the provider came back.

This module does NOT reimplement `providers.rpc.multi_rpc_manager`'s
queue/circuit-breaker machinery -- that stays exactly as built and is scoped
to RPC traffic behind the Provider Gateway (Helius/Alchemy/dRPC/QuickNode/
Ankr). This is the lighter-weight equivalent for the market-data / indexed-
API family, deliberately mirroring the same design vocabulary
(`_ProviderHealth`, consecutive-failure threshold, cooldown, half-open probe)
so the two systems read the same way, without sharing code that would couple
an RPC-JSON dispatch loop to a plain REST client.

Design (per provider key, in-process, no external storage):
  - CLOSED (healthy): every call is allowed through normally.
  - OPEN (broken): calls are skipped without hitting the network at all
    until the cooldown elapses -- this is the "stop being repeatedly
    called" requirement.
  - HALF-OPEN (probe): once the cooldown elapses, exactly one call is let
    through as a health probe. Success closes the circuit (full reset).
    Failure re-opens it and restarts the cooldown -- this is the
    "automatically recovering when the provider becomes healthy again"
    requirement, without hammering a still-down provider every request in
    the meantime.

Two independent failure classes are tracked with two different thresholds,
because they mean very different things operationally:
  - AUTH/CREDIT failures (401 Unauthorized, 402 Payment Required, 403
    Forbidden) mean the request will keep failing identically until a human
    fixes the API key or billing -- there is nothing to gain from a few
    retries first, so these trip the circuit almost immediately
    (default threshold 1).
  - TRANSIENT failures (429, 5xx, timeouts, connection errors) can and do
    clear up on their own, so these use a slightly more forgiving threshold
    (default 3) before the circuit opens, matching the tolerance already
    used by providers.rpc.multi_rpc_manager for the same failure classes.

A provider that is not configured (no API key) is simply never called by
its own module-level guard -- that is a configuration state, not a health
state, and is intentionally NOT modeled here.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

try:
    from config.settings import (
        SOLANA_TRACKER_CIRCUIT_BREAKER_THRESHOLD,
        SOLANA_TRACKER_CIRCUIT_BREAKER_AUTH_THRESHOLD,
        SOLANA_TRACKER_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    )
except ImportError:  # pragma: no cover - config always present in production
    SOLANA_TRACKER_CIRCUIT_BREAKER_THRESHOLD = 3
    SOLANA_TRACKER_CIRCUIT_BREAKER_AUTH_THRESHOLD = 1
    SOLANA_TRACKER_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 120

logger = logging.getLogger("AlphaPulse.ProviderCircuitBreaker")

# Failure classification -- callers pass one of these to record_failure() so
# the breaker can apply the right threshold. Kept as plain strings (not an
# enum) to match this codebase's existing style for small status constants
# (see domain.intelligence.holder_state.PROVIDER_ERROR and friends).
FAILURE_AUTH_OR_CREDITS = "auth_or_credits"   # 401 / 402 / 403
FAILURE_TRANSIENT = "transient"               # 429 / 5xx / timeout / connection error


class _CircuitState:
    """Per-provider circuit-breaker state. Mirrors
    providers.rpc.multi_rpc_manager._ProviderHealth's shape deliberately --
    same fields, same semantics -- so a reader familiar with one understands
    the other immediately."""

    __slots__ = (
        "consecutive_failures",
        "consecutive_auth_failures",
        "opened_at",
        "probe_in_flight",
    )

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.consecutive_auth_failures = 0
        self.opened_at: Optional[float] = None
        # Guards against two concurrent callers both being let through as
        # the "one probe" the instant the cooldown elapses -- only the
        # first caller to observe half-open gets the probe; concurrent
        # others are still short-circuited until the probe resolves.
        self.probe_in_flight = False

    def is_open(self) -> bool:
        return self.opened_at is not None

    def cooldown_elapsed(self) -> bool:
        if self.opened_at is None:
            return True
        return (time.monotonic() - self.opened_at) >= SOLANA_TRACKER_CIRCUIT_BREAKER_COOLDOWN_SECONDS


_state: dict[str, _CircuitState] = {}


def _get(provider_key: str) -> _CircuitState:
    state = _state.get(provider_key)
    if state is None:
        state = _CircuitState()
        _state[provider_key] = state
    return state


def allow_request(provider_key: str) -> bool:
    """True if a call to this provider should be attempted at all.

    CLOSED -> always True. OPEN and cooldown not yet elapsed -> False (this
    is what stops a persistently unhealthy provider from being called on
    every single token). OPEN and cooldown elapsed -> True exactly once
    (the half-open recovery probe); concurrent callers during that single
    probe attempt are still turned away so a burst of concurrent signal
    scans doesn't turn "one probe" into "hammer it the instant the timer
    expires".
    """
    state = _get(provider_key)
    if not state.is_open():
        return True

    if not state.cooldown_elapsed():
        return False

    if state.probe_in_flight:
        # Someone else already claimed the recovery probe this window.
        return False

    state.probe_in_flight = True
    logger.info(
        f"[CircuitBreaker] {provider_key}: cooldown elapsed, allowing one "
        f"recovery probe request"
    )
    return True


def record_success(provider_key: str) -> None:
    """A call to this provider completed successfully -- reset it to fully
    healthy. A single success (whether a normal closed-circuit call or the
    one half-open probe) is enough to close the circuit again; this
    intentionally does not require several consecutive good responses,
    since staying openly degraded any longer than necessary is exactly the
    "unnecessarily stop AlphaPulse from producing valid signals" failure
    mode this breaker exists to prevent.
    """
    state = _get(provider_key)
    was_open = state.is_open()
    state.consecutive_failures = 0
    state.consecutive_auth_failures = 0
    state.opened_at = None
    state.probe_in_flight = False
    if was_open:
        logger.info(f"[CircuitBreaker] {provider_key}: recovered, circuit closed")


def record_failure(provider_key: str, failure_type: str = FAILURE_TRANSIENT) -> None:
    """A call to this provider failed. `failure_type` selects which
    threshold applies (see module docstring). Trips the circuit once the
    relevant consecutive-failure count reaches its threshold; a failed
    half-open probe re-opens the circuit and restarts the cooldown rather
    than compounding onto whatever count was there before the probe.
    """
    state = _get(provider_key)
    probe_failed = state.probe_in_flight
    state.probe_in_flight = False

    if failure_type == FAILURE_AUTH_OR_CREDITS:
        state.consecutive_auth_failures += 1
        threshold = SOLANA_TRACKER_CIRCUIT_BREAKER_AUTH_THRESHOLD
        tripped = state.consecutive_auth_failures >= threshold
    else:
        state.consecutive_failures += 1
        threshold = SOLANA_TRACKER_CIRCUIT_BREAKER_THRESHOLD
        tripped = state.consecutive_failures >= threshold

    if tripped or probe_failed:
        if state.opened_at is None:
            logger.warning(
                f"[CircuitBreaker] {provider_key}: circuit OPEN "
                f"(failure_type={failure_type}, threshold={threshold}, "
                f"cooldown={SOLANA_TRACKER_CIRCUIT_BREAKER_COOLDOWN_SECONDS}s) "
                f"-- further calls suppressed until cooldown elapses"
            )
        state.opened_at = time.monotonic()


def is_open(provider_key: str) -> bool:
    """Diagnostic/test helper -- current breaker state without mutating it
    or consuming a recovery probe slot."""
    return _get(provider_key).is_open()


def reset(provider_key: str) -> None:
    """Test helper: force a provider back to a clean CLOSED state."""
    _state.pop(provider_key, None)
