import logging

try:
    from config.settings import HELIUS_API, HELIUS_API_KEY, HELIUS_HISTORY_CACHE_TTL_SECONDS
except ImportError:
    HELIUS_API = "https://api.helius.xyz/v0"
    HELIUS_API_KEY = None
    HELIUS_HISTORY_CACHE_TTL_SECONDS = 600.0

from providers.rpc.helius_request_manager import helius_manager, PRIORITY_LOW

logger = logging.getLogger("AlphaPulse.FundingGraph")

# Cost/latency controls. Tracing every holder on every candidate would be
# far too slow/expensive for a live scanner, so this only ever runs on
# candidates that already passed every hard gate and score cutoff (see
# services/pump_radar.py) — same principle as the smart-money lookup.
MAX_WALLETS_TRACED = 15
PAGE_SIZE = 100
MAX_PAGES_PER_WALLET = 3
MIN_CLUSTER_SIZE = 3


async def _get_first_funder(address: str) -> str | None:
    """
    Walks a wallet's transaction history backward (oldest-first, via
    Helius's Enhanced Transaction History API) to find whoever sent it its
    first native SOL transfer — i.e. its funding source.

    Capped at MAX_PAGES_PER_WALLET pages. If the wallet's full history is
    longer than that, pagination stops and this returns None rather than
    guessing — a wallet with that much history is unlikely to be a
    purpose-built insider/sniper wallet anyway, so skipping it doesn't
    weaken detection of the thing this function is actually looking for.

    The per-wallet result is cached (see get_funding_clusters below) since
    the same top-holder wallets frequently reappear across many different
    token candidates within a scan session — tracing the same wallet's
    funder twice within the cache window would be a pure duplicate call.

    NOT YET LIVE-VALIDATED: the pagination parameter name (`before`) and
    the exact shape of `nativeTransfers` match Helius's documented
    Enhanced Transactions API as of this build, but no network access was
    available here to confirm against a live response. Verify against one
    real wallet before trusting this in production.
    """
    url = f"{HELIUS_API}/addresses/{address}/transactions"
    before = None
    last_page: list = []

    for page_num in range(MAX_PAGES_PER_WALLET):
        params = {"api-key": HELIUS_API_KEY, "limit": PAGE_SIZE}
        if before:
            params["before"] = before

        page = await helius_manager.request_json(
            "GET",
            url,
            params=params,
            priority=PRIORITY_LOW,
            timeout=8,
            context=f"funding_graph:{address}:page{page_num}",
        )

        if not isinstance(page, list) or not page:
            break

        last_page = page
        if len(page) < PAGE_SIZE:
            # Reached the wallet's earliest transactions.
            break
        before = page[-1].get("signature")
    else:
        # Exhausted MAX_PAGES_PER_WALLET without reaching genesis.
        return None

    if not last_page:
        return None

    oldest_tx = last_page[-1]
    for transfer in (oldest_tx.get("nativeTransfers") or []):
        if transfer.get("toUserAccount") == address:
            return transfer.get("fromUserAccount")

    return None


async def get_funding_clusters(holder_addresses: list[str]) -> dict:
    """
    Real funding-graph bundle/sniper detection: traces the token's top
    holder wallets back to whoever funded each one, then groups wallets
    that share the same funding source.

    This is a genuine upgrade over services/holders.py's bundle detector,
    which only clusters wallets with SIMILAR BALANCE SIZE — a surface
    heuristic that produces false positives (coincidentally similar
    legitimate buys) and false negatives (a bundle deliberately using
    varied amounts to dodge that exact heuristic). Wallets funded from the
    same source right before a launch is a much stronger, harder-to-fake
    signal of coordinated/insider control.

    Returns:
        {
          "clusters": [{"funder": <addr>, "wallets": [...]}, ...],
          "largest_cluster_size": int,
          "traced": int,   # how many wallets a funder was actually found for
        }
    All-empty/zero if HELIUS_API_KEY is missing or nothing could be
    traced — never a fabricated result.
    """
    addrs = (holder_addresses or [])[:MAX_WALLETS_TRACED]
    if not addrs or not HELIUS_API_KEY:
        return {"clusters": [], "largest_cluster_size": 0, "traced": 0}

    funders: dict[str, list[str]] = {}
    traced = 0

    for addr in addrs:
        cache_key = f"first_funder:{addr}"
        funder = helius_manager.get_cached(cache_key)
        if funder is None:
            funder = await _get_first_funder(addr)
            # Cache even a "not found" (None) result isn't useful to
            # distinguish from "not yet looked up", so only cache a real
            # hit — this keeps behavior identical to the un-cached path
            # for wallets whose funder genuinely couldn't be traced.
            if funder:
                helius_manager.set_cached(cache_key, funder, HELIUS_HISTORY_CACHE_TTL_SECONDS)
        if funder:
            traced += 1
            funders.setdefault(funder, []).append(addr)

    clusters = [
        {"funder": f, "wallets": wallets}
        for f, wallets in funders.items()
        if len(wallets) >= MIN_CLUSTER_SIZE
    ]
    clusters.sort(key=lambda c: len(c["wallets"]), reverse=True)
    largest = len(clusters[0]["wallets"]) if clusters else 0

    return {
        "clusters": clusters,
        "largest_cluster_size": largest,
        "traced": traced,
    }
