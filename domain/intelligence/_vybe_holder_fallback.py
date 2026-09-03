"""Vybe indexed holder fallback for AlphaPulse.

Additive only: this module wraps the existing holder pipeline and is tried
only after all previously installed holder providers return no usable
accounts. It preserves the existing {owner, amount} contract consumed by
HolderAnalysis and never changes discovery, scoring, or qualification logic.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger("AlphaPulse.Holders")

ENDPOINT = "https://api.vybenetwork.xyz/v4/tokens/{mint}/top-holders"
# Update (2026-08-22): VYBE_API_KEY is not currently set in production
# (this module has never actually been invoked there -- provider_configured()
# returns False and every call short-circuits before reaching the network),
# so a "403 Insufficient credits" seen previously came from a key/account
# that is no longer wired in here. No code change can create credits on a
# third-party account. What IS fixable in code is cost, for whenever a
# funded key is added: 1000 rows is far more than concentration math
# (top_holder_pct/top10_pct/top25_pct/bundle detection) needs -- matching
# the page sizes already used by the other indexed fallbacks (Birdeye 100,
# Moralis 100, Shyft 100/page) reduces credits spent per lookup without
# changing which wallets are returned (still balance-sorted, top holders
# first).
DEFAULT_LIMIT = 100  # was 1000 -- concentration math only needs top holders


def _error_detail(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("message", "error", "msg", "detail"):
        value = body.get(key)
        if value:
            return str(value)[:200]
    return None


def _extract_accounts(body: Any) -> list[dict[str, str]]:
    """Normalize Vybe holder rows to the existing {owner, amount} shape."""
    if not isinstance(body, dict):
        return []

    rows = body.get("data")
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("holders") or rows.get("items")
    if not isinstance(rows, list):
        rows = body.get("holders") or body.get("items")
    if not isinstance(rows, list):
        return []

    accounts: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        owner = (
            row.get("ownerAddress")
            or row.get("owner_address")
            or row.get("address")
            or row.get("owner")
        )
        amount = row.get("balance")
        if amount is None:
            amount = row.get("amount")
        if amount is None:
            amount = row.get("balanceAmount")
        if not owner or amount is None:
            continue
        try:
            if float(amount) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        accounts.append({"owner": str(owner), "amount": str(amount)})

    return accounts


async def fetch_token_holders(
    mint: str,
    timeout_seconds: float = 10.0,
) -> list[dict[str, str]] | None:
    """Return Vybe holders, or None when Vybe is unavailable/unusable."""
    api_key = os.getenv("VYBE_API_KEY")
    if not api_key:
        return None

    params = {"limit": str(DEFAULT_LIMIT), "page": "0"}
    headers = {"X-API-Key": api_key, "accept": "application/json"}

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                ENDPOINT.format(mint=mint), params=params, headers=headers
            ) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    body = None
    except Exception as exc:
        logger.warning(
            "[HolderProvider] provider=vybe status=request_failed mint=%s error=%s",
            mint[:8], type(exc).__name__,
        )
        return None

    if status != 200:
        logger.warning(
            "[HolderProvider] provider=vybe status=unavailable http=%s mint=%s detail=%s",
            status, mint[:8], _error_detail(body),
        )
        return None

    accounts = _extract_accounts(body)
    if not accounts:
        logger.warning(
            "[HolderProvider] provider=vybe status=empty_or_malformed mint=%s",
            mint[:8],
        )
        return None

    accounts.sort(key=lambda row: (-float(row.get("amount") or 0), row.get("owner", "")))
    logger.info(
        "[HolderProvider] provider=vybe status=success mint=%s returned=%d",
        mint[:8], len(accounts),
    )
    return accounts


def provider_configured() -> bool:
    return bool(os.getenv("VYBE_API_KEY"))


def provider_name() -> str:
    return "vybe"


def install() -> None:
    """Append Vybe to the existing indexed holder fallback chain."""
    from domain.intelligence import holders

    if getattr(holders._fetch_token_accounts, "_alphapulse_vybe_fallback", False):
        return

    original_fetch = holders._fetch_token_accounts

    async def _fetch_with_vybe(contract_address: str, priority=holders.PRIORITY_LOW):
        result = await original_fetch(contract_address, priority=priority)

        if result is not None and result.accounts:
            return result
        if not provider_configured():
            return result

        logger.info(
            "[HolderProvider] provider=vybe status=attempting mint=%s "
            "(earlier providers returned 0 accounts)",
            contract_address[:8],
        )

        try:
            vybe_accounts = await fetch_token_holders(contract_address)
        except Exception as exc:
            logger.warning(
                "[HolderProvider] provider=vybe status=failed_safely mint=%s error=%s",
                contract_address[:8], type(exc).__name__,
            )
            return result

        if not vybe_accounts:
            return result

        truncated = len(vybe_accounts) > holders.MAX_HOLDER_ACCOUNTS
        if truncated:
            vybe_accounts = vybe_accounts[: holders.MAX_HOLDER_ACCOUNTS]

        return holders._HolderAccountsResult(
            accounts=vybe_accounts,
            truncated=truncated,
            raw_account_count=len(vybe_accounts),
        )

    _fetch_with_vybe._alphapulse_vybe_fallback = True
    holders._fetch_token_accounts = _fetch_with_vybe
    logger.info("[HolderProvider] Vybe indexed holder fallback installed")
