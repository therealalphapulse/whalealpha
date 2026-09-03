"""Normalize holder-analysis availability semantics at the signal boundary.

Holder retrieval is evidence, not a prerequisite. This adapter keeps the
existing HolderAnalysis metrics and safety gates intact while ensuring RPC
failure or untrusted parsing never becomes a fake concentration signal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any


PROVIDER_ERROR = "provider_error"
INVALID_HOLDER_RESPONSE = "invalid_holder_response"
UNAVAILABLE_HOLDER_DATA = "unavailable_early_token"
VALID_HOLDER_DATA = "ok"


def _unknown(status: str) -> dict[str, Any]:
    return {
        "total_holders": None,
        "top_holder_pct": None,
        "top10_pct": None,
        "top25_pct": None,
        "dev_holding_pct": None,
        # Unknown means no evidence either way -- 0 would silently read as a
        # confirmed "0% bundled / 0 bundle wallets" result downstream (see
        # scoring.evaluate_sybil_bundle_risk / _score_wallet_behavior, which
        # now treat bundle_pct is None as unresolved rather than clean).
        "bundle_wallet_count": None,
        "bundle_pct": None,
        "top_holder_addresses": [],
        "holder_analysis_status": status,
        "holder_data_truncated": False,
    }


def normalize_holder_analysis(result: dict[str, Any] | None) -> dict[str, Any]:
    """Return a safe, explicit holder state without inventing metrics.

    ``None`` from the legacy holder API means the RPC chain failed. A normal
    ``ok`` analysis is retained. A parser-drop-shaped ``ok`` result is
    relabeled invalid so its missing metrics can never become concentration
    evidence. The existing Pump.fun empty-result state remains an explicit
    unavailable state.
    """
    if result is None:
        return _unknown(PROVIDER_ERROR)

    status = result.get("holder_analysis_status")
    if status == UNAVAILABLE_HOLDER_DATA:
        return result
    if status in {PROVIDER_ERROR, INVALID_HOLDER_RESPONSE}:
        return result

    # A successful analysis must have a real positive holder distribution.
    # The legacy parser can otherwise return `ok` with a count but no
    # concentration metrics when every returned account had zero/invalid
    # token amount data. That is not trustworthy holder evidence.
    total_holders = result.get("total_holders")
    top_holder_pct = result.get("top_holder_pct")
    top10_pct = result.get("top10_pct")
    top25_pct = result.get("top25_pct")
    if (
        status == VALID_HOLDER_DATA
        and total_holders is not None
        and top_holder_pct is None
        and top10_pct is None
        and top25_pct is None
    ):
        return _unknown(INVALID_HOLDER_RESPONSE)

    if status == VALID_HOLDER_DATA:
        return result

    # Be conservative with unexpected states: unknown is not a reason to
    # manufacture a holder score or concentration percentage.
    return _unknown(INVALID_HOLDER_RESPONSE)


def install() -> None:
    """Install the normalizer on the production PumpRadar holder boundary."""
    from domain.signals import pump_radar

    original = pump_radar.get_holder_analysis
    if getattr(original, "_alphapulse_holder_state_normalized", False):
        return

    @wraps(original)
    async def normalized(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original(*args, **kwargs)
        normalized_result = normalize_holder_analysis(result)
        if normalized_result.get("holder_analysis_status") == PROVIDER_ERROR:
            pump_radar.logger.warning(
                "Holder data unavailable (provider error) — continuing with "
                "unknown holder evidence"
            )
        elif normalized_result.get("holder_analysis_status") == INVALID_HOLDER_RESPONSE:
            pump_radar.logger.warning(
                "Holder data invalid/unparseable — continuing with unknown "
                "holder evidence"
            )
        return normalized_result

    normalized._alphapulse_holder_state_normalized = True
    pump_radar.get_holder_analysis = normalized
