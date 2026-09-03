"""Codex indexed holder fallback for AlphaPulse.

Additive only: this adapter runs after the existing holder providers and
normalizes Codex's GraphQL holder records into the existing {owner, amount}
contract consumed by HolderAnalysis. It never changes scoring, qualification,
or trading behavior.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger("AlphaPulse.Holders")

ENDPOINT = "https://graph.codex.io/graphql"
SOLANA_NETWORK_ID = 1399811149
DEFAULT_LIMIT = 200

_QUERY = """
query GetTokenHolders($input: HoldersInput!) {
  holders(input: $input) {
    items {
      address
      balance
      shiftedBalance
    }
    count
    status
    top10HoldersPercent
    cursor
  }
}
"""


def _extract_accounts(body: Any) -> list[dict[str, str]]:
    """Normalize Codex holder records to the existing {owner, amount} shape."""
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    holders = data.get("holders") if isinstance(data, dict) else None
    if not isinstance(holders, dict):
        return []

    rows = holders.get("items")
    if not isinstance(rows, list):
        return []

    accounts: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        owner = row.get("address")
        amount = row.get("balance")
        if amount is None:
            amount = row.get("shiftedBalance")
        if not owner or amount is None:
            continue
        try:
            if float(amount) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        accounts.append({"owner": str(owner), "amount": str(amount)})

    accounts.sort(key=lambda row: (-float(row.get("amount") or 0), row.get("owner", "")))
    return accounts


def _graphql_error(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict) and first.get("message"):
            return str(first["message"])[:200]
    return None


async def fetch_token_holders(
    mint: str,
    timeout_seconds: float = 10.0,
) -> list[dict[str, str]] | None:
    """Return Codex holders, or None when Codex is unavailable/unusable."""
    api_key = os.getenv("CODEX_API_KEY", "").strip()
    if not api_key:
        return None

    payload = {
        "query": _QUERY,
        "variables": {
            "input": {
                "tokenId": f"{mint}:{SOLANA_NETWORK_ID}",
                "limit": DEFAULT_LIMIT,
                "filterContracts": True,
            }
        },
    }
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(ENDPOINT, json=payload, headers=headers) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    body = None
    except Exception as exc:
        logger.warning(
            "[HolderProvider] provider=codex status=request_failed mint=%s error=%s",
            mint[:8], type(exc).__name__,
        )
        return None

    if status != 200:
        logger.warning(
            "[HolderProvider] provider=codex status=unavailable http=%s mint=%s detail=%s",
            status, mint[:8], _graphql_error(body),
        )
        return None

    graphql_error = _graphql_error(body)
    accounts = _extract_accounts(body)
    if graphql_error or not accounts:
        logger.warning(
            "[HolderProvider] provider=codex status=%s mint=%s detail=%s",
            "graphql_error" if graphql_error else "empty_or_malformed",
            mint[:8],
            graphql_error,
        )
        return None

    holders = body.get("data", {}).get("holders", {}) if isinstance(body, dict) else {}
    logger.info(
        "[HolderProvider] provider=codex status=success mint=%s returned=%d total_holders=%s top10_pct=%s",
        mint[:8],
        len(accounts),
        holders.get("count"),
        holders.get("top10HoldersPercent"),
    )
    return accounts


def provider_configured() -> bool:
    return bool(os.getenv("CODEX_API_KEY", "").strip())


def provider_name() -> str:
    return "codex"


def install() -> None:
    """Append Codex to the existing indexed holder fallback chain."""
    from domain.intelligence import holders

    if getattr(holders._fetch_token_accounts, "_alphapulse_codex_fallback", False):
        return

    original_fetch = holders._fetch_token_accounts

    async def _fetch_with_codex(contract_address: str, priority=holders.PRIORITY_LOW):
        result = await original_fetch(contract_address, priority=priority)

        if result is not None and result.accounts:
            return result
        if not provider_configured():
            return result

        logger.info(
            "[HolderProvider] provider=codex status=attempting mint=%s "
            "(earlier providers returned no usable accounts)",
            contract_address[:8],
        )

        try:
            codex_accounts = await fetch_token_holders(contract_address)
        except Exception as exc:
            logger.warning(
                "[HolderProvider] provider=codex status=failed_safely mint=%s error=%s",
                contract_address[:8], type(exc).__name__,
            )
            return result

        if not codex_accounts:
            return result

        truncated = len(codex_accounts) > holders.MAX_HOLDER_ACCOUNTS
        if truncated:
            codex_accounts = codex_accounts[: holders.MAX_HOLDER_ACCOUNTS]

        return holders._HolderAccountsResult(
            accounts=codex_accounts,
            truncated=truncated,
            raw_account_count=len(codex_accounts),
        )

    _fetch_with_codex._alphapulse_codex_fallback = True
    holders._fetch_token_accounts = _fetch_with_codex
    logger.info("[HolderProvider] Codex indexed holder fallback installed")
