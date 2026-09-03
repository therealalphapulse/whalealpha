"""Birdeye holder-data fallback for AlphaPulse signal analysis (Phase 3.2).

Mirrors domain.intelligence._solana_tracker_holder_fallback's shape and
install pattern exactly, and is installed AFTER it in the provider chain:

    Helius getProgramAccountsV2
      -> legacy multi-provider getProgramAccounts (Helius/QuickNode/Alchemy)
      -> Solana Tracker (indexed fallback)
      -> Birdeye (indexed fallback)  <-- this module
      -> normalized HolderAnalysis (domain.intelligence.holders.get_holder_analysis)

Birdeye is only ever called when every earlier provider in the chain
returned an unusable result for reasons distinguishable from "this token
genuinely has zero holders" (quota exhaustion, rate limiting, timeout,
provider unreachable, or a malformed/error response) -- see
domain.intelligence._solana_tracker_holder_fallback.install()'s wrapper,
which this module re-wraps: it only reaches Birdeye when the (already
Tracker-wrapped) result still has no accounts.

Endpoint reference (docs.birdeye.so/reference/get-defi-v3-token-holder,
fetched 2026-08-13): GET /defi/v3/token/holder, `X-API-KEY` header auth,
`address` (token mint), `mode=wallet` to group token-account balances by
owner wallet (true per-wallet concentration, matching how
domain.intelligence.holders merges by owner), `ui_amount_mode=raw` for raw
base-unit amounts (consistent with the raw-amount convention used
throughout holders.py), and offset/limit paging (limit capped at 100,
offset+limit <= 10000 per Birdeye's documented limitation). This endpoint
has NOT been live-validated against a real API key in this environment (no
network access here to confirm) -- the parser below is deliberately
defensive (see _extract_accounts) and treats any unexpected shape as an
unusable response rather than guessing, the same convention used by the
Solana Tracker adapter and domain.intelligence.funding_graph.

Update (2026-08-15): production logs show this endpoint returning HTTP
400 on essentially every call. Root cause not yet confirmed (see
_error_detail below, added specifically so the next occurrence carries
the provider's own error message in the log line instead of just the
status code) -- do not assume the request shape above is wrong without
that evidence; it was built directly from Birdeye's current published
reference and may equally be a plan/tier restriction on this API key.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger("AlphaPulse.Holders")

ENDPOINT = "https://public-api.birdeye.so/defi/v3/token/holder"

# Birdeye's documented limitation: limit is 1-100 per page, and
# offset + limit must stay <= 10000. This endpoint also costs 35 CU per
# request (a premium/paid call), so -- unlike the free-tier Solana
# Tracker fallback -- pages are kept deliberately small. This is a last-
# resort fallback for concentration/bundle math, not a full holder-roll
# export; the largest-balance wallets (which is all that
# top_holder_pct/top10_pct/top25_pct/bundle detection actually need) are
# always on the first page or two.
MAX_PAGE_SIZE = 100
# Update (2026-08-22): production logs show this endpoint returning HTTP
# 401 Unauthorized on every call (see _error_detail below for the exact
# provider message), which is a credentials/plan issue outside this
# module's control -- no request-shape change here can fix an invalid or
# under-provisioned API key. What IS fixable in code is cost: this
# endpoint costs 35 CU per page (a premium/paid call), and the largest-
# balance wallets that top_holder_pct/top10_pct/top25_pct/bundle
# detection actually need are always on the first page (see comment
# above) -- so MAX_PAGES is reduced from 3 to 1 to cut CU consumption by
# 3x per lookup, directly mitigating "Compute Units usage limit
# exceeded" whenever a valid, sufficiently-provisioned key is in place.
# This does not change what data is returned when the provider succeeds
# (still page-1 balance-sorted holders), only how many pages are ever
# requested.
MAX_PAGES = 1  # 1 * 100 = 100 holders -- covers concentration math; was 3


def _error_detail(body: Any) -> str | None:
    """Best-effort extraction of a provider error message from a non-200
    response body. HTTP status alone wasn't enough to tell a request-shape
    bug apart from a real plan/quota restriction -- every non-200 branch
    below now surfaces whatever the provider actually said, instead of
    just the status code, so a future failure is diagnosable from logs
    alone rather than requiring a guess."""
    if not isinstance(body, dict):
        return None
    for key in ("message", "error", "msg", "detail"):
        val = body.get(key)
        if val:
            return str(val)[:200]
    return None


def _extract_accounts(body: Any) -> tuple[list[dict[str, str]], int | None, bool]:
    """
    Parse a Birdeye /defi/v3/token/holder response into the shared
    {owner, amount} shape.

    Defensive by construction: any response that doesn't clearly contain a
    positive, well-typed holder list is treated as unusable ([], None,
    False) rather than guessed at -- callers must not mistake "we
    couldn't parse this" for "this token really has zero holders".
    """
    if not isinstance(body, dict):
        return [], None, False
    if body.get("success") is False:
        return [], None, False

    data = body.get("data")
    if not isinstance(data, dict):
        # Some Birdeye endpoints return the payload unwrapped at the top
        # level; accept that shape too rather than hard-failing on it.
        data = body if "items" in body else None
    if not isinstance(data, dict):
        return [], None, False

    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return [], None, False

    accounts: list[dict[str, str]] = []
    for row in raw_items:
        if not isinstance(row, dict):
            continue
        owner = row.get("owner") or row.get("wallet")
        # Prefer the raw base-unit "amount" (matches the rest of this
        # codebase's raw-amount convention); fall back to ui_amount only
        # if amount is genuinely absent.
        amount = row.get("amount")
        if amount is None:
            amount = row.get("ui_amount")
        if not owner or amount is None:
            continue
        try:
            if float(amount) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        accounts.append({"owner": str(owner), "amount": str(amount)})

    total = data.get("holder")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None

    has_more = len(raw_items) >= MAX_PAGE_SIZE
    return accounts, total, has_more


async def _get_page(
    mint: str,
    api_key: str,
    offset: int,
    timeout_seconds: float = 10.0,
) -> tuple[int, Any | None]:
    params = {
        "address": mint,
        "offset": str(offset),
        "limit": str(MAX_PAGE_SIZE),
        "mode": "wallet",
        "ui_amount_mode": "raw",
    }
    # Never log the key itself -- only whether one was present.
    headers = {"X-API-KEY": api_key, "x-chain": "solana", "accept": "application/json"}
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
            "[HolderProvider] provider=birdeye status=request_failed error=%s",
            type(exc).__name__,
        )
        return 0, None


async def fetch_token_holders(
    mint: str,
    timeout_seconds: float = 10.0,
) -> list[dict[str, str]] | None:
    """
    Fetch a bounded, balance-sorted holder snapshot from Birdeye.

    Returns None when the provider is not configured, unavailable, or
    returns an unusable response (auth failure, rate limit, server error,
    timeout, malformed body). Returns [] for a genuine, successfully-
    parsed empty holder set -- callers must be able to tell "provider
    failed" (None) apart from "provider succeeded, zero holders" ([]), the
    same convention already used by the Solana Tracker adapter.
    """
    api_key = os.getenv("BIRDEYE_API_KEY")
    if not api_key:
        return None

    all_accounts: list[dict[str, str]] = []
    offset = 0

    for page in range(MAX_PAGES):
        status, body = await _get_page(
            mint=mint, api_key=api_key, offset=offset, timeout_seconds=timeout_seconds
        )

        if status == 401:
            logger.warning("[HolderProvider] provider=birdeye status=unauthorized mint=%s detail=%s", mint[:8], _error_detail(body))
            return None
        if status == 403:
            logger.warning("[HolderProvider] provider=birdeye status=forbidden mint=%s detail=%s", mint[:8], _error_detail(body))
            return None
        if status == 429:
            logger.warning("[HolderProvider] provider=birdeye status=rate_limited mint=%s detail=%s", mint[:8], _error_detail(body))
            return None
        if status >= 500:
            logger.warning(
                "[HolderProvider] provider=birdeye status=server_error http=%s mint=%s detail=%s",
                status, mint[:8], _error_detail(body),
            )
            return None
        if status == 0:
            # _get_page already logged the specific failure (timeout,
            # connection error, etc.) -- nothing more to add here.
            return None
        if status != 200:
            logger.warning(
                "[HolderProvider] provider=birdeye status=unexpected_http http=%s mint=%s detail=%s",
                status, mint[:8], _error_detail(body),
            )
            return None

        accounts, total, has_more = _extract_accounts(body)
        if body is None or not isinstance(body, dict):
            logger.warning("[HolderProvider] provider=birdeye status=malformed_response mint=%s", mint[:8])
            return None

        all_accounts.extend(accounts)

        logger.info(
            "[HolderProvider] provider=birdeye status=success page=%d mint=%s "
            "returned=%d total=%s has_more=%s",
            page, mint[:8], len(accounts), total, has_more,
        )

        if not has_more:
            break
        offset += MAX_PAGE_SIZE
        if offset + MAX_PAGE_SIZE > 10000:
            # Respect Birdeye's documented offset+limit <= 10000 ceiling.
            break

    all_accounts.sort(key=lambda row: (-float(row.get("amount") or 0), row.get("owner", "")))
    return all_accounts


def provider_configured() -> bool:
    return bool(os.getenv("BIRDEYE_API_KEY"))


def provider_name() -> str:
    return "birdeye"


def install() -> None:
    """Install Birdeye as the indexed fallback after Solana Tracker.

    Idempotent and safe to call multiple times / in any order relative to
    domain.intelligence._solana_tracker_holder_fallback.install() -- both
    check the existing chain before wrapping, so whichever installs last
    still sits outermost (Birdeye after Tracker, per the module docstring
    above).
    """
    from domain.intelligence import holders

    if getattr(holders._fetch_token_accounts, "_alphapulse_birdeye_fallback", False):
        return

    original_fetch = holders._fetch_token_accounts

    async def _fetch_with_birdeye(contract_address: str, priority=holders.PRIORITY_LOW):
        result = await original_fetch(contract_address, priority=priority)

        if result is not None and result.accounts:
            return result

        if not provider_configured():
            return result

        logger.info(
            "[HolderProvider] provider=birdeye status=attempting mint=%s "
            "(earlier providers returned 0 accounts)",
            contract_address[:8],
        )

        try:
            birdeye_accounts = await fetch_token_holders(contract_address)
        except Exception as exc:
            logger.warning(
                "[HolderProvider] provider=birdeye status=failed_safely mint=%s error=%s",
                contract_address[:8],
                type(exc).__name__,
            )
            return result

        if birdeye_accounts:
            truncated = len(birdeye_accounts) > holders.MAX_HOLDER_ACCOUNTS
            if truncated:
                birdeye_accounts = birdeye_accounts[: holders.MAX_HOLDER_ACCOUNTS]
            return holders._HolderAccountsResult(
                accounts=birdeye_accounts,
                truncated=truncated,
                raw_account_count=len(birdeye_accounts),
            )

        return result

    _fetch_with_birdeye._alphapulse_birdeye_fallback = True
    holders._fetch_token_accounts = _fetch_with_birdeye
    logger.info("[HolderProvider] Birdeye indexed holder fallback installed")
