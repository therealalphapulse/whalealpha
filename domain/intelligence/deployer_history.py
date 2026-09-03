import logging

try:
    from config.settings import HELIUS_API, HELIUS_API_KEY, HELIUS_HISTORY_CACHE_TTL_SECONDS
except ImportError:
    HELIUS_API = "https://api.helius.xyz/v0"
    HELIUS_API_KEY = None
    HELIUS_HISTORY_CACHE_TTL_SECONDS = 600.0

from providers.rpc.helius_request_manager import helius_manager, PRIORITY_LOW

logger = logging.getLogger("AlphaPulse.DeployerHistory")

PAGE_SIZE = 100
MAX_PAGES = 3

# Helius Enhanced Transactions `type`/`source` values associated with
# token creation. Checked defensively (not all of these are guaranteed to
# appear for every launch platform) — see the NOT YET LIVE-VALIDATED note
# below.
_CREATION_TYPES = {"CREATE", "TOKEN_MINT", "CREATE_POOL", "INIT_POOL"}
_CREATION_SOURCES = {"PUMP_FUN"}


async def get_deployer_launch_history(dev_address: str, current_contract: str) -> dict | None:
    """
    Checks a token's dev/creator wallet for evidence of PRIOR token
    launches, via Helius's Enhanced Transaction History API.

    Serial ruggers overwhelmingly reuse the same deployer wallet across
    many launches — this is one of the highest-signal, cheapest checks
    available and nothing in the pipeline currently looks at it.

    Returns {"prior_launches": int, "prior_mints": [...]} when the lookup
    genuinely completed (even if prior_launches is 0 — a real, verified
    clean history), or None when the check could NOT be completed
    (missing API key, no dev_address, request failure).

    Callers MUST treat None as "unable to verify" and never display or
    score it as a clean 0 — a failed check and a verified-clean wallet are
    different facts, and confusing them would misrepresent an unknown as
    a positive signal.

    The raw set of mints ever seen created by dev_address is cached
    (independent of current_contract, which is only used to exclude the
    token being checked right now) — the same deployer wallet frequently
    shows up across several candidates in a scan session, so this avoids
    re-paginating its full transaction history each time.

    NOT YET LIVE-VALIDATED: `_CREATION_TYPES`/`_CREATION_SOURCES` are a
    best-effort guess at how Helius categorizes Pump.fun / Raydium pool
    creation transactions, written without network access to confirm
    against a real deployer wallet's transaction history. Before trusting
    `prior_launches` in production, pull the raw transaction list for a
    wallet you know has launched multiple tokens and confirm these type/
    source values actually appear — otherwise this will silently
    undercount and should be treated as a lower bound, not an exact count.
    """
    if not HELIUS_API_KEY or not dev_address:
        return None

    cache_key = f"deployer_mints:{dev_address}"
    seen_mints_all = helius_manager.get_cached(cache_key)

    if seen_mints_all is None:
        url = f"{HELIUS_API}/addresses/{dev_address}/transactions"
        seen_mints: set[str] = set()
        before = None
        fetched_ok = False

        for page_num in range(MAX_PAGES):
            params = {"api-key": HELIUS_API_KEY, "limit": PAGE_SIZE}
            if before:
                params["before"] = before

            page = await helius_manager.request_json(
                "GET",
                url,
                params=params,
                priority=PRIORITY_LOW,
                timeout=8,
                context=f"deployer_history:{dev_address}:page{page_num}",
            )

            if not isinstance(page, list) or not page:
                break

            fetched_ok = True

            for tx in page:
                tx_type = (tx.get("type") or "").upper()
                source = (tx.get("source") or "").upper()
                if tx_type in _CREATION_TYPES or source in _CREATION_SOURCES:
                    for transfer in (tx.get("tokenTransfers") or []):
                        mint = transfer.get("mint")
                        if mint:
                            seen_mints.add(mint)

            if len(page) < PAGE_SIZE:
                break
            before = page[-1].get("signature")

        if not fetched_ok:
            return None

        seen_mints_all = list(seen_mints)
        helius_manager.set_cached(cache_key, seen_mints_all, HELIUS_HISTORY_CACHE_TTL_SECONDS)

    prior_mints = [m for m in seen_mints_all if m != current_contract]

    return {
        "prior_launches": len(prior_mints),
        "prior_mints": prior_mints[:10],
    }
