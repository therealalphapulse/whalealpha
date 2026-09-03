"""
On-chain verification for crypto Premium payments.

This is the "automatic" half of the hybrid payment system: given a
transaction signature the user claims paid for a plan, confirm (by
reading the transaction itself from the RPC, not trusting anything the
user typed) that it actually transferred the right asset, to the right
address, for at least the right amount, and hasn't been used before.

IMPORTANT — TEST BEFORE TRUSTING THIS WITH REAL ACTIVATIONS
-------------------------------------------------------------
Same caveat as services/wallet_withdraw.py: do a real end-to-end test
(pay a plan for real with a throwaway/small amount, on mainnet, for
each of SOL, USDC, and USDT) before relying on this to gate paid
access. In particular, double-check the USDC_MINT / USDT_MINT
constants below against Solana's official token list for your cluster
before going live — using the wrong mint would just make that coin
silently never verify (safe failure mode: no free activation), but it
would also mean real user payments don't get credited automatically,
so it's worth confirming before launch rather than after a complaint.

Duplicate-use prevention is enforced by the CALLER
(services.premium_payments.verify_crypto_payment), which checks the
signature against every existing PremiumPayment row before calling
into this module — this module only answers "does this tx really pay
X of asset Y to address Z", it doesn't know about your database.
"""

import logging
import aiohttp

from domain.trading.real.jupiter_swap import SOLANA_RPC_URL
from domain.trading.real.wallet_withdraw import get_associated_token_address
from solders.pubkey import Pubkey

logger = logging.getLogger("AlphaPulse.PaymentVerify")

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
MINTS = {"USDC": USDC_MINT, "USDT": USDT_MINT}

# How much underpayment to tolerate (covers negligible rounding, never
# a meaningful discount). Anything short of this is rejected as
# underpaid rather than silently accepted.
AMOUNT_TOLERANCE = 0.0005


class VerificationError(ValueError):
    """Expected verification failure (bad signature, wrong amount, etc.) —
    distinct from an unexpected/transient RPC error."""


async def _get_transaction(signature: str) -> dict:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(SOLANA_RPC_URL, json=payload, timeout=20) as resp:
            data = await resp.json()
            if "error" in data:
                raise VerificationError(f"RPC error looking up transaction: {data['error']}")
            result = data.get("result")
            if not result:
                raise VerificationError(
                    "Transaction not found yet. If you just sent it, wait ~30 seconds for it to "
                    "confirm and try again."
                )
            return result


async def verify_sol_payment(signature: str, receive_address: str, expected_sol: float) -> float:
    """
    Returns the actual SOL amount transferred to `receive_address` in
    this transaction if it's at least `expected_sol` (minus tolerance).
    Raises VerificationError otherwise. Never returns a value below
    what was actually paid.
    """
    tx = await _get_transaction(signature)
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        raise VerificationError("That transaction failed on-chain — nothing was transferred.")

    message = (tx.get("transaction") or {}).get("message") or {}
    account_keys = message.get("accountKeys") or []
    key_strings = [
        (k.get("pubkey") if isinstance(k, dict) else k) for k in account_keys
    ]

    if receive_address not in key_strings:
        raise VerificationError("This transaction doesn't involve the payment address at all.")

    idx = key_strings.index(receive_address)
    pre = (meta.get("preBalances") or [])
    post = (meta.get("postBalances") or [])
    if idx >= len(pre) or idx >= len(post):
        raise VerificationError("Couldn't read balance changes for the payment address.")

    delta_lamports = post[idx] - pre[idx]
    actual_sol = delta_lamports / 1_000_000_000

    if actual_sol < expected_sol - AMOUNT_TOLERANCE:
        raise VerificationError(
            f"This transaction only sent {actual_sol:.4f} SOL to the payment address — "
            f"{expected_sol} SOL was expected."
        )

    return actual_sol


async def verify_spl_payment(signature: str, receive_owner_address: str, asset: str, expected_amount: float) -> float:
    """
    Same as verify_sol_payment but for an SPL token (USDC/USDT) sent to
    the associated token account of `receive_owner_address` for that
    asset's mint. Returns the actual amount transferred.
    """
    mint = MINTS.get(asset.upper())
    if not mint:
        raise VerificationError(f"Unsupported asset: {asset}")

    owner_pubkey = Pubkey.from_string(receive_owner_address)
    mint_pubkey = Pubkey.from_string(mint)
    expected_ata = str(get_associated_token_address(owner_pubkey, mint_pubkey))

    tx = await _get_transaction(signature)
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        raise VerificationError("That transaction failed on-chain — nothing was transferred.")

    pre_balances = meta.get("preTokenBalances") or []
    post_balances = meta.get("postTokenBalances") or []

    message = (tx.get("transaction") or {}).get("message") or {}
    account_keys = message.get("accountKeys") or []
    key_strings = [(k.get("pubkey") if isinstance(k, dict) else k) for k in account_keys]

    def _amount_for_our_ata(balances: list) -> float:
        for b in balances:
            idx = b.get("accountIndex")
            if idx is None or idx >= len(key_strings):
                continue
            account_address = key_strings[idx]
            if account_address != expected_ata:
                continue
            if b.get("mint") != mint:
                continue
            ui_amount = ((b.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0
            return float(ui_amount)
        return 0.0

    pre_amount = _amount_for_our_ata(pre_balances)
    post_amount = _amount_for_our_ata(post_balances)
    actual_amount = post_amount - pre_amount

    if expected_ata not in key_strings:
        raise VerificationError("This transaction doesn't send tokens to the payment address's token account.")

    if actual_amount < expected_amount - AMOUNT_TOLERANCE:
        raise VerificationError(
            f"This transaction only sent {actual_amount:.4f} {asset} to the payment address — "
            f"{expected_amount} {asset} was expected."
        )

    return actual_amount
