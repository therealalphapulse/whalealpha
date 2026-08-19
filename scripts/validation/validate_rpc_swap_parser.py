import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "..", "src"))
import _stub_deps2  # noqa: F401
import _stub_httpx  # noqa: F401
import _stub_structlog  # noqa: F401

from whale_alpha.integrations.wallet_discovery_source import (  # REAL module
    WalletHistoryFetch,
    _extract_swap_from_rpc_transaction,
)

print("=" * 70)
print("TASK 2 — RPC-fallback swap parser RUNTIME VALIDATION (real function)")
print("=" * 70)

WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
OTHER = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

def make_tx(*, sol_delta_lamports, pre_token, post_token, err=None, block_time=1_725_000_000):
    return {
        "blockTime": block_time,
        "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}, {"pubkey": OTHER}]}},
        "meta": {
            "err": err,
            "preBalances": [5_000_000_000, 1_000_000_000],
            "postBalances": [5_000_000_000 + sol_delta_lamports, 1_000_000_000],
            "preTokenBalances": pre_token,
            "postTokenBalances": post_token,
        },
    }

sol_price = 150.0

# 1. BUY: wallet spends SOL, receives USDC-mint token
buy_tx = make_tx(
    sol_delta_lamports=-2_000_000_000,  # spent 2 SOL
    pre_token=[],
    post_token=[{"accountIndex": 2, "owner": WALLET, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": 300.0}}],
)
swap = _extract_swap_from_rpc_transaction(buy_tx, WALLET, sol_price)
assert swap is not None and swap.side == "BUY" and swap.token_mint == USDC_MINT
assert abs(swap.amount_usd - (2.0 * sol_price)) < 0.01
print(f"[PASS] BUY reconstructed from raw RPC tx: {swap.side} {swap.token_mint[:8]}... ${swap.amount_usd:.2f}")

# 2. SELL: wallet's token balance fully drains (account closes, vanishes from postTokenBalances), receives SOL
sell_tx = make_tx(
    sol_delta_lamports=+1_500_000_000,  # received 1.5 SOL
    pre_token=[{"accountIndex": 2, "owner": WALLET, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": 300.0}}],
    post_token=[],
)
swap2 = _extract_swap_from_rpc_transaction(sell_tx, WALLET, sol_price)
assert swap2 is not None and swap2.side == "SELL" and swap2.token_mint == USDC_MINT
assert abs(swap2.amount_usd - (1.5 * sol_price)) < 0.01
print(f"[PASS] SELL reconstructed (token account closed): {swap2.side} {swap2.token_mint[:8]}... ${swap2.amount_usd:.2f}")

# 3. Failed transaction -> never counted as a swap (err is not None)
failed_tx = make_tx(
    sol_delta_lamports=-2_000_000_000,
    pre_token=[],
    post_token=[{"accountIndex": 2, "owner": WALLET, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": 300.0}}],
    err={"InstructionError": [0, "Custom"]},
)
assert _extract_swap_from_rpc_transaction(failed_tx, WALLET, sol_price) is None
print("[PASS] Failed transaction (meta.err set) -> never fabricated as a swap")

# 4. Plain SPL transfer (token moved, but NO corresponding SOL delta) -> not a swap
transfer_tx = make_tx(
    sol_delta_lamports=0,
    pre_token=[],
    post_token=[{"accountIndex": 2, "owner": WALLET, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": 50.0}}],
)
assert _extract_swap_from_rpc_transaction(transfer_tx, WALLET, sol_price) is None
print("[PASS] Plain token transfer (no SOL-side delta) -> correctly NOT classified as a swap (no fabrication)")

# 5. Wallet not present in this transaction at all -> None, not a crash
unrelated_tx = {
    "blockTime": 1_725_000_000,
    "transaction": {"message": {"accountKeys": [{"pubkey": OTHER}]}},
    "meta": {"err": None, "preBalances": [1], "postBalances": [1], "preTokenBalances": [], "postTokenBalances": []},
}
assert _extract_swap_from_rpc_transaction(unrelated_tx, WALLET, sol_price) is None
print("[PASS] Wallet absent from accountKeys -> None, handled gracefully")

# 6. WalletHistoryFetch dataclass: partial/source flags used by the fallback chain
primary = WalletHistoryFetch(swaps=None, transient=True, source="HELIUS")
fallback = WalletHistoryFetch(swaps=[swap], transient=False, source="RPC_FALLBACK", partial=True)
assert primary.partial is False and fallback.partial is True
print(f"[PASS] WalletHistoryFetch: primary.partial={primary.partial}, RPC_FALLBACK.partial={fallback.partial}")

print()
print("ALL 6 TASK-2 RUNTIME CHECKS PASSED against the real wallet_discovery_source.py functions.")
