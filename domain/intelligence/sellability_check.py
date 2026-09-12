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
real buyer would make, using Jupiter's own public, read-only `/quote`
endpoint (the same endpoint `domain.trading.real.jupiter_swap.get_quote`
uses for real trades) -- NO wallet, NO signing, NO broadcast, NO funds
at risk:

  1. BUY leg:  SOL  -> token   (a small, configurable probe size)
  2. SELL leg: token -> SOL    (selling back exactly what the BUY leg
                                 would have produced)

A token is rejected as unsellable when:
  * No route exists for the BUY leg (no real entry liquidity), or
  * No route exists for the SELL leg -- the single hardest-to-fake
    honeypot signal there is: it means Jupiter's own router, given the
    exact number of base units a real buyer would be holding, cannot
    find any way to convert them back to SOL right now, against the
    live pool, and
  * A route exists for both legs but the round-trip loses materially
    more than ordinary slippage/fees would explain (SELLABILITY_MAX_
    ROUND_TRIP_LOSS_PCT), consistent with a hidden sell tax or a
    razor-thin trap pool.

Fail-closed by default (SELLABILITY_FAIL_CLOSED): if the simulation
itself cannot be completed (Jupiter unreachable, rate-limited, timed
out), that is an UNKNOWN sellability state, not a passing one -- same
"never fabricate a positive from an absence" convention this codebase
already applies to GoPlus security-data unavailability.
"""

from __future__ import annotations

import logging
import time

from config.settings import (
    SELLABILITY_CHECK_ENABLED,
    SELLABILITY_PROBE_SOL_AMOUNT,
    SELLABILITY_SLIPPAGE_BPS,
    SELLABILITY_MAX_ROUND_TRIP_LOSS_PCT,
    SELLABILITY_MAX_PRICE_IMPACT_PCT,
    SELLABILITY_CACHE_TTL_SECONDS,
    SELLABILITY_FAIL_CLOSED,
)
from domain.trading.real.jupiter_swap import get_quote, WRAPPED_SOL_MINT, SwapError
from providers.cache import get_cache

logger = logging.getLogger("AlphaPulse.SellabilityCheck")

LAMPORTS_PER_SOL = 1_000_000_000


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


async def verify_sellability(contract: str, *, chain: str = "solana") -> dict:
    """
    Runs the live BUY->SELL Jupiter quote simulation for `contract`.

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
    """
    if not SELLABILITY_CHECK_ENABLED:
        return _neutral_result(checked=False, reject=False,
                                warnings=["sellability_check_disabled"])

    # Jupiter only routes Solana. Other chains (e.g. Robinhood Chain)
    # get a non-blocking neutral result here, exactly the same
    # "Solana-only enrichments are simply omitted for non-Solana
    # candidates" convention already documented in
    # domain.signals.candidate_validation.
    if chain != "solana":
        return _neutral_result(checked=False, reject=False,
                                warnings=["sellability_check_unsupported_for_chain"])

    if not contract:
        return _neutral_result(checked=False, reject=SELLABILITY_FAIL_CLOSED,
                                reasons=["sellability_unverified (missing contract)"] if SELLABILITY_FAIL_CLOSED else [])

    probe_lamports = max(int(SELLABILITY_PROBE_SOL_AMOUNT * LAMPORTS_PER_SOL), 1)
    cache_key = f"sellability:{chain}:{contract}:{probe_lamports}"

    cache = await get_cache()
    try:
        cached = await cache.get(cache_key)
    except Exception as e:  # cache backend hiccup must never block the pipeline
        logger.warning(f"Sellability cache read failed for {contract[:8]}: {e}")
        cached = None
    if cached is not None:
        return cached

    started = time.monotonic()
    try:
        buy_quote = await get_quote(WRAPPED_SOL_MINT, contract, probe_lamports,
                                     slippage_bps=SELLABILITY_SLIPPAGE_BPS)
        out_amount_raw = buy_quote.get("outAmount") if buy_quote else None
        out_amount = int(out_amount_raw) if out_amount_raw is not None else 0

        if out_amount <= 0:
            result = _neutral_result(
                checked=True, reject=True,
                reasons=["No buy route available (no real entry liquidity)"],
                details={"probe_sol": SELLABILITY_PROBE_SOL_AMOUNT},
            )
            await _safe_cache_set(cache, cache_key, result)
            return result

        sell_quote = await get_quote(contract, WRAPPED_SOL_MINT, out_amount,
                                      slippage_bps=SELLABILITY_SLIPPAGE_BPS)
        sell_lamports_raw = sell_quote.get("outAmount") if sell_quote else None
        sell_lamports = int(sell_lamports_raw) if sell_lamports_raw is not None else 0

        if sell_lamports <= 0:
            # The single strongest, hardest-to-fake honeypot signal:
            # can buy, cannot sell back at all.
            result = _neutral_result(
                checked=True, reject=True,
                reasons=["No sell route available -- token cannot be sold back to SOL (honeypot)"],
                details={
                    "probe_sol": SELLABILITY_PROBE_SOL_AMOUNT,
                    "buy_out_amount": out_amount,
                },
            )
            await _safe_cache_set(cache, cache_key, result)
            return result

        round_trip_loss_pct = ((probe_lamports - sell_lamports) / probe_lamports) * 100.0
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

        result = _neutral_result(
            checked=True,
            reject=bool(reasons),
            reasons=reasons,
            warnings=warnings,
            details={
                "probe_sol": SELLABILITY_PROBE_SOL_AMOUNT,
                "buy_out_amount": out_amount,
                "sell_out_lamports": sell_lamports,
                "round_trip_loss_pct": round(round_trip_loss_pct, 2),
                "buy_price_impact_pct": buy_impact,
                "sell_price_impact_pct": sell_impact,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            },
        )
        await _safe_cache_set(cache, cache_key, result)
        return result

    except SwapError as e:
        logger.warning(f"Sellability simulation failed for {contract[:8]} (Jupiter): {e}")
    except Exception as e:
        logger.error(f"Sellability simulation error for {contract[:8]}: {e}")

    # Unreachable/unverifiable this cycle -- fail closed per Production
    # Validation Policy (same convention as GoPlus security-data
    # unavailability): an unknown sellability state is never treated as
    # a passing one. Never cached -- a transient outage must not lock a
    # token out for the full cache TTL once Jupiter recovers.
    return _neutral_result(
        checked=False,
        reject=SELLABILITY_FAIL_CLOSED,
        reasons=["Sellability unverified (Jupiter simulation unavailable)"] if SELLABILITY_FAIL_CLOSED else [],
        warnings=[] if SELLABILITY_FAIL_CLOSED else ["Sellability unverified (Jupiter simulation unavailable)"],
    )


async def _safe_cache_set(cache, key: str, value: dict) -> None:
    try:
        await cache.set(key, value, SELLABILITY_CACHE_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"Sellability cache write failed for key={key}: {e}")
