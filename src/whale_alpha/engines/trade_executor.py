"""Executes a swap via Jupiter — port of src/engines/manualTrading/tradeExecutor.ts.

This is the single choke point both manual trades and approved auto-trades
pass through, so risk checks and logging stay consistent.

SECURITY: decrypts the user's key only for the duration of this call and never
logs it. For anything beyond development, replace decrypt_secret_bytes with a
call into your KMS signer instead of materializing a Keypair in app memory at
all.

--- NEW vs. the TS original (porting requirement #3) ---
Before sending the swap transaction, this now writes a Trade row with status
SUBMITTED to the database *first* (with the blockhash/lastValidBlockHeight the
transaction was built against), and only updates it to CONFIRMED/FAILED after
the RPC confirms. This did not exist in the TS version: the old code awaited
`sendTransaction` + `confirmTransaction` with no durable record in between, so
a process crash between submission and confirmation could leave a trade that
actually landed on-chain with no corresponding DB row (or stuck at PENDING
forever). See engines/reconciliation.py for the startup-time sweep that
resolves any trade left in PENDING/SUBMITTED from a prior process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from sqlalchemy.ext.asyncio import AsyncSession

from whale_alpha.config import Env
from whale_alpha.db.models import Trade, TradeStatus
from whale_alpha.integrations import jupiter_client
from whale_alpha.integrations.solana_connection import create_connection
from whale_alpha.utils.logger import child_logger
from whale_alpha.utils.security.encryption import (
    EncryptedPayload,
    decrypt_secret_bytes,
    deserialize_encrypted,
    zero_bytearray,
)

log = child_logger("tradeExecutor")

SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class ExecuteTradeParams:
    side: Literal["BUY", "SELL"]
    token_mint: str
    amount_lamports_or_tokens: int  # lamports if BUY (spending SOL), token base units if SELL
    slippage_bps: int
    encrypted_wallet_key: str  # serialized EncryptedPayload from the User record
    trade_row_id: str  # id of the pre-created PENDING Trade row (see below)


@dataclass
class ExecuteTradeResult:
    tx_signature: str
    in_amount: str
    out_amount: str
    price_impact_pct: str


async def execute_trade(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    env: Env,
    params: ExecuteTradeParams,
) -> ExecuteTradeResult:
    input_mint = SOL_MINT if params.side == "BUY" else params.token_mint
    output_mint = params.token_mint if params.side == "BUY" else SOL_MINT

    quote = await jupiter_client.get_quote(
        http_client,
        env,
        jupiter_client.QuoteParams(
            input_mint=input_mint,
            output_mint=output_mint,
            amount=params.amount_lamports_or_tokens,
            slippage_bps=params.slippage_bps,
        ),
    )

    payload: EncryptedPayload = deserialize_encrypted(params.encrypted_wallet_key)
    secret_key_bytes = decrypt_secret_bytes(payload, env)
    try:
        signer = Keypair.from_bytes(bytes(secret_key_bytes))

        swap = await jupiter_client.get_swap_transaction(
            http_client, env, quote, str(signer.pubkey())
        )

        import base64

        unsigned_tx = VersionedTransaction.from_bytes(base64.b64decode(swap.swap_transaction))
        # solders' VersionedTransaction is immutable — there is no in-place
        # `.sign()` method (unlike solana-py's legacy Transaction). Signing
        # means constructing a new VersionedTransaction from the deserialized
        # message plus the required signers.
        tx = VersionedTransaction(unsigned_tx.message, [signer])

        connection = create_connection(env)
        try:
            # --- NEW: write SUBMITTED *before* sending, so a crash after this
            # point is recoverable by the startup reconciliation sweep instead
            # of leaving an orphaned on-chain transaction with no DB record.
            trade = await session.get(Trade, params.trade_row_id)
            if trade is None:
                raise RuntimeError(f"Trade row {params.trade_row_id} not found before submission")
            trade.status = TradeStatus.SUBMITTED
            trade.last_blockhash = str(tx.message.recent_blockhash)
            trade.last_valid_block_height = swap.last_valid_block_height
            trade.submitted_at = datetime.now(timezone.utc)
            await session.commit()

            send_resp = await connection.send_raw_transaction(bytes(tx))
            tx_signature = str(send_resp.value)

            trade.tx_signature = tx_signature
            await session.commit()

            confirm_resp = await connection.confirm_transaction(send_resp.value, commitment="confirmed")
            confirmed = bool(confirm_resp.value and confirm_resp.value[0] and not confirm_resp.value[0].err)

            trade.status = TradeStatus.CONFIRMED if confirmed else TradeStatus.FAILED
            trade.confirmed_at = datetime.now(timezone.utc) if confirmed else None
            await session.commit()

            if not confirmed:
                raise RuntimeError(f"Transaction {tx_signature} did not confirm")

            log.info(
                "Trade executed",
                tx_signature=tx_signature,
                side=params.side,
                token_mint=params.token_mint,
            )

            return ExecuteTradeResult(
                tx_signature=tx_signature,
                in_amount=quote.in_amount,
                out_amount=quote.out_amount,
                price_impact_pct=quote.price_impact_pct,
            )
        finally:
            await connection.close()
    finally:
        # Best-effort: overwrite the secret key bytes so they aren't needlessly
        # retained. See utils/security/encryption.py module docstring for why
        # this is "best-effort" rather than a hard guarantee in CPython.
        zero_bytearray(secret_key_bytes)
