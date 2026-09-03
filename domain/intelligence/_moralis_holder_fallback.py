"""Indexed holder fallback for AlphaPulse signal analysis — Moralis.

NEW (AlphaPulse Provider Integration Task, 2026-08-19). Moralis is used only
when the primary Helius holder snapshot AND every indexed fallback ahead of
it in the chain (Solana Tracker, Birdeye — see workers/holder_runtime_bootstrap.py)
have returned an unavailable or empty result. It is installed as an
*additional* link in that existing chain, not a replacement for any of it:
see install() below, which follows the exact wrap-and-delegate pattern used
by _solana_tracker_holder_fallback.py and _birdeye_holder_fallback.py.

Endpoint: Moralis Solana Gateway "Get Top Holders"
  GET https://solana-gateway.moralis.io/token/mainnet/{address}/top-holders
  header: X-API-Key
Docs: https://docs.moralis.com/web3-data-api/solana/reference

This module intentionally does not go through providers/rpc/multi_rpc_manager.py
(it isn't a Solana JSON-RPC call, it's a plain REST/indexer read, same as the
Solana Tracker and Birdeye fallbacks it sits alongside) and does not touch
scoring, risk, or qualification logic in domain/signals — it only supplies
holder-account rows in the same {owner, amount} shape those already consume.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger("AlphaPulse.Holders")

ENDPOINT = "https://solana-gateway.moralis.io/token/mainnet/{mint}/top-holders"
DEFAULT_LIMIT = 100


def _error_detail(body: Any) -> str | None:
    """Best-effort extraction of a provider error message from a non-200
    response body — mirrors the identical helper in
    _solana_tracker_holder_fallback.py / _birdeye_holder_fallback.py so a
    real quota/plan restriction can be distinguished from a request-shape
    bug directly from logs."""
    if not isinstance(body, dict):
        return None
    for key in ("message", "error", "msg", "detail"):
        val = body.get(key)
        if val:
            return str(val)[:200]
    return None


def _extract_accounts(body: Any) -> list[dict[str, str]]:
    """Normalize a Moralis top-holders response to the {owner, amount}
    shape domain.intelligence.holders already consumes.

    Moralis' documented response is a list (bare array) or a dict with a
    "result"/"holders" list depending on API version; each row carries an
    owner address under "ownerAddress"/"owner_address"/"owner" and a
    balance under "balance"/"amount". Unknown/empty shapes return [] so the
    caller can tell "provider succeeded but no holders" from a hard failure
    (via the HTTP status check in fetch_token_holders, not this function).
    """
    rows: Any = body
    if isinstance(body, dict):
        rows = body.get("result") if body.get("result") is not None else body.get("holders")
    if not isinstance(rows, list):
        return []

    accounts: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        owner = row.get("ownerAddress") or row.get("owner_address") or row.get("owner")
        amount = row.get("balance") or row.get("amount") or row.get("balanceFormatted")
        if not owner or amount is None:
            continue
        try:
            if float(amount) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        accounts.append({"owner": str(owner), "amount": str(amount)})

    return accounts


async def _get(
    mint: str,
    api_key: str,
    timeout_seconds: float = 10.0,
) -> tuple[int, Any | None]:
    headers = {"X-API-Key": api_key, "accept": "application/json"}
    params = {"limit": str(DEFAULT_LIMIT)}
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                ENDPOINT.format(mint=mint),
                params=params,
                headers=headers,
            ) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    body = None
                return status, body
    except Exception as exc:
        logger.warning(
            "[HolderDiag] Moralis holder request failed safely: %s",
            type(exc).__name__,
        )
        return 0, None


async def fetch_token_holders(
    mint: str,
    timeout_seconds: float = 10.0,
) -> list[dict[str, str]] | None:
    """Fetch a bounded, provider-ranked holder snapshot from Moralis.

    Returns None when the provider is not configured, unavailable, or
    returns an unusable response. An empty successful response is returned
    as [] so the caller can distinguish "provider succeeded but no holders"
    from a provider failure — same convention as every other holder
    fallback in this package.
    """
    api_key = os.getenv("MORALIS_API_KEY")
    if not api_key:
        return None

    status, body = await _get(mint=mint, api_key=api_key, timeout_seconds=timeout_seconds)

    if status != 200:
        logger.warning(
            "[HolderDiag] Moralis holder fallback unavailable for %s (http=%s) detail=%s",
            mint[:8],
            status,
            _error_detail(body),
        )
        return None

    accounts = _extract_accounts(body)
    logger.info(
        "[HolderDiag] Moralis holder fallback mint=%s returned=%d",
        mint[:8],
        len(accounts),
    )

    accounts.sort(key=lambda row: (-float(row.get("amount") or 0), row.get("owner", "")))
    return accounts


def provider_configured() -> bool:
    return bool(os.getenv("MORALIS_API_KEY"))


def provider_name() -> str:
    return "moralis"


def install() -> None:
    """Install Moralis as an indexed fallback, further down the chain than
    Solana Tracker and Birdeye (see workers/holder_runtime_bootstrap.py for
    the authoritative install order)."""
    from domain.intelligence import holders

    if getattr(holders._fetch_token_accounts, "_alphapulse_moralis_fallback", False):
        return

    original_fetch = holders._fetch_token_accounts

    async def _fetch_with_moralis(contract_address: str, priority=holders.PRIORITY_LOW):
        result = await original_fetch(contract_address, priority=priority)

        # Preserve a real upstream snapshot, including small positive holder sets.
        if result is not None and result.accounts:
            return result

        if not provider_configured():
            return result

        logger.info(
            "[HolderDiag] %s: holder path still empty after prior fallbacks; "
            "trying Moralis indexed fallback",
            contract_address[:8],
        )

        try:
            moralis_accounts = await fetch_token_holders(contract_address)
        except Exception as exc:
            logger.warning(
                "[HolderDiag] %s: Moralis fallback failed safely: %s",
                contract_address[:8],
                type(exc).__name__,
            )
            return result

        if moralis_accounts:
            truncated = len(moralis_accounts) > holders.MAX_HOLDER_ACCOUNTS
            if truncated:
                moralis_accounts = moralis_accounts[: holders.MAX_HOLDER_ACCOUNTS]

            logger.info(
                "[HolderDiag] %s: Moralis fallback returned %d positive holder "
                "accounts%s; using them for HolderAnalysis",
                contract_address[:8],
                len(moralis_accounts),
                " (bounded to largest balances)" if truncated else "",
            )
            return holders._HolderAccountsResult(
                accounts=moralis_accounts,
                truncated=truncated,
                raw_account_count=len(moralis_accounts),
            )

        # Never turn an unavailable/empty indexed response into invented
        # concentration data. Preserve the original result semantics.
        return result

    _fetch_with_moralis._alphapulse_moralis_fallback = True
    holders._fetch_token_accounts = _fetch_with_moralis
    logger.info("[HolderDiag] Moralis indexed holder fallback installed")
