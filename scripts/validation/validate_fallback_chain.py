import asyncio, sys, types
import os
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "..", "src"))
import _stub_httpx        # noqa: F401
import _stub_structlog    # noqa: F401
import _stub_deps2        # noqa: F401

import whale_alpha.integrations.wallet_discovery_source as wds  # REAL module, module ref for monkeypatching


class FakeEnv:
    HELIUS_API_KEY = "test-key"
    HELIUS_API_BASE = "https://api.helius.xyz"
    DISCOVERY_HISTORY_CACHE_TTL_SECONDS = 300
    DISCOVERY_HISTORY_NEGATIVE_CACHE_TTL_SECONDS = 3600
    DISCOVERY_HISTORY_MAX_CONCURRENCY = 5
    DISCOVERY_HISTORY_MAX_RETRIES = 1
    DISCOVERY_HISTORY_RETRY_BASE_SECONDS = 0.01
    DISCOVERY_HISTORY_RETRY_MAX_SECONDS = 0.02
    DISCOVERY_HISTORY_STALE_CACHE_TTL_SECONDS = 21600
    DISCOVERY_HISTORY_RPC_FALLBACK_ENABLED = True
    DISCOVERY_HISTORY_RPC_FALLBACK_MAX_SIGNATURES = 10
    DISCOVERY_RPC_MIN_INTERVAL_SECONDS = 0.0
    DISCOVERY_RPC_MAX_RETRIES = 1


class FakeResp:
    def __init__(self, status): self.status_code = status; self.headers = {}
    def json(self): return []


class Always429Client:
    async def request(self, method, url, timeout=None, **kwargs):
        return FakeResp(429)


async def main():
    print("=" * 70)
    print("TASK 2 — fetch_wallet_swap_history FALLBACK-CHAIN orchestration (real function)")
    print("=" * 70)
    env = FakeEnv()
    client = Always429Client()

    # --- Scenario A: Helius rate-limited, no stale cache, RPC fallback disabled -> transient (retry queue)
    env.DISCOVERY_HISTORY_RPC_FALLBACK_ENABLED = False
    result_a = await wds.fetch_wallet_swap_history(client, env, "WALLET_A", sol_price_usd=150.0, connection=None)
    assert result_a.swaps is None and result_a.transient is True
    print(f"[PASS] Scenario A: Helius 429, no cache, RPC fallback disabled -> transient=True (goes to retry queue)")

    # --- Scenario B: Helius rate-limited, RPC fallback enabled + connection supplied, RPC produces real swaps
    env.DISCOVERY_HISTORY_RPC_FALLBACK_ENABLED = True

    async def fake_get_wallet_recent_transactions(connection, address, **kwargs):
        return [{
            "blockTime": 1_725_000_000,
            "transaction": {"message": {"accountKeys": [{"pubkey": address}, {"pubkey": "OTHER"}]}},
            "meta": {
                "err": None,
                "preBalances": [5_000_000_000, 0],
                "postBalances": [3_000_000_000, 0],
                "preTokenBalances": [],
                "postTokenBalances": [
                    {"accountIndex": 2, "owner": address, "mint": "TOKEN_MINT", "uiTokenAmount": {"uiAmount": 400.0}}
                ],
            },
        }]

    wds.get_wallet_recent_transactions = fake_get_wallet_recent_transactions  # monkeypatch the real module's binding
    result_b = await wds.fetch_wallet_swap_history(
        client, env, "WALLET_B", sol_price_usd=150.0, connection=object()
    )
    assert result_b.swaps is not None and len(result_b.swaps) == 1
    assert result_b.source == "RPC_FALLBACK" and result_b.partial is True
    print(f"[PASS] Scenario B: Helius 429 -> RPC fallback reconstructs {len(result_b.swaps)} swap(s), "
          f"source={result_b.source}, partial={result_b.partial}")

    # --- Scenario C: same wallet again -> now served from the STALE CACHE that Scenario B populated
    # (Helius still 429ing, but we never call the RPC fallback this time)
    calls_before = 0
    orig = wds.get_wallet_recent_transactions
    call_count = {"n": 0}
    async def counting_rpc(*a, **kw):
        call_count["n"] += 1
        return await orig(*a, **kw)
    wds.get_wallet_recent_transactions = counting_rpc
    result_c = await wds.fetch_wallet_swap_history(client, env, "WALLET_B", sol_price_usd=150.0, connection=object())
    assert result_c.source == "CACHE_STALE" and result_c.partial is True
    assert call_count["n"] == 0  # RPC fallback never invoked -- served from stale cache first
    print(f"[PASS] Scenario C: same wallet re-fetched -> served from stale cache (source={result_c.source}), "
          f"RPC fallback NOT re-invoked ({call_count['n']} calls)")

    print()
    print("ALL 3 FALLBACK-CHAIN ORCHESTRATION SCENARIOS PASSED against the real fetch_wallet_swap_history.")


asyncio.run(main())
