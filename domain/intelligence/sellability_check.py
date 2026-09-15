"""
domain/intelligence/sellability_check.py

Production Risk Layer -- Live Buy->Sell Sellability Simulation.

Every existing security signal in this codebase (GoPlus / RugCheck
`hard_reject_reasons()`, `risk_engine.evaluate_verified_red_flags()`,
holder-concentration scoring, LP-lock verification, funding-cluster and
deployer-history checks) is either a *declared-attribute* check (does
the mint have an active freeze/mint authority, is it flagged blacklisted,
etc.) or a *statistical/heuristic* check (holder concentration, funding
graph, wash-trading estimates). None of them actually attempt the trade.

That is a real, known gap: a token can pass every declared-attribute and
heuristic check and still be unsellable in practice -- a custom
transfer-hook, a post-launch blacklist the security providers haven't
indexed yet, an extreme/hidden sell tax, or a pool with no real exit
liquidity for anything but the deployer's own wallet. Static analysis
cannot see any of that; only actually attempting to route the trade can.

This module closes that gap by simulating the exact two-step trade a
real buyer would make, using each chain's own public, read-only quote
endpoint -- NO wallet, NO signing, NO broadcast, NO funds at risk on
either chain:

  * Solana:          domain.trading.real.jupiter_swap.get_quote
                      (Jupiter -- the same quote function real trades use)
  * Robinhood Chain:  domain.trading.real.robinhood_swap.get_quote
                      (Uniswap's Trading API -- same quote function real
                      trades use. Only the read-only /quote step is ever
                      called here, never /swap -- so no approval or
                      funded wallet is required; a fixed, unfunded probe
                      address is used purely for quote routing.)

  1. BUY leg:  native (SOL / ETH) -> token   (small, configurable probe size)
  2. SELL leg: token -> native                (selling back exactly what
                                                the BUY leg would have
                                                produced)

A token is rejected as unsellable when:
  * No route exists for the BUY leg (no real entry liquidity), or
  * No route exists for the SELL leg -- the single hardest-to-fake
    honeypot signal there is: it means the chain's own router, given the
    exact number of base units a real buyer would be holding, cannot
    find any way to convert them back to the native asset right now,
    against the live pool, and
  * A route exists for both legs but the round-trip loses materially
    more than ordinary slippage/fees would explain (SELLABILITY_MAX_
    ROUND_TRIP_LOSS_PCT), consistent with a hidden sell tax or a
    razor-thin trap pool.

Known limitation (both chains): this is a router/quote-level simulation,
not a full transaction execution trace. A token whose contract reverts
sell transfers via custom code (rather than affecting the quoted price)
may not be caught by the quote math alone -- that class of honeypot is
what GoPlus's own `is_honeypot` / `cannot_sell_all` static flags
(already checked earlier in `hard_reject_reasons()`, for both chains)
are specifically designed to catch. This module is a second, independent
layer on top of that, not a replacement for it.

Fail-closed by default (SELLABILITY_FAIL_CLOSED): if the simulation
itself cannot be completed (RPC/API unreachable, rate-limited, timed
out), that is an UNKNOWN sellability state, not a passing one -- same
"never fabricate a positive from an absence" convention this codebase
already applies to GoPlus security-data unavailability.
"""

from __future__ import annotations

import asyncio
import logging
import time

from config.settings import (
    SELLABILITY_CHECK_ENABLED,
    SELLABILITY_PROBE_SOL_AMOUNT,
    SELLABILITY_PROBE_ETH_AMOUNT,
    SELLABILITY_SLIPPAGE_BPS,
    SELLABILITY_MAX_ROUND_TRIP_LOSS_PCT,
    SELLABILITY_MAX_PRICE_IMPACT_PCT,
    SELLABILITY_CACHE_TTL_SECONDS,
    SELLABILITY_FAIL_CLOSED,
    SELLABILITY_EVM_MAX_RETRIES,
    SELLABILITY_EVM_RETRY_BACKOFF_SECONDS,
    ROBINHOOD_CHAIN_ID,
    ROBINHOOD_SELLABILITY_PROBE_ADDRESS,
)
from domain.trading.real.jupiter_swap import (
    get_quote as _jupiter_get_quote,
    WRAPPED_SOL_MINT,
    SwapError as JupiterSwapError,
)
from domain.trading.real.robinhood_swap import (
    get_quote as _robinhood_get_quote,
    NATIVE_ETH_ADDRESS,
    SwapError as RobinhoodSwapError,
)
from providers.cache import get_cache

logger = logging.getLogger("WhaleAlpha.SellabilityCheck")

LAMPORTS_PER_SOL = 1_000_000_000
WEI_PER_ETH = 1_000_000_000_000_000_000

# Exceptions from either chain's swap client that mean "the simulation
# itself did not complete" (network/API/RPC failure), as opposed to a
# completed simulation that came back showing no route.
_SIMULATION_ERRORS = (JupiterSwapError, RobinhoodSwapError, Exception)


def _neutral_result(*, checked: bool, reject: bool, reasons: list[str] | None = None,
                     warnings: list[str] | None = None, details: dict | None = None) -> dict:
    return {
        "checked": checked,
        "sellable": (not reject) if checked else None,
        "reject": reject,
        "reasons": reasons or [],
        "warnings": warnings or [],
        "details": details or {},
    }


def _price_impact_pct(quote: dict | None) -> float | None:
    if not quote:
        return None
    try:
        raw = quote.get("priceImpactPct")
        if raw is None:
            return None
        return abs(float(raw)) * 100.0
    except (TypeError, ValueError):
        return None


def _is_no_route_error(exc: Exception) -> bool:
    """True for Uniswap's own decisive 'no route with sufficient liquidity'
    error -- as distinct from a transient failure. Production evidence
    (2026-09-13): this comes back as a raised SwapError, not a quote
    response with outAmount=0, so it has to be caught and reclassified
    here rather than falling through to the generic unverified/fail-
    closed path, which would otherwise log it (and treat it) identically
    to an actual Uniswap outage."""
    return "NoRouteFoundError" in str(exc)


def _is_transient_upstream_error(exc: Exception) -> bool:
    """True for Uniswap's own explicitly-retryable error class.
    Production evidence (2026-09-13): errorCode 'UpstreamTimeoutError',
    whose own `detail` field says 'the request may succeed on retry' --
    treating this identically to a hard failure (no retry) was rejecting
    otherwise-legitimate, sellable tokens on nothing more than Uniswap's
    own transient routing-dependency hiccups."""
    return "UpstreamTimeoutError" in str(exc)


async def _evm_quote_with_retry(*args, **kwargs) -> dict:
    """Thin retry wrapper around `_robinhood_get_quote` -- retries only
    `_is_transient_upstream_error` failures, up to SELLABILITY_EVM_MAX_
    RETRIES times with linear backoff. Any other error (including a
    decisive no-route error) is raised immediately, unretried."""
    last_exc: Exception | None = None
    for attempt in range(SELLABILITY_EVM_MAX_RETRIES + 1):
        try:
            return await _robinhood_get_quote(*args, **kwargs)
        except RobinhoodSwapError as e:
            last_exc = e
            if _is_transient_upstream_error(e) and attempt < SELLABILITY_EVM_MAX_RETRIES:
                await asyncio.sleep(SELLABILITY_EVM_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise
    raise last_exc  # pragma: no cover -- loop always returns or raises above


def _evaluate_round_trip(*, probe_units: int, out_amount: int, sell_units: int,
                          buy_quote: dict | None, sell_quote: dict | None,
                          probe_label: str, probe_display) -> dict:
    """
    Shared BUY->SELL round-trip evaluation, chain-agnostic once both legs
    have already been quoted in base units of the chain's native asset
    (lamports for Solana, wei for Robinhood Chain/EVM).
    """
    round_trip_loss_pct = ((probe_units - sell_units) / probe_units) * 100.0
    buy_impact = _price_impact_pct(buy_quote)
    sell_impact = _price_impact_pct(sell_quote)

    reasons: list[str] = []
    warnings: list[str] = []

    if round_trip_loss_pct >= SELLABILITY_MAX_ROUND_TRIP_LOSS_PCT:
        reasons.append(
            f"Buy->sell simulation loses {round_trip_loss_pct:.1f}% round-trip "
            f"(threshold {SELLABILITY_MAX_ROUND_TRIP_LOSS_PCT:.0f}%) -- "
            "likely hidden sell tax or trap liquidity"
        )
    elif round_trip_loss_pct >= SELLABILITY_MAX_ROUND_TRIP_LOSS_PCT * 0.6:
        warnings.append(f"Elevated simulated round-trip loss ({round_trip_loss_pct:.1f}%)")

    for label, impact in (("buy", buy_impact), ("sell", sell_impact)):
        if impact is not None and impact >= SELLABILITY_MAX_PRICE_IMPACT_PCT:
            reasons.append(
                f"Extreme {label}-side price impact ({impact:.1f}%) -- "
                "insufficient real liquidity to safely exit"
            )

    return _neutral_result(
        checked=True,
        reject=bool(reasons),
        reasons=reasons,
        warnings=warnings,
        details={
            probe_label: probe_display,
            "buy_out_amount": out_amount,
            "sell_out_native_units": sell_units,
            "round_trip_loss_pct": round(round_trip_loss_pct, 2),
            "buy_price_impact_pct": buy_impact,
            "sell_price_impact_pct": sell_impact,
        },
    )


async def _verify_sellability_solana(contract: str, cache, cache_key: str) -> dict:
    probe_lamports = max(int(SELLABILITY_PROBE_SOL_AMOUNT * LAMPORTS_PER_SOL), 1)
    started = time.monotonic()
    try:
        buy_quote = await _jupiter_get_quote(WRAPPED_SOL_MINT, contract, probe_lamports,
                                              slippage_bps=SELLABILITY_SLIPPAGE_BPS)
        out_amount = int((buy_quote or {}).get("outAmount") or 0)

        if out_amount <= 0:
            result = _neutral_result(
                checked=True, reject=True,
                reasons=["No buy route available (no real entry liquidity)"],
                details={"probe_sol": SELLABILITY_PROBE_SOL_AMOUNT},
            )
            await _safe_cache_set(cache, cache_key, result)
            return result

        sell_quote = await _jupiter_get_quote(contract, WRAPPED_SOL_MINT, out_amount,
                                               slippage_bps=SELLABILITY_SLIPPAGE_BPS)
        sell_lamports = int((sell_quote or {}).get("outAmount") or 0)

        if sell_lamports <= 0:
            result = _neutral_result(
                checked=True, reject=True,
                reasons=["No sell route available -- token cannot be sold back to SOL (honeypot)"],
                details={"probe_sol": SELLABILITY_PROBE_SOL_AMOUNT, "buy_out_amount": out_amount},
            )
            await _safe_cache_set(cache, cache_key, result)
            return result

        result = _evaluate_round_trip(
            probe_units=probe_lamports, out_amount=out_amount, sell_units=sell_lamports,
            buy_quote=buy_quote, sell_quote=sell_quote,
            probe_label="probe_sol", probe_display=SELLABILITY_PROBE_SOL_AMOUNT,
        )
        result["details"]["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        await _safe_cache_set(cache, cache_key, result)
        return result

    except _SIMULATION_ERRORS as e:
        logger.warning(f"Sellability simulation failed for {contract[:8]} (Jupiter): {e}")
        return _unverified_result()


async def _verify_sellability_evm(contract: str, cache, cache_key: str) -> dict:
    probe_wei = max(int(SELLABILITY_PROBE_ETH_AMOUNT * WEI_PER_ETH), 1)
    probe_address = ROBINHOOD_SELLABILITY_PROBE_ADDRESS
    started = time.monotonic()
    try:
        try:
            buy_quote = await _evm_quote_with_retry(NATIVE_ETH_ADDRESS, contract, probe_wei,
                                                     slippage_bps=SELLABILITY_SLIPPAGE_BPS,
                                                     swapper=probe_address)
        except RobinhoodSwapError as e:
            if _is_no_route_error(e):
                result = _neutral_result(
                    checked=True, reject=True,
                    reasons=["No buy route available (no real entry liquidity)"],
                    details={"probe_eth": SELLABILITY_PROBE_ETH_AMOUNT},
                )
                await _safe_cache_set(cache, cache_key, result)
                return result
            raise
        out_amount = int((buy_quote or {}).get("outAmount") or 0)

        if out_amount <= 0:
            result = _neutral_result(
                checked=True, reject=True,
                reasons=["No buy route available (no real entry liquidity)"],
                details={"probe_eth": SELLABILITY_PROBE_ETH_AMOUNT},
            )
            await _safe_cache_set(cache, cache_key, result)
            return result

        try:
            sell_quote = await _evm_quote_with_retry(contract, NATIVE_ETH_ADDRESS, out_amount,
                                                      slippage_bps=SELLABILITY_SLIPPAGE_BPS,
                                                      swapper=probe_address)
        except RobinhoodSwapError as e:
            if _is_no_route_error(e):
                result = _neutral_result(
                    checked=True, reject=True,
                    reasons=["No sell route available -- token cannot be sold back to ETH (honeypot)"],
                    details={"probe_eth": SELLABILITY_PROBE_ETH_AMOUNT, "buy_out_amount": out_amount},
                )
                await _safe_cache_set(cache, cache_key, result)
                return result
            raise
        sell_wei = int((sell_quote or {}).get("outAmount") or 0)

        if sell_wei <= 0:
            result = _neutral_result(
                checked=True, reject=True,
                reasons=["No sell route available -- token cannot be sold back to ETH (honeypot)"],
                details={"probe_eth": SELLABILITY_PROBE_ETH_AMOUNT, "buy_out_amount": out_amount},
            )
            await _safe_cache_set(cache, cache_key, result)
            return result

        result = _evaluate_round_trip(
            probe_units=probe_wei, out_amount=out_amount, sell_units=sell_wei,
            buy_quote=buy_quote, sell_quote=sell_quote,
            probe_label="probe_eth", probe_display=SELLABILITY_PROBE_ETH_AMOUNT,
        )
        result["details"]["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        await _safe_cache_set(cache, cache_key, result)
        return result

    except _SIMULATION_ERRORS as e:
        logger.warning(f"Sellability simulation failed for {contract[:8]} (Uniswap/Robinhood Chain): {e}")
        return _unverified_result()


def _unverified_result() -> dict:
    # Unreachable/unverifiable this cycle -- fail closed per Production
    # Validation Policy (same convention as GoPlus security-data
    # unavailability): an unknown sellability state is never treated as
    # a passing one. Never cached -- a transient outage must not lock a
    # token out for the full cache TTL once the RPC/API recovers.
    return _neutral_result(
        checked=False,
        reject=SELLABILITY_FAIL_CLOSED,
        reasons=["Sellability unverified (live simulation unavailable)"] if SELLABILITY_FAIL_CLOSED else [],
        warnings=[] if SELLABILITY_FAIL_CLOSED else ["Sellability unverified (live simulation unavailable)"],
    )


async def verify_sellability(contract: str, *, chain: str = "solana") -> dict:
    """
    Runs the live BUY->SELL quote simulation for `contract` on `chain`.

    Returns:
        {
          "checked": bool,          # did the simulation actually run/complete
          "sellable": bool | None,  # None only when checked is False
          "reject": bool,           # True => candidate must not be alerted on
          "reasons": [...],         # non-empty only when reject is True
          "warnings": [...],
          "details": {...},         # probe size, quoted amounts, round-trip %, etc.
        }

    Callers must treat `reject=True` as an absolute veto, the same way
    `hard_reject_reasons()` and `evaluate_verified_red_flags()` already
    are treated elsewhere in this pipeline -- this is not an input to
    scoring, it is a hard gate.

    Supported chains today: "solana" (via Jupiter) and whatever
    `config.settings.ROBINHOOD_CHAIN_ID` is set to (via Uniswap's
    Trading API on Robinhood Chain). Any other chain value gets a
    non-blocking neutral result, the same "enrichments that don't
    generalize to another chain are simply omitted" convention already
    documented in domain.signals.candidate_validation.
    """
    if not SELLABILITY_CHECK_ENABLED:
        return _neutral_result(checked=False, reject=False,
                                warnings=["sellability_check_disabled"])

    if not contract:
        return _neutral_result(checked=False, reject=SELLABILITY_FAIL_CLOSED,
                                reasons=["sellability_unverified (missing contract)"] if SELLABILITY_FAIL_CLOSED else [])

    if chain not in ("solana", ROBINHOOD_CHAIN_ID):
        return _neutral_result(checked=False, reject=False,
                                warnings=["sellability_check_unsupported_for_chain"])

    if chain == "solana":
        probe_units = max(int(SELLABILITY_PROBE_SOL_AMOUNT * LAMPORTS_PER_SOL), 1)
    else:
        probe_units = max(int(SELLABILITY_PROBE_ETH_AMOUNT * WEI_PER_ETH), 1)
    cache_key = f"sellability:{chain}:{contract}:{probe_units}"

    cache = await get_cache()
    try:
        cached = await cache.get(cache_key)
    except Exception as e:  # cache backend hiccup must never block the pipeline
        logger.warning(f"Sellability cache read failed for {contract[:8]}: {e}")
        cached = None
    if cached is not None:
        return cached

    if chain == "solana":
        return await _verify_sellability_solana(contract, cache, cache_key)
    return await _verify_sellability_evm(contract, cache, cache_key)


async def _safe_cache_set(cache, key: str, value: dict) -> None:
    try:
        await cache.set(key, value, SELLABILITY_CACHE_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"Sellability cache write failed for key={key}: {e}")
