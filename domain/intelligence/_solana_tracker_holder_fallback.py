"""Free-first indexed holder fallback for AlphaPulse signal analysis.

Solana Tracker is used only when the primary Helius holder snapshot is
unavailable or empty. The adapter normalizes its holder response to the
same {owner, amount} shape consumed by domain.intelligence.holders.

The paginated endpoint is deliberately preferred over the top-100 endpoint:
it returns a real `total` count and up to 5,000 holders/page, while still
keeping the signal path bounded. We do not request identity/PnL enrichment;
the signal engine needs balances/concentration, not expensive enrichment.

Update (2026-08-15): production logs show this provider now failing with
HTTP 403 after working normally the day before (real holder counts were
being returned successfully as of Aug 14). Most likely explanation is
free-tier credit/quota exhaustion rather than a code fault, but
_error_detail below (added alongside the identical helper in
_birdeye_holder_fallback.py) now surfaces the provider's own message so
this can be confirmed from logs instead of guessed at.

Update (2026-08-28, AlphaPulse Provider Resilience task): the Aug 15
incident above exposed two separate gaps, both fixed here:

  1. There was no memory of the 403 across calls -- every single token
     scan re-hit this endpoint and re-logged the same failure for as long
     as the outage lasted. fetch_token_holders() now checks the shared
     circuit breaker in providers.marketdata._provider_circuit_breaker
     before making any request, and stops calling out entirely once the
     failure pattern repeats, resuming automatically once a probe
     succeeds. This never changes fallback ORDER or disables the
     provider permanently -- see workers/holder_runtime_bootstrap.py: a
     circuit-open Solana Tracker still causes install()'s wrapper to fall
     through to Birdeye/Vybe exactly as an unconfigured or hard-failing
     provider always has.
  2. If page 1 of a paginated fetch succeeded (a real, usable batch of
     balances) and page 2 then failed, the old code discarded the
     already-fetched page 1 data and returned None for the whole request
     -- treating a partial success as if nothing had been fetched at all.
     fetch_token_holders() now returns the accumulated partial batch
     instead (see _fetch_paginated_holders below); the caller marks it
     truncated, exactly like any other bounded/truncated holder snapshot
     in this codebase (domain/intelligence/holders.py MAX_HOLDER_ACCOUNTS),
     rather than throwing away real evidence because of what a LATER page
     did.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

from providers.marketdata import _provider_circuit_breaker as _breaker

logger = logging.getLogger("AlphaPulse.Holders")

ENDPOINT = "https://data.solanatracker.io/tokens/{mint}/holders/paginated"
MAX_PAGE_SIZE = 5000
MAX_PAGES = 2

# Separate breaker key from providers.marketdata.solanatracker's
# "solana_tracker" key: this hits a different endpoint (paginated
# holders vs. search / single-token) with an independent failure mode,
# so its health is tracked independently rather than sharing fate with
# (or being masked by) the liquidity/bundle-risk lookups.
_BREAKER_KEY = "solana_tracker_holders"


def _error_detail(body: Any) -> str | None:
    """Best-effort extraction of a provider error message from a non-200
    response body -- see the identical helper in
    domain.intelligence._birdeye_holder_fallback for why this exists:
    HTTP status alone doesn't distinguish a real quota/plan restriction
    from a request-shape bug."""
    if not isinstance(body, dict):
        return None
    for key in ("message", "error", "msg", "detail"):
        val = body.get(key)
        if val:
            return str(val)[:200]
    return None


def _extract_accounts(body: Any) -> tuple[list[dict[str, str]], int | None, bool]:
    if not isinstance(body, dict) or body.get("error"):
        return [], None, False

    raw_accounts = body.get("accounts")
    if not isinstance(raw_accounts, list):
        return [], None, False

    accounts: list[dict[str, str]] = []
    for row in raw_accounts:
        if not isinstance(row, dict):
            continue
        owner = row.get("wallet") or row.get("owner")
        amount = row.get("amount")
        if not owner or amount is None:
            continue
        try:
            if float(amount) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        accounts.append({"owner": str(owner), "amount": str(amount)})

    total = body.get("total")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None

    return accounts, total, bool(body.get("hasMore"))


async def _get_page(
    mint: str,
    api_key: str,
    cursor: str | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[int, Any | None]:
    params = {"limit": str(MAX_PAGE_SIZE)}
    if cursor:
        params["cursor"] = cursor

    headers = {"x-api-key": api_key}
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
            "[HolderDiag] Solana Tracker holder request failed safely: %s",
            type(exc).__name__,
        )
        return 0, None


def _classify_failure(status: int) -> str:
    """Map an HTTP status (0 == request-level exception, see _get_page) to
    a circuit-breaker failure class. 401/402/403 are the fast-trip
    auth/credits class (see module docstring); everything else that isn't
    a 200 -- 429, 5xx, 404, and the status=0 connection/timeout sentinel --
    is the more tolerant transient class."""
    if status in (401, 402, 403):
        return _breaker.FAILURE_AUTH_OR_CREDITS
    return _breaker.FAILURE_TRANSIENT


async def _fetch_paginated_holders(
    mint: str,
    timeout_seconds: float = 10.0,
) -> tuple[list[dict[str, str]] | None, bool]:
    """Fetch a bounded, balance-sorted holder snapshot.

    Returns (accounts, is_partial):
      - accounts is None when the provider was not configured, is
        currently circuit-broken, or failed before ever returning one
        usable page -- a genuine "we have no data at all" outcome.
      - accounts is [] (empty list, not None) when the provider was
        reached and explicitly reported zero holders on a page with no
        further pages -- a genuine, provider-confirmed empty result, kept
        distinct from "unavailable" exactly as before this change.
      - accounts is a non-empty list otherwise.
      - is_partial is True when pagination stopped before the provider
        reported "no more pages" -- either because a LATER page failed
        after an earlier one already produced usable accounts (in which
        case what's returned is real, partial evidence, not a fabricated
        complete snapshot -- see module docstring point 2), or because
        MAX_PAGES was reached while the provider still had more to give
        (the same kind of bounded truncation domain/intelligence/holders.py
        already applies via MAX_HOLDER_ACCOUNTS). is_partial is always
        False when accounts is None (nothing to call partial).
    """
    api_key = os.getenv("SOLANA_TRACKER_API_KEY")
    if not api_key:
        return None, False

    if not _breaker.allow_request(_BREAKER_KEY):
        logger.info(
            "[HolderDiag] Solana Tracker holder fallback skipped for %s — "
            "circuit breaker open (provider unhealthy, cooling down)",
            mint[:8],
        )
        return None, False

    all_accounts: list[dict[str, str]] = []
    got_any_page = False
    cursor: str | None = None

    for page in range(MAX_PAGES):
        status, body = await _get_page(
            mint=mint,
            api_key=api_key,
            cursor=cursor,
            timeout_seconds=timeout_seconds,
        )
        accounts, total, has_more = _extract_accounts(body)

        if status != 200:
            logger.warning(
                "[HolderDiag] Solana Tracker holder fallback unavailable for %s (http=%s) detail=%s",
                mint[:8],
                status,
                _error_detail(body),
            )
            _breaker.record_failure(_BREAKER_KEY, _classify_failure(status))
            if got_any_page:
                # A later page failed after an earlier one already gave us
                # real, usable balances — that partial batch is genuine
                # evidence and must not be discarded just because
                # pagination could not finish. See module docstring point 2.
                logger.info(
                    "[HolderDiag] Solana Tracker: keeping %d holder account(s) "
                    "fetched before the failure on page %d for %s (partial)",
                    len(all_accounts),
                    page,
                    mint[:8],
                )
                break
            return None, False

        if body is None or not isinstance(body, dict) or "accounts" not in body:
            logger.warning(
                "[HolderDiag] Solana Tracker returned an unusable holder payload for %s",
                mint[:8],
            )
            _breaker.record_failure(_BREAKER_KEY, _breaker.FAILURE_TRANSIENT)
            if got_any_page:
                break
            return None, False

        got_any_page = True
        all_accounts.extend(accounts)
        next_cursor = body.get("cursor")

        logger.info(
            "[HolderDiag] Solana Tracker holder fallback page=%d mint=%s "
            "returned=%d total=%s has_more=%s",
            page,
            mint[:8],
            len(accounts),
            total,
            has_more,
        )

        if not has_more or not next_cursor:
            _breaker.record_success(_BREAKER_KEY)
            all_accounts.sort(
                key=lambda row: (-float(row.get("amount") or 0), row.get("owner", ""))
            )
            return all_accounts, False
        cursor = str(next_cursor)
    else:
        # Every MAX_PAGES iterations completed with has_more still true —
        # not a failure (every page we asked for came back healthy), just
        # a snapshot bounded by MAX_PAGES same as holders.MAX_HOLDER_ACCOUNTS
        # bounds the primary RPC path. A clean, fully-successful fetch that
        # happens to be capped is still a health success for the breaker.
        _breaker.record_success(_BREAKER_KEY)
        all_accounts.sort(
            key=lambda row: (-float(row.get("amount") or 0), row.get("owner", ""))
        )
        return all_accounts, True

    # Reached only via the `break` above: a later page failed but an
    # earlier one already produced usable accounts.
    all_accounts.sort(
        key=lambda row: (-float(row.get("amount") or 0), row.get("owner", ""))
    )
    return all_accounts, True


async def fetch_token_holders(
    mint: str,
    timeout_seconds: float = 10.0,
) -> list[dict[str, str]] | None:
    """Fetch a bounded, balance-sorted holder snapshot.

    Returns None when the provider is not configured, unavailable
    (including circuit-broken), or returns an unusable response without
    ever producing a single usable page. An empty successful response is
    returned as [] so the caller can distinguish "provider succeeded but
    no holders" from a provider failure -- unchanged from before this
    file added circuit-breaker/partial-page support.

    Callers that need to know whether a result was a partial (truncated
    by a mid-pagination failure or by MAX_PAGES) fetch — currently only
    install()'s wrapper below, which needs it to set
    HolderAccountsResult.truncated correctly — should call
    _fetch_paginated_holders directly instead of this thin wrapper.
    """
    accounts, _is_partial = await _fetch_paginated_holders(mint, timeout_seconds=timeout_seconds)
    return accounts


def provider_configured() -> bool:
    return bool(os.getenv("SOLANA_TRACKER_API_KEY"))


def provider_name() -> str:
    return "solana_tracker"


def install() -> None:
    """Install Tracker as the indexed fallback after the primary Helius path."""
    from domain.intelligence import holders

    if getattr(holders._fetch_token_accounts, "_alphapulse_solana_tracker_fallback", False):
        return

    original_fetch = holders._fetch_token_accounts

    async def _fetch_with_tracker(contract_address: str, priority=holders.PRIORITY_LOW):
        result = await original_fetch(contract_address, priority=priority)

        # Preserve a real RPC snapshot, including small positive holder sets.
        if result is not None and result.accounts:
            return result

        if not provider_configured():
            return result

        logger.info(
            "[HolderDiag] %s: primary holder path returned %s accounts; "
            "trying Solana Tracker indexed fallback",
            contract_address[:8],
            len(result.accounts) if result is not None else "unavailable",
        )

        try:
            tracker_accounts, was_partial_fetch = await _fetch_paginated_holders(contract_address)
        except Exception as exc:
            logger.warning(
                "[HolderDiag] %s: Solana Tracker fallback failed safely: %s",
                contract_address[:8],
                type(exc).__name__,
            )
            return result

        if tracker_accounts:
            # Truncated if either this codebase's own global cap kicked in
            # (same as the primary RPC path's MAX_HOLDER_ACCOUNTS bound) or
            # the paginated fetch itself stopped early — a later page
            # failing mid-pagination, or MAX_PAGES being reached while the
            # provider still had more to give. Either way the concentration
            # metrics computed from this batch remain accurate for the
            # top-N holders (truncation only ever drops the smallest,
            # longest-tail balances — see domain/intelligence/holders.py),
            # but scoring.py's stricter truncated-snapshot threshold for
            # bundle_pct (BUNDLE_SEVERE_PCT_TRUNCATED) should still apply.
            truncated = was_partial_fetch or len(tracker_accounts) > holders.MAX_HOLDER_ACCOUNTS
            if len(tracker_accounts) > holders.MAX_HOLDER_ACCOUNTS:
                tracker_accounts = tracker_accounts[: holders.MAX_HOLDER_ACCOUNTS]

            logger.info(
                "[HolderDiag] %s: Solana Tracker fallback returned %d positive "
                "holder accounts%s; using them for HolderAnalysis",
                contract_address[:8],
                len(tracker_accounts),
                " (bounded to largest balances)" if truncated else "",
            )
            return holders._HolderAccountsResult(
                accounts=tracker_accounts,
                truncated=truncated,
                raw_account_count=len(tracker_accounts),
            )

        # Never turn an unavailable/empty indexed response into invented
        # concentration data. Preserve the original RPC result semantics.
        return result

    _fetch_with_tracker._alphapulse_solana_tracker_fallback = True
    holders._fetch_token_accounts = _fetch_with_tracker
    logger.info("[HolderDiag] Solana Tracker indexed holder fallback installed")
