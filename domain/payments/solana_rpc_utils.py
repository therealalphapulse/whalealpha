"""
Solana RPC URL and Associated-Token-Account helpers for crypto premium
payment verification (domain/payments/solana_payment_verify.py).

Premium subscriptions can still be paid in SOL/USDC/USDT on Solana —
that is a separate concern from the WhaleAlpha *trading* wallet, which
is now fully native to Robinhood Chain (see domain/trading/real/
robinhood_wallet.py / robinhood_swap.py). This module exists so payment
verification doesn't need to depend on the old Solana trading modules
that were removed from domain/trading/real/ during the Robinhood-chain
migration cleanup.
"""
from __future__ import annotations

from solders.pubkey import Pubkey

from config.settings import HELIUS_API_KEY

if HELIUS_API_KEY:
    SOLANA_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
else:
    SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Derive the SPL Associated Token Account address for (owner, mint)."""
    ata, _bump = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return ata
