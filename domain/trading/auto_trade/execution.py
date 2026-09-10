"""domain/trading/auto_trade/execution.py

§11 (Buy Execution Engine), §12 (Transaction Confirmation), §13
(Critical Retry Rule), §23 (Sell Execution), §24 (Sell Retry Logic).

Thin orchestration layer over domain/trading/real/robinhood_swap.py's
existing quote/build/sign/send/confirm/on-chain-delta primitives --
deliberately NOT over real_trade_engine.py, so this stays isolated from
models/real_trade.py while reusing the shared, battle-tested execution
primitives (§44/§45). Every function returns a SwapResult-shaped dict
with an explicit `uncertain` flag distinguishing "definitely did not
happen, safe to retry" from "outcome unknown, must reconcile before
retrying" (§13/§24). This module never touches the DB.
"""

from __future__ import annotations

import logging

from domain.trading.real import robinhood_swap as robinhood_swap
from domain.trading.real.robinhood_swap import NATIVE_ETH_ADDRESS, SwapError
from domain.trading.real.robinhood_wallet import get_real_wallet, PRIORITY_FEE_TIERS
from infra.kms.wallet_crypto import decrypt_secret

logger = logging.getLogger("AlphaPulse.AutoTrade.Execution")


def _output_decimals(quote: dict) -> int:
    for key in ("outputDecimals", "outDecimals", "output_decimals"):
        value = quote.get(key) if isinstance(quote, dict) else None
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 0


async def execute_buy_swap(
    user_id: int,
    contract: str,
    sol_amount: float,
    slippage_bps: int = 150,
    priority_fee_tier: str = "auto",
) -> dict:
    """ETH -> token buy."""
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"ok": False, "uncertain": False, "reason": "No active wallet."}
    if sol_amount <= 0:
        return {"ok": False, "uncertain": False, "reason": "Amount must be greater than 0 ETH."}

    wei = int(sol_amount * 1_000_000_000_000_000_000)
    priority_fee_wei = PRIORITY_FEE_TIERS.get(priority_fee_tier, "auto")

    try:
        quote = await robinhood_swap.get_quote(
            input_mint=NATIVE_ETH_ADDRESS, output_mint=contract, amount_wei=wei, slippage_bps=slippage_bps
        )
        tx_b64 = await robinhood_swap.build_swap_transaction(quote, wallet.public_key, priority_fee_wei=priority_fee_wei)
    except SwapError as e:
        logger.warning("[AutoTrade] buy quote/build failed user=%s contract=%s: %s", user_id, contract, e)
        return {"ok": False, "uncertain": False, "reason": str(e)}
    except Exception as e:
        logger.error("[AutoTrade] unexpected buy quote/build error user=%s contract=%s: %s", user_id, contract, e)
        return {"ok": False, "uncertain": False, "reason": "Unexpected error preparing the swap. No funds were moved."}

    secret_bytes = decrypt_secret(wallet.encrypted_secret, wallet.encryption_nonce)
    try:
        send_result = await robinhood_swap.sign_send_and_confirm(tx_b64, secret_bytes)
    except SwapError as e:
        logger.warning("[AutoTrade] buy sign/send failed user=%s contract=%s: %s", user_id, contract, e)
        return {"ok": False, "uncertain": True, "reason": str(e)}
    except Exception as e:
        logger.error("[AutoTrade] unexpected buy sign/send error user=%s contract=%s: %s", user_id, contract, e)
        return {"ok": False, "uncertain": True, "reason": "Unexpected error while executing the swap. Outcome is not confirmed."}
    finally:
        del secret_bytes

    if send_result["status"] == "failed":
        return {
            "ok": False, "uncertain": False,
            "reason": f"Transaction rejected on-chain (tx: {send_result['signature']}).",
            "signature": send_result["signature"],
        }

    signature = send_result["signature"]
    try:
        decimals = await robinhood_swap.get_mint_decimals(contract)
    except SwapError:
        decimals = _output_decimals(quote)

    quote_token_quantity = float(quote.get("outAmount", 0)) / (10 ** decimals) if decimals else 0.0
    token_quantity = quote_token_quantity
    quantity_source = "quote_estimate"
    onchain_fill_confirmed = False
    try:
        fill = await robinhood_swap.get_confirmed_transaction_deltas(signature, wallet.public_key, contract)
        if fill["token_delta_raw"] > 0:
            token_quantity = fill["token_delta_raw"] / (10 ** decimals)
            quantity_source = "onchain_confirmed"
            onchain_fill_confirmed = True
    except SwapError as e:
        logger.warning("[AutoTrade] buy fill lookup failed user=%s contract=%s sig=%s: %s", user_id, contract, signature, e)

    if send_result["status"] != "confirmed" and not onchain_fill_confirmed:
        return {
            "ok": False, "uncertain": True, "signature": signature,
            "reason": f"Buy submitted but not confirmed on-chain (tx: {signature}).",
        }

    effective_entry_price = (sol_amount / token_quantity) if token_quantity > 0 else None

    return {
        "ok": True, "uncertain": False,
        "signature": signature,
        "token_quantity": token_quantity,
        "decimals": decimals,
        "quantity_source": quantity_source,
        "confirmation": send_result["status"],
        "effective_entry_price": effective_entry_price,
        "sol_spent": sol_amount,
        "quote": quote,
    }


async def execute_sell_swap(
    user_id: int,
    contract: str,
    token_amount: float,
    decimals: int,
    slippage_bps: int = 150,
    priority_fee_tier: str = "auto",
) -> dict:
    """Token -> ETH sell for `token_amount` (UI units, not raw)."""
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"ok": False, "uncertain": False, "reason": "No active wallet."}
    if token_amount <= 0:
        return {"ok": False, "uncertain": False, "reason": "Nothing to sell."}

    raw_amount = int(token_amount * (10 ** decimals))
    priority_fee_wei = PRIORITY_FEE_TIERS.get(priority_fee_tier, "auto")

    try:
        quote = await robinhood_swap.get_quote(
            input_mint=contract, output_mint=NATIVE_ETH_ADDRESS, amount_wei=raw_amount, slippage_bps=slippage_bps
        )
        tx_b64 = await robinhood_swap.build_swap_transaction(quote, wallet.public_key, priority_fee_wei=priority_fee_wei)
    except SwapError as e:
        logger.warning("[AutoTrade] sell quote/build failed user=%s contract=%s: %s", user_id, contract, e)
        return {"ok": False, "uncertain": False, "reason": str(e)}
    except Exception as e:
        logger.error("[AutoTrade] unexpected sell quote/build error user=%s contract=%s: %s", user_id, contract, e)
        return {"ok": False, "uncertain": False, "reason": "Unexpected error preparing the sell. No tokens were moved."}

    secret_bytes = decrypt_secret(wallet.encrypted_secret, wallet.encryption_nonce)
    try:
        send_result = await robinhood_swap.sign_send_and_confirm(tx_b64, secret_bytes)
    except SwapError as e:
        logger.warning("[AutoTrade] sell sign/send failed user=%s contract=%s: %s", user_id, contract, e)
        return {"ok": False, "uncertain": True, "reason": str(e)}
    except Exception as e:
        logger.error("[AutoTrade] unexpected sell sign/send error user=%s contract=%s: %s", user_id, contract, e)
        return {"ok": False, "uncertain": True, "reason": "Unexpected error while executing the sell. Outcome is not confirmed."}
    finally:
        del secret_bytes

    if send_result["status"] == "failed":
        return {
            "ok": False, "uncertain": False,
            "reason": f"Sell transaction rejected on-chain (tx: {send_result['signature']}).",
            "signature": send_result["signature"],
        }

    signature = send_result["signature"]
    sol_received = None
    actual_amount_sold = token_amount
    onchain_confirmed = False
    try:
        fill = await robinhood_swap.get_confirmed_transaction_deltas(signature, wallet.public_key, contract)
        if fill["sol_delta_wei"] and fill["sol_delta_wei"] > 0:
            sol_received = fill["sol_delta_wei"] / 1_000_000_000_000_000_000
            onchain_confirmed = True
        if fill["token_delta_raw"] and fill["token_delta_raw"] < 0:
            actual_amount_sold = abs(fill["token_delta_raw"]) / (10 ** decimals)
    except SwapError as e:
        logger.warning("[AutoTrade] sell fill lookup failed user=%s contract=%s sig=%s: %s", user_id, contract, signature, e)

    if send_result["status"] != "confirmed" and not onchain_confirmed:
        return {
            "ok": False, "uncertain": True, "signature": signature,
            "reason": f"Sell submitted but not confirmed on-chain (tx: {signature}).",
        }

    if sol_received is None:
        sol_received = float(quote.get("outAmount", 0)) / 1_000_000_000_000_000_000

    return {
        "ok": True, "uncertain": False,
        "signature": signature,
        "sol_received": sol_received,
        "actual_amount_sold": actual_amount_sold,
        "confirmation": send_result["status"],
        "quote": quote,
    }
