"""
Real Wallet withdrawals — sending SOL or SPL tokens from a user's
AlphaPulse Real Wallet to an external address.

SCOPE / WHAT TO TEST BEFORE TRUSTING THIS WITH SIZE
----------------------------------------------------
Solana transactions are atomic: if an instruction or account is wrong,
the whole transaction fails on-chain and nothing moves — it does not
silently send to the wrong place or the wrong amount. That said, this
file hand-builds the SPL Associated Token Account + TransferChecked
instructions (there's no spl-token/solana-py dependency in this project,
only solders), so before relying on it for real size: do one small SOL
withdrawal and one small SPL token withdrawal first, on mainnet, to a
wallet you control, and confirm both the amount and the destination
match before treating this as done.

Flow for every withdrawal:
  1. validate_withdraw_address() - reject anything that isn't a real
     base58 Solana pubkey up front, before touching the wallet.
  2. execute_sol_withdrawal() / execute_spl_withdrawal() - build the
     transfer instruction(s), decrypt the key just-in-time (same
     just-in-time-decrypt pattern as real_trade_engine.py), sign,
     broadcast, poll for on-chain confirmation, and record a
     WalletWithdrawal row regardless of outcome once broadcast.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.hash import Hash
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.system_program import transfer, TransferParams, ID as SYSTEM_PROGRAM_ID

from infra.db.session import async_session
from models.wallet_withdrawal import WalletWithdrawal
from domain.trading.real.solana_wallet import get_real_wallet
from infra.kms.wallet_crypto import decrypt_secret
from domain.trading.real.jupiter_swap import (
    SOLANA_RPC_URL,
    get_sol_balance,
    confirm_signature,
    realwallet_rpc_call,
    SwapError,
)

logger = logging.getLogger("AlphaPulse.WalletWithdraw")

TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# Left in the wallet on a "Max" SOL withdrawal so the account stays
# rent-exempt and there's still something left to pay the network fee
# on the withdrawal transaction itself.
SOL_WITHDRAW_RESERVE = 0.003

# One withdrawal in flight per user at a time. Prevents a double-tap or a
# retried request from racing two withdrawals against the same balance
# before either has broadcast — mirrors the atomic DB claim
# real_trade_engine.execute_real_sell() uses for sells, at the layer
# available here (withdrawals have no open "position" row to claim
# against beforehand, so an in-process lock is the equivalent guard).
_withdraw_locks: dict[int, asyncio.Lock] = {}


def _withdraw_lock(user_id: int) -> asyncio.Lock:
    lock = _withdraw_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _withdraw_locks[user_id] = lock
    return lock


class WithdrawError(ValueError):
    """User-facing withdrawal validation/execution error."""


def validate_withdraw_address(address: str) -> bool:
    """True if `address` decodes as a valid 32-byte Solana public key."""
    try:
        Pubkey.from_string(address.strip())
        return True
    except Exception:
        return False


def get_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    ata, _bump = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return ata


async def _get_latest_blockhash() -> Hash:
    # Direct, bounded, multi-provider path (see jupiter_swap.realwallet_rpc_call)
    # instead of the shared MultiRPCManager queue — a withdrawal is exactly
    # as latency- and reliability-sensitive as a buy/sell, and must not wait
    # on unrelated background traffic or a background-degraded provider.
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]}
    try:
        data = await realwallet_rpc_call(payload, context="wallet_withdraw:latest_blockhash")
    except SwapError as e:
        raise WithdrawError(f"Could not fetch a recent blockhash from the RPC: {e}")
    value = (data.get("result") or {}).get("value") or {}
    blockhash = value.get("blockhash")
    if not blockhash:
        raise WithdrawError("Could not fetch a recent blockhash from the RPC. Try again shortly.")
    return Hash.from_string(blockhash)


async def _account_exists(pubkey: Pubkey) -> bool:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [str(pubkey), {"encoding": "base64"}],
    }
    try:
        data = await realwallet_rpc_call(payload, context="wallet_withdraw:account_exists")
    except SwapError as e:
        raise WithdrawError(f"Could not check the destination account on-chain: {e}")
    return (data.get("result") or {}).get("value") is not None


async def _broadcast(signed_tx: Transaction) -> str:
    import base64
    raw_b64 = base64.b64encode(bytes(signed_tx)).decode()
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [raw_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
    }
    try:
        data = await realwallet_rpc_call(payload, context="wallet_withdraw:broadcast", timeout_s=30, attempts_per_provider=2)
    except SwapError as e:
        raise WithdrawError(f"RPC did not accept the transaction: {e}")
    signature = data.get("result")
    if not signature:
        raise WithdrawError("RPC did not return a transaction signature.")
    return signature


async def _record_withdrawal(user_id, mint, symbol, amount, destination, signature, status, fail_reason=None):
    async with async_session() as session:
        row = WalletWithdrawal(
            user_id=user_id, mint=mint, symbol=symbol, amount=amount,
            destination_address=destination, tx_signature=signature,
            status=status, fail_reason=fail_reason,
        )
        session.add(row)
        await session.commit()
        return row


async def get_max_sol_withdrawable(user_id: int) -> float:
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return 0.0
    balance = await get_sol_balance(wallet.public_key)
    return max(0.0, balance - SOL_WITHDRAW_RESERVE)


async def execute_sol_withdrawal(user_id: int, to_address: str, sol_amount: float) -> dict:
    """
    Sends native SOL from the user's Real Wallet to `to_address`.
    Returns {"ok": True, "signature": str, "confirmation": "confirmed"|"timeout"}
    or {"ok": False, "reason": str}. Never raises for expected failure
    modes — see real_trade_engine.py's docstrings for the same convention.
    """
    if not validate_withdraw_address(to_address):
        return {"ok": False, "reason": "That doesn't look like a valid Solana address. Double-check it and try again."}

    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"ok": False, "reason": "No active Real Wallet. Use /realwallet to set one up."}

    if sol_amount <= 0:
        return {"ok": False, "reason": "Amount must be greater than 0 SOL."}

    lock = _withdraw_lock(user_id)
    if lock.locked():
        return {"ok": False, "reason": "A withdrawal for this wallet is already in progress. Wait for it to finish before starting another."}

    async with lock:
        balance = await get_sol_balance(wallet.public_key)
        if sol_amount > balance - SOL_WITHDRAW_RESERVE:
            return {
                "ok": False,
                "reason": (
                    f"That would leave less than {SOL_WITHDRAW_RESERVE} SOL in the wallet "
                    f"(needed to stay rent-exempt and cover fees). Max withdrawable right now: "
                    f"{max(0.0, balance - SOL_WITHDRAW_RESERVE):.4f} SOL."
                ),
            }

        from_pubkey = Pubkey.from_string(wallet.public_key)
        to_pubkey = Pubkey.from_string(to_address.strip())
        lamports = int(sol_amount * 1_000_000_000)

        try:
            blockhash = await _get_latest_blockhash()
            ix = transfer(TransferParams(from_pubkey=from_pubkey, to_pubkey=to_pubkey, lamports=lamports))

            secret_bytes = decrypt_secret(wallet.encrypted_secret, wallet.encryption_nonce)
            try:
                keypair = Keypair.from_bytes(secret_bytes)
                signed_tx = Transaction.new_signed_with_payer([ix], from_pubkey, [keypair], blockhash)
            finally:
                del secret_bytes

            signature = await _broadcast(signed_tx)
        except WithdrawError as e:
            logger.warning(f"SOL withdrawal failed for user {user_id}: {e}")
            return {"ok": False, "reason": str(e)}
        except Exception as e:
            logger.error(f"Unexpected SOL withdrawal error for user {user_id}: {e}")
            return {"ok": False, "reason": "Unexpected error building/sending the transaction. No funds were moved if this happened before broadcast."}

        result = await confirm_signature(signature)
        status = result["status"]

        if status == "failed":
            await _record_withdrawal(user_id, "SOL", "SOL", sol_amount, to_address, signature, "failed", str(result.get("err")))
            return {"ok": False, "reason": f"Transaction was rejected on-chain (tx: {signature}). No funds moved."}

        await _record_withdrawal(user_id, "SOL", "SOL", sol_amount, to_address, signature, status)
        return {"ok": True, "signature": signature, "confirmation": status}


async def execute_spl_withdrawal(
    user_id: int, mint: str, symbol: str, token_amount: float, decimals: int, to_address: str
) -> dict:
    """
    Sends an SPL token from the user's Real Wallet to `to_address`,
    creating the recipient's associated token account first if it
    doesn't exist yet (funded by the sender, same as every wallet app
    does). Same return-dict convention as execute_sol_withdrawal().
    """
    if not validate_withdraw_address(to_address):
        return {"ok": False, "reason": "That doesn't look like a valid Solana address. Double-check it and try again."}

    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"ok": False, "reason": "No active Real Wallet. Use /realwallet to set one up."}

    if token_amount <= 0:
        return {"ok": False, "reason": "Amount must be greater than 0."}

    lock = _withdraw_lock(user_id)
    if lock.locked():
        return {"ok": False, "reason": "A withdrawal for this wallet is already in progress. Wait for it to finish before starting another."}

    async with lock:
        owner_pubkey = Pubkey.from_string(wallet.public_key)
        mint_pubkey = Pubkey.from_string(mint)
        dest_owner_pubkey = Pubkey.from_string(to_address.strip())

        source_ata = get_associated_token_address(owner_pubkey, mint_pubkey)
        dest_ata = get_associated_token_address(dest_owner_pubkey, mint_pubkey)

        amount_units = int(round(token_amount * (10 ** decimals)))

        instructions = []
        try:
            if not await _account_exists(dest_ata):
                # AssociatedTokenAccount "CreateIdempotent" instruction (index 1):
                # safe even if the account gets created elsewhere in a race.
                instructions.append(Instruction(
                    program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
                    accounts=[
                        AccountMeta(owner_pubkey, is_signer=True, is_writable=True),   # funding account (sender pays rent)
                        AccountMeta(dest_ata, is_signer=False, is_writable=True),
                        AccountMeta(dest_owner_pubkey, is_signer=False, is_writable=False),
                        AccountMeta(mint_pubkey, is_signer=False, is_writable=False),
                        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                        AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                    ],
                    data=bytes([1]),
                ))

            # SPL Token "TransferChecked" instruction (index 12). Using the
            # checked variant (vs plain Transfer) so the on-chain program
            # itself verifies `decimals` matches the mint — if AlphaPulse
            # ever got the decimals wrong, the transaction fails instead of
            # silently moving the wrong amount.
            transfer_data = bytes([12]) + amount_units.to_bytes(8, "little") + bytes([decimals])
            instructions.append(Instruction(
                program_id=TOKEN_PROGRAM_ID,
                accounts=[
                    AccountMeta(source_ata, is_signer=False, is_writable=True),
                    AccountMeta(mint_pubkey, is_signer=False, is_writable=False),
                    AccountMeta(dest_ata, is_signer=False, is_writable=True),
                    AccountMeta(owner_pubkey, is_signer=True, is_writable=False),
                ],
                data=transfer_data,
            ))

            blockhash = await _get_latest_blockhash()

            secret_bytes = decrypt_secret(wallet.encrypted_secret, wallet.encryption_nonce)
            try:
                keypair = Keypair.from_bytes(secret_bytes)
                signed_tx = Transaction.new_signed_with_payer(instructions, owner_pubkey, [keypair], blockhash)
            finally:
                del secret_bytes

            signature = await _broadcast(signed_tx)
        except WithdrawError as e:
            logger.warning(f"SPL withdrawal failed for user {user_id} ({mint}): {e}")
            return {"ok": False, "reason": str(e)}
        except Exception as e:
            logger.error(f"Unexpected SPL withdrawal error for user {user_id} ({mint}): {e}")
            return {"ok": False, "reason": "Unexpected error building/sending the transaction. No funds were moved if this happened before broadcast."}

        result = await confirm_signature(signature)
        status = result["status"]

        if status == "failed":
            await _record_withdrawal(user_id, mint, symbol, token_amount, to_address, signature, "failed", str(result.get("err")))
            return {"ok": False, "reason": f"Transaction was rejected on-chain (tx: {signature}). No funds moved."}

        await _record_withdrawal(user_id, mint, symbol, token_amount, to_address, signature, status)
        return {"ok": True, "signature": signature, "confirmation": status}


async def get_withdrawal_history(user_id: int, limit: int = 10) -> list[WalletWithdrawal]:
    async with async_session() as session:
        result = await session.execute(
            select(WalletWithdrawal)
            .where(WalletWithdrawal.user_id == user_id)
            .order_by(WalletWithdrawal.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()
