"""Adaptive market-quality filter policy for AlphaPulse.

This is deliberately a small runtime policy adapter. It replaces the overly
coarse MC/liquidity/volume gates with context-aware checks while preserving
hard safety floors. It does not change security, holder, quota, confidence,
or notification logic.

Policy:
- absolute liquidity floor remains $5k;
- liquidity must also be >= 3% of market cap;
- the normal MC ceiling remains $1.5M, but can extend to $3M only when
  recent flow provides corroboration;
- 1h volume below $1k is rejected unless it has sufficient volume/MC,
  trade count, and buy pressure to demonstrate meaningful activity.

Phase 3.1: bundle similarity is evidence rather than automatic Sybil proof.
The source scoring module owns the severe bundle/concentration decision, so
this adapter does not patch hard_reject_reasons.

The adapter is installed once by sitecustomize after the scoring module is
loaded, because the production pipeline imports these functions directly.

MC/liquidity integrity fix: this adapter previously only enforced a LOWER
floor on liquidity relative to market cap (liquidity >= 3% of MC) and had
NO upper bound at all -- so a candidate whose reported liquidity was
larger than (or comparable to) its market cap could clear this gate with
nothing to catch it, producing alerts like "MC $41.2K / Liquidity $67.7K".
The original, non-adaptive gate in scoring.py always enforced that
invariant (MC must exceed liquidity by at least MC_LP_MIN_RATIO), but this
adapter replaces that gate at runtime and did not carry the invariant
over. It now reuses scoring.MC_LP_MIN_RATIO and scoring.effective_market_cap
so there remains exactly one definition of "healthy MC/liquidity" in the
codebase, and this adapter's context-aware floors are additive on top of
it, not a replacement for it.
"""

from __future__ import annotations

from domain.signals.scoring import effective_market_cap, MC_LP_MIN_RATIO


ABSOLUTE_MIN_LIQUIDITY_USD = 5_000.0
MIN_MARKET_CAP_USD = 8_000.0
NORMAL_MAX_MARKET_CAP_USD = 1_500_000.0
EXTENDED_MAX_MARKET_CAP_USD = 3_000_000.0
MIN_LIQUIDITY_TO_MC = 0.03
LOW_VOLUME_FLOOR_1H = 1_000.0
LOW_VOLUME_TO_MC = 0.02
LOW_VOLUME_MIN_TX = 8
LOW_VOLUME_MIN_BUY_RATIO = 0.55
EXTENSION_MIN_VOLUME_TO_MC = 0.08
EXTENSION_MIN_BUY_RATIO = 0.58
EXTENSION_MIN_TX = 10
EXTENSION_MIN_1H_PRICE_PCT = 5.0


def _f(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return default


def _flow_features(data: dict) -> tuple[float, float, int, float]:
    mc = effective_market_cap(data)
    vol = _f(data.get("volume_1h"))
    buys = _f(data.get("txns_1h_buys"))
    sells = _f(data.get("txns_1h_sells"))
    tx = int(buys + sells)
    buy_ratio = buys / tx if tx > 0 else 0.0
    vol_mc = vol / mc if mc > 0 else 0.0
    return vol, vol_mc, tx, buy_ratio


def adaptive_mc_liquidity_gate(data: dict) -> tuple[bool, str | None]:
    """Return the adaptive market-quality decision without network I/O."""
    liq = _f(data.get("liquidity"))
    mc = effective_market_cap(data)

    if liq < ABSOLUTE_MIN_LIQUIDITY_USD:
        return False, f"liquidity ${liq:,.0f} below absolute floor ${ABSOLUTE_MIN_LIQUIDITY_USD:,.0f}"
    if mc < MIN_MARKET_CAP_USD:
        return False, f"market cap ${mc:,.0f} below minimum ${MIN_MARKET_CAP_USD:,.0f}"

    # Non-negotiable MC/liquidity integrity invariant (restored). The
    # adaptive floors below are context-aware refinements of the LOWER
    # liquidity-depth requirement; they were never meant to remove this
    # UPPER-side sanity check that market cap must genuinely exceed
    # liquidity by a healthy margin. Without this, a token whose
    # liquidity exceeds or matches its market cap -- a real, observed
    # production incident -- had nothing to reject it.
    if mc <= liq:
        return False, f"market cap (${mc:,.0f}) does not exceed liquidity (${liq:,.0f})"
    if (mc / liq) < MC_LP_MIN_RATIO:
        return False, f"MC/liquidity ratio {mc / liq:.2f}x below required {MC_LP_MIN_RATIO:.1f}x"

    vol, vol_mc, tx, buy_ratio = _flow_features(data)
    p1h = _f(data.get("price_change_1h"))

    if mc > NORMAL_MAX_MARKET_CAP_USD:
        extended = (
            mc <= EXTENDED_MAX_MARKET_CAP_USD
            and vol_mc >= EXTENSION_MIN_VOLUME_TO_MC
            and buy_ratio >= EXTENSION_MIN_BUY_RATIO
            and tx >= EXTENSION_MIN_TX
            and p1h >= EXTENSION_MIN_1H_PRICE_PCT
        )
        if not extended:
            return False, (
                f"market cap ${mc:,.0f} above normal ceiling ${NORMAL_MAX_MARKET_CAP_USD:,.0f} "
                "without corroborating momentum"
            )

    required_liq = max(ABSOLUTE_MIN_LIQUIDITY_USD, mc * MIN_LIQUIDITY_TO_MC)
    if liq < required_liq:
        return False, (
            f"liquidity ${liq:,.0f} below adaptive depth floor ${required_liq:,.0f} "
            f"({MIN_LIQUIDITY_TO_MC:.1%} of MC)"
        )

    if vol < LOW_VOLUME_FLOOR_1H:
        meaningful_low_volume = (
            vol_mc >= LOW_VOLUME_TO_MC
            and tx >= LOW_VOLUME_MIN_TX
            and buy_ratio >= LOW_VOLUME_MIN_BUY_RATIO
        )
        if not meaningful_low_volume:
            return False, (
                f"low 1h volume ${vol:,.0f}: insufficient flow confirmation "
                f"(vol/MC={vol_mc:.2%}, tx={tx}, buy_ratio={buy_ratio:.0%})"
            )

    return True, None


def install(scoring_module) -> None:
    """Patch only the market-quality hard gate, exactly once."""
    if getattr(scoring_module, "_adaptive_filter_policy_installed", False):
        return

    scoring_module.passes_mc_liquidity_gate = adaptive_mc_liquidity_gate
    scoring_module._adaptive_filter_policy_installed = True


__all__ = [
    "adaptive_mc_liquidity_gate",
    "install",
    "ABSOLUTE_MIN_LIQUIDITY_USD",
    "MIN_MARKET_CAP_USD",
    "NORMAL_MAX_MARKET_CAP_USD",
    "EXTENDED_MAX_MARKET_CAP_USD",
    "MIN_LIQUIDITY_TO_MC",
    "LOW_VOLUME_FLOOR_1H",
    "LOW_VOLUME_TO_MC",
    "LOW_VOLUME_MIN_TX",
    "LOW_VOLUME_MIN_BUY_RATIO",
    "EXTENSION_MIN_VOLUME_TO_MC",
    "EXTENSION_MIN_BUY_RATIO",
    "EXTENSION_MIN_TX",
    "EXTENSION_MIN_1H_PRICE_PCT",
]
