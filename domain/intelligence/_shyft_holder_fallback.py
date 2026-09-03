"""Indexed holder fallback for AlphaPulse signal analysis — Shyft.

NEW (AlphaPulse Provider Integration Task, 2026-08-19). Installed as the
last link in the indexed holder-fallback chain (Solana Tracker -> Birdeye
-> Moralis -> Shyft; see workers/holder_runtime_bootstrap.py for the
authoritative install order). Follows the exact wrap-and-delegate pattern
used by _solana_tracker_holder_fallback.py / _birdeye_holder_fallback.py /
_moralis_holder_fallback.py — additive only, nothing upstream is modified.

Endpoint: Shyft "Get Token Owners" (paginated, sorted by balance desc)
  GET https://api.shyft.to/sol/v1/token/get_owners
      ?network=mainnet-beta&token_address={mint}&page=N&size=M
  header: x-api-key
Docs: https://docs.shyft.to (Fungible Tokens -> Get Owners)

This is a plain REST/indexer read, not a Solana JSON-RPC call, so — same as
its siblings in this package — it deliberately does not go through
providers/rpc/multi_rpc_manager.py and does not touch scoring, risk, or
qualification logic; it only supplies holder-account rows in the same
{owner, amount} shape domain.intelligence.holders already consumes.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger("AlphaPulse.Holders")

ENDPOINT = "https://api.shyft.to/sol/v1/token/get_owners"
PAGE_SIZE = 100
MAX_PAGES = 2


def _error_detail(body: Any) -> str | None:
    """Mirrors the identical helper in the sibling holder-fallback modules —
    surfaces the provider's own error message so a quota/plan restriction
    can be told apart from a request-shape bug from logs alone."""
    if not isinstance(body, dict):
        return None
    for key in ("message", "error", "msg", "detail"):
        val = body.get(key)
        if val:
            return str(val)[:200]
    return None


def _extract_accounts(body: Any) -> tuple[list[dict[str, str]], bool]:
    """Normalize a Shyft get_owners response to {owner, amount}.

    Shyft wraps results as {"success": bool, "result": [...]}` per its
    standard REST convention. Each row carries the owner under
    "owner"/"address" and a balance under "amount"/"balance". Returns
    (accounts, has_more) — has_more is a best-effort guess from whether a
    full page was returned, since Shyft's paginated token endpoint does not
    always echo a total count.
    """
    if not isinstance(body, dict) or body.get("success") is False:
        return [], False

    rows = body.get("result")
    if isinstance(rows, dict):
        rows = rows.get("owners") or rows.get("data")
    if not isinstance(rows, list):
        return [], False

    accounts: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        owner = row.get("owner") or row.get("address") or row.get("wallet")
        amount = row.get("amount") or row.get("balance")
        if not owner or amount is None:
            continue
        try:
            if float(amount) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        accounts.append({"owner": str(owner), "amount": str(amount)})

    return accounts, len(rows) >= PAGE_SIZE


async def _get_page(
    mint: str,
    api_key: str,
    page: int,
    timeout_seconds: float = 10.0,
) -> tuple[int, Any | None]:
    headers = {"x-api-key": api_key}
    params = {
        "network": "mainnet-beta",
        "token_address": mint,
        "page": str(page),
        "size": str(PAGE_SIZE),
    }
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(ENDPOINT, params=params, headers=headers) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    body = None
                return status, body
    except Exception as exc:
        logger.warning(
            "[HolderDiag] Shyft holder request failed safely: %s",
            type(exc).__name__,
        )
        return 0, None


async def fetch_token_holders(
    mint: str,
    timeout_seconds: float = 10.0,
) -> list[dict[str, str]] | None:
    """Fetch a bounded, balance-sorted holder snapshot from Shyft.

    Returns None when the provider is not configured, unavailable, or
    returns an unusable response. An empty successful response is returned
    as [] so the caller can distinguish "provider succeeded but no holders"
    from a provider failure — same convention as every other holder
    fallback in this package.
    """
    api_key = os.getenv("SHYFT_API_KEY")
    if not api_key:
        return None

    all_accounts: list[dict[str, str]] = []
    for page in range(1, MAX_PAGES + 1):
        status, body = await _get_page(mint=mint, api_key=api_key, page=page, timeout_seconds=timeout_seconds)

        if status != 200:
            if page == 1:
                logger.warning(
                    "[HolderDiag] Shyft holder fallback unavailable for %s (http=%s) detail=%s",
                    mint[:8],
                    status,
                    _error_detail(body),
                )
                return None
            break

        accounts, has_more = _extract_accounts(body)
        if page == 1 and body is not None and not isinstance(body, dict):
            logger.warning(
                "[HolderDiag] Shyft returned an unusable holder payload for %s",
                mint[:8],
            )
            return None

        all_accounts.extend(accounts)
        logger.info(
            "[HolderDiag] Shyft holder fallback page=%d mint=%s returned=%d has_more=%s",
            page,
            mint[:8],
            len(accounts),
            has_more,
        )
        if not has_more:
            break

    all_accounts.sort(key=lambda row: (-float(row.get("amount") or 0), row.get("owner", "")))
    return all_accounts


def provider_configured() -> bool:
    return bool(os.getenv("SHYFT_API_KEY"))


def provider_name() -> str:
    return "shyft"


def install() -> None:
    """Install Shyft as the last indexed fallback in the chain (see
    workers/holder_runtime_bootstrap.py for the authoritative install
    order)."""
    from domain.intelligence import holders

    if getattr(holders._fetch_token_accounts, "_alphapulse_shyft_fallback", False):
        return

    original_fetch = holders._fetch_token_accounts

    async def _fetch_with_shyft(contract_address: str, priority=holders.PRIORITY_LOW):
        result = await original_fetch(contract_address, priority=priority)

        # Preserve a real upstream snapshot, including small positive holder sets.
        if result is not None and result.accounts:
            return result

        if not provider_configured():
            return result

        logger.info(
            "[HolderDiag] %s: holder path still empty after prior fallbacks; "
            "trying Shyft indexed fallback",
            contract_address[:8],
        )

        try:
            shyft_accounts = await fetch_token_holders(contract_address)
        except Exception as exc:
            logger.warning(
                "[HolderDiag] %s: Shyft fallback failed safely: %s",
                contract_address[:8],
                type(exc).__name__,
            )
            return result

        if shyft_accounts:
            truncated = len(shyft_accounts) > holders.MAX_HOLDER_ACCOUNTS
            if truncated:
                shyft_accounts = shyft_accounts[: holders.MAX_HOLDER_ACCOUNTS]

            logger.info(
                "[HolderDiag] %s: Shyft fallback returned %d positive holder "
                "accounts%s; using them for HolderAnalysis",
                contract_address[:8],
                len(shyft_accounts),
                " (bounded to largest balances)" if truncated else "",
            )
            return holders._HolderAccountsResult(
                accounts=shyft_accounts,
                truncated=truncated,
                raw_account_count=len(shyft_accounts),
            )

        # Never turn an unavailable/empty indexed response into invented
        # concentration data. Preserve the original result semantics.
        return result

    _fetch_with_shyft._alphapulse_shyft_fallback = True
    holders._fetch_token_accounts = _fetch_with_shyft
    logger.info("[HolderDiag] Shyft indexed holder fallback installed")
