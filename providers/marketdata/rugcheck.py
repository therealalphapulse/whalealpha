"""
providers/marketdata/rugcheck.py

NEW (AlphaPulse Provider Integration Task, 2026-08-19). RugCheck (served via
the already-configured FluxRPC-issued key, RUGCHECK_API_KEY) is an
*additive* fallback for GoPlus token-security data only -- see
check_token_security() in providers/marketdata/goplus.py, which is the
single place this module is called from.

This is purely additive: it is only ever reached after GoPlus's own two
endpoint attempts both return no usable data, and only takes effect if
RUGCHECK_API_KEY is set. Nothing about GoPlus's request shape, endpoints,
retry behavior, or normalized field mapping changes. Every existing
caller of check_token_security() (security/auto_scan/score/premium
commands, domain/signals/pump_radar.py, domain/signals/scoring.py,
domain/intelligence/premium_signal_engine.py) is unaffected: it keeps
calling check_token_security() and keeps receiving the exact same
normalized dict shape GoPlus has always returned, or None.

Endpoint: RugCheck public API, token report
  GET https://api.rugcheck.xyz/v1/tokens/{mint}/report
  header: Authorization: Bearer <RUGCHECK_API_KEY>
Docs: https://api.rugcheck.xyz/swagger/index.html

Uses the same shared, cached, retried HTTP helper GoPlus already uses
(providers/marketdata/_resilience.py) rather than rolling a new transport.
"""

from __future__ import annotations

import logging

from config.settings import RUGCHECK_API, RUGCHECK_API_KEY
from providers.marketdata._resilience import get_json

logger = logging.getLogger("AlphaPulse.RugCheck")


def _mint_authority_status(token: dict) -> str:
    # RugCheck reports mint authority as either an address string (present)
    # or null/absent (revoked/never set) -- normalize to GoPlus's "1"/"0"
    # convention so format_security_report()'s existing flag rendering
    # needs no changes.
    return "1" if token.get("mintAuthority") else "0"


def _freeze_authority_status(token: dict) -> str:
    return "1" if token.get("freezeAuthority") else "0"


def _holder_stats(top_holders: list) -> tuple[float | None, float | None]:
    if not isinstance(top_holders, list) or not top_holders:
        return None, None

    percentages = []
    for holder in top_holders:
        if not isinstance(holder, dict):
            continue
        pct = holder.get("pct")
        try:
            if pct is not None:
                percentages.append(round(float(pct), 2))
        except (TypeError, ValueError):
            continue

    if not percentages:
        return None, None

    percentages.sort(reverse=True)
    top_holder = percentages[0]
    top_10 = round(sum(percentages[:10]), 2)
    return top_holder, top_10


def _normalize_rugcheck_security(payload: dict) -> dict | None:
    """Maps a RugCheck /report response onto the exact same normalized
    dict shape providers/marketdata/goplus.py::_normalize_token_security
    already produces, so format_security_report() and every downstream
    consumer of check_token_security()'s return value needs no changes."""
    if not isinstance(payload, dict):
        return None

    token = payload.get("token")
    if not isinstance(token, dict):
        return None

    top_holders = payload.get("topHolders")
    top_holder_percent, top_10_holder_percent = _holder_stats(top_holders)

    holder_count = payload.get("totalHolders") or ""
    creator = payload.get("creator") or ""

    return {
        # Solana-specific risks -- same field set GoPlus normalizes to.
        # RugCheck's /report does not report these remaining fields, so
        # they're marked "unknown" -- format_security_report() already
        # renders "unknown" as a neutral ⚪ line, the same treatment a
        # partial GoPlus response gets today.
        "mintable": _mint_authority_status(token),
        "freezable": _freeze_authority_status(token),
        "metadata_mutable": "unknown",
        "balance_mutable_authority": "unknown",
        "closable": "unknown",
        "default_account_state_upgradable": "unknown",
        "transfer_fee": "unknown",
        "transfer_fee_upgradable": "unknown",
        "non_transferable": "unknown",
        "trusted_token": "1" if payload.get("verification") else "0",

        # Holder / creator data
        "holder_count": str(holder_count) if holder_count else "",
        "top_holder_percent": top_holder_percent,
        "top_10_holder_percent": top_10_holder_percent,
        "creator_percent": None,
        "creator_balance": "",
        "creator_address": str(creator),
        "owner_address": "",
        "total_supply": str(token.get("supply", "N/A")),

        # Keep old fields for compatibility with existing score/security code
        "is_honeypot": "1" if payload.get("rugged") else "0",
        "is_blacklisted": "0",
        "is_whitelisted": "0",
        "is_proxy": "0",
        "owner_change_balance": "0",
        "hidden_owner": "0",
        "selfdestruct": "0",
        "external_call": "0",
        "cannot_sell_all": "0",
        "cannot_buy": "0",
        "trading_cooldown": "0",
    }


async def check_token_security(contract_address: str) -> dict | None:
    """RugCheck fallback for GoPlus token security data. Returns None if
    RugCheck is not configured, unreachable, or returns no usable token
    report -- callers already treat None as "no security data available"
    identically to a GoPlus miss, so no caller-side handling changes."""
    if not RUGCHECK_API_KEY:
        return None

    url = f"{RUGCHECK_API}/tokens/{contract_address}/report"
    headers = {"Authorization": f"Bearer {RUGCHECK_API_KEY}"}

    try:
        payload = await get_json(url, headers=headers, cache_ttl_seconds=30, timeout_seconds=10)
    except Exception as e:
        logger.error(f"RugCheck error for {contract_address}: {e}")
        return None

    if payload is None:
        logger.warning(f"RugCheck fetch failed for {contract_address}")
        return None

    normalized = _normalize_rugcheck_security(payload)
    if normalized is None:
        logger.info(f"No usable RugCheck security data found for {contract_address}")
        return None

    return normalized
