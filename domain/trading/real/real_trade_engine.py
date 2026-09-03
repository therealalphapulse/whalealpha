"""
Real Wallet trade execution.

This is the single swap primitive used by manual, automated, DCA, and
Real Wallet TP/SL sells.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from infra.db.session import async_session
from models.real_trade import RealTrade
from domain.trading.real.solana_wallet import get_real_wallet, PRIORITY_FEE_TIERS
from infra.kms.wallet_crypto import decrypt_secret
from domain.trading.real import jupiter_swap
from domain.trading.real.jupiter_swap import WRAPPED_SOL_MINT, SwapError
from providers.marketdata.dexscreener import get_token_card_info

logger = logging.getLogger("AlphaPulse.RealTradeEngine")

SELL_ALREADY_IN_PROGRESS = "A sell is already in progress for this position. Please wait for it to finish."
BUY_NETWORK_RESERVE_LAMPORTS = 10_000_000
DB_FINALIZE_RETRIES = 3

# Sells claim an existing "open" RealTrade row atomically (status -> "selling"),
# which is naturally race-proof at the DB level. A buy has no pre-existing row
# to claim against, so it needs its own guard against a double-tap or a retried
# request firing two concurrent buys for the same user — same convention as
# wallet_withdraw.py's per-user lock.
_buy_locks: dict[int, asyncio.Lock] = {}


def _buy_lock(user_id: int) -> asyncio.Lock:
    lock = _buy_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _buy_locks[user_id] = lock
    return lock


def _output_decimals(quote: dict) -> int:
    """Best-effort decimals fallback from a Jupiter quote."""
    for key in ("outputDecimals", "outDecimals", "output_decimals"):
        value = quote.get(key) if isinstance(quote, dict) else None
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 0


async def get_open_real_trade_by_contract(user_id: int, contract: str) -> RealTrade | None:
    async with async_session() as session:
        result = await session.execute(select(RealTrade).where(RealTrade.user_id == user_id, RealTrade.contract == contract, RealTrade.status == "open"))
        return result.scalar_one_or_none()


async def get_open_real_trades(user_id: int) -> list[RealTrade]:
    async with async_session() as session:
        result = await session.execute(select(RealTrade).where(RealTrade.user_id == user_id, RealTrade.status == "open").order_by(RealTrade.opened_at.desc()))
        return result.scalars().all()


async def get_real_trade_history(user_id: int, limit: int = 20) -> list[RealTrade]:
    """Return recent executed RealWallet trades for this user.

    History is a record of executed trades, not only closed positions. A buy
    that succeeded today is therefore visible immediately even while its
    position remains open. A confirmed sell is also included immediately
    after finalization.
    """
    async with async_session() as session:
        result = await session.execute(
            select(RealTrade)
            .where(RealTrade.user_id == user_id, RealTrade.status != "selling")
            .order_by(
                RealTrade.closed_at.desc().nullslast(),
                RealTrade.opened_at.desc(),
                RealTrade.id.desc(),
            )
            .limit(limit)
        )
        return result.scalars().all()


async def get_real_positions_view(user_id: int) -> list[dict]:
    """Build the live view used by the Real Wallet Positions screen."""
    trades = await get_open_real_trades(user_id)
    if not trades:
        return []

    async def _price_for_trade(trade: RealTrade) -> tuple[RealTrade, float, bool]:
        try:
            info = await get_token_card_info(trade.contract)
            raw_price = info.get("price") if info else None
            price = float(raw_price)
            if price > 0:
                return trade, price, False
        except Exception as exc:
            logger.warning(
                "[RealWallet] position price refresh failed user=%s trade=%s mint=%s: %s",
                user_id, trade.id, trade.contract, exc,
            )

        fallback = float(trade.current_price or trade.entry_price or 0.0)
        return trade, fallback, True

    refreshed = await asyncio.gather(*(_price_for_trade(t) for t in trades))
    positions: list[dict] = []

    for trade, current_price, price_stale in refreshed:
        entry_price = float(trade.entry_price or 0.0)
        remaining_quantity = float(trade.remaining_quantity or 0.0)
        token_quantity = float(trade.token_quantity or 0.0)
        sol_spent = float(trade.sol_spent or 0.0)

        entry_value_sol = sol_spent * (remaining_quantity / token_quantity) if token_quantity > 0 else 0.0
        current_value_sol = entry_value_sol * (current_price / entry_price) if entry_price > 0 and entry_value_sol > 0 else 0.0
        unrealized_pnl_sol = current_value_sol - entry_value_sol
        roi_pct = (unrealized_pnl_sol / entry_value_sol * 100.0) if entry_value_sol > 0 else 0.0

        trade.current_price = current_price
        positions.append({
            "trade": trade,
            "entry_price": entry_price,
            "current_price": current_price,
            "remaining_quantity": remaining_quantity,
            "current_value_sol": current_value_sol,
            "unrealized_pnl_sol": unrealized_pnl_sol,
            "roi_pct": roi_pct,
            "price_stale": price_stale,
        })

    return positions


async def execute_real_buy(
    user_id: int,
    contract: str,
    name: str,
    symbol: str,
    current_price: float,
    sol_amount: float,
    slippage_bps: int = 150,
    priority_fee_tier: str = "auto",
    source: str = "manual",
) -> dict:
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"ok": False, "reason": "No active Real Wallet. Use /realwallet to set one up."}
    if sol_amount <= 0:
        return {"ok": False, "reason": "Amount must be greater than 0 SOL."}

    lock = _buy_lock(user_id)
    if lock.locked():
        return {"ok": False, "reason": "A buy is already in progress for this wallet. Wait for it to finish before starting another."}

    async with lock:
        lamports = int(sol_amount * 1_000_000_000)
        priority_fee_lamports = PRIORITY_FEE_TIERS.get(priority_fee_tier, "auto")
        try:
            balance_lamports = int((await jupiter_swap.get_sol_balance(wallet.public_key)) * 1_000_000_000)
        except SwapError as e:
            logger.warning("Real buy balance preflight failed for user %s: %s", user_id, e)
            return {"ok": False, "reason": "Unable to check wallet balance."}
        except Exception as e:
            logger.error("Unexpected RealWallet balance preflight error for user %s: %s", user_id, e)
            return {"ok": False, "reason": "Unable to check wallet balance."}

        required_lamports = lamports + BUY_NETWORK_RESERVE_LAMPORTS
        if isinstance(priority_fee_lamports, int):
            required_lamports += priority_fee_lamports
        if balance_lamports < required_lamports:
            return {"ok": False, "reason": "Insufficient funds."}

        try:
            quote = await jupiter_swap.get_quote(input_mint=WRAPPED_SOL_MINT, output_mint=contract, amount_lamports=lamports, slippage_bps=slippage_bps)
            tx_b64 = await jupiter_swap.build_swap_transaction(quote, wallet.public_key, priority_fee_lamports=priority_fee_lamports)
            secret_bytes = decrypt_secret(wallet.encrypted_secret, wallet.encryption_nonce)
            try:
                send_result = await jupiter_swap.sign_send_and_confirm(tx_b64, secret_bytes)
            finally:
                del secret_bytes
        except SwapError as e:
            logger.warning("Real buy failed for user %s on %s: %s", user_id, contract, e)
            return {"ok": False, "reason": str(e)}
        except Exception as e:
            logger.error("Unexpected real buy error for user %s on %s: %s", user_id, contract, e)
            return {"ok": False, "reason": "Unexpected error while executing the swap. No funds were moved if this happened before broadcast."}

        if send_result["status"] == "failed":
            return {"ok": False, "reason": f"Transaction was rejected on-chain (tx: {send_result['signature']}). No trade was recorded."}

        signature = send_result["signature"]
        try:
            decimals = await jupiter_swap.get_mint_decimals(contract)
        except SwapError:
            decimals = _output_decimals(quote)

        # The quote's outAmount is only a pre-trade estimate. Always prefer
        # the ACTUAL amount the wallet received, read from the confirmed
        # transaction's own token-balance deltas — this is what cost basis,
        # remaining_quantity, and every downstream sell/PnL calculation is
        # keyed off, so an estimate here silently corrupts all of them.
        quote_token_quantity = float(quote.get("outAmount", 0)) / (10 ** decimals)
        token_quantity = quote_token_quantity
        quantity_source = "quote_estimate"
        try:
            fill = await jupiter_swap.get_confirmed_transaction_deltas(signature, wallet.public_key, contract)
            if fill["token_delta_raw"] > 0:
                token_quantity = fill["token_delta_raw"] / (10 ** decimals)
                quantity_source = "onchain_confirmed"
            else:
                logger.warning(
                    "[RealWallet] BUY_FILL_UNVERIFIED user=%s mint=%s signature=%s token_delta_raw=%s "
                    "falling back to quote estimate=%s",
                    user_id, contract, signature, fill["token_delta_raw"], quote_token_quantity,
                )
        except SwapError as e:
            logger.warning(
                "[RealWallet] BUY_FILL_LOOKUP_FAILED user=%s mint=%s signature=%s: %s "
                "(falling back to quote estimate)",
                user_id, contract, signature, e,
            )
        logger.info(
            "[RealWallet] BUY_FILLED user=%s mint=%s signature=%s token_quantity=%s source=%s quote_estimate=%s",
            user_id, contract, signature, token_quantity, quantity_source, quote_token_quantity,
        )

        async with async_session() as session:
            trade = RealTrade(
                user_id=user_id, contract=contract, name=name, symbol=symbol,
                entry_price=current_price, current_price=current_price,
                sol_spent=sol_amount, token_quantity=token_quantity,
                remaining_quantity=token_quantity, status="open",
                tx_signature_buy=signature, slippage_bps=slippage_bps,
                token_decimals=decimals, source=source,
            )
            session.add(trade)
            await session.commit()
            await session.refresh(trade)

        return {"ok": True, "trade": trade, "signature": signature, "confirmation": send_result["status"]}


async def _finalize_sell_record(
    user_id: int,
    trade_id: int,
    current_price: float,
    signature: str,
    sold_quantity: float,
    sol_received: float,
) -> RealTrade | None:
    """Persist a confirmed sell, retrying transient DB failures.

    The blockchain transaction is irreversible. Therefore DB persistence must
    be treated as reconciliation, not as part of transaction success/failure.
    A DB outage must never turn a confirmed sell into a user-visible
    "sell failed" message and must never invite a duplicate sell.
    """
    last_error: Exception | None = None

    for attempt in range(1, DB_FINALIZE_RETRIES + 1):
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(RealTrade).where(RealTrade.id == trade_id, RealTrade.user_id == user_id)
                )
                trade = result.scalar_one_or_none()
                if not trade:
                    raise RuntimeError("Trade record disappeared after successful sell")

                remaining_before = float(trade.remaining_quantity or 0.0)
                new_remaining = max(0.0, remaining_before - sold_quantity)
                trade.current_price = current_price
                trade.exit_price = current_price
                trade.sol_received = sol_received
                trade.tx_signature_sell = signature
                trade.remaining_quantity = new_remaining
                trade.realized_pnl_sol = sol_received - (
                    float(trade.sol_spent or 0.0)
                    * (sold_quantity / float(trade.token_quantity or 1.0))
                )
                trade.pnl_pct = (
                    ((current_price / float(trade.entry_price)) - 1.0) * 100.0
                    if trade.entry_price else 0.0
                )

                if new_remaining <= 0:
                    trade.remaining_quantity = 0.0
                    trade.status = "closed_manual"
                    # real_trades.closed_at is TIMESTAMP WITHOUT TIME ZONE.
                    # Store a naive UTC datetime so asyncpg does not reject
                    # an aware datetime with "can't subtract offset-naive and
                    # offset-aware datetimes" during flush.
                    trade.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                else:
                    trade.status = "open"

                await session.commit()
                await session.refresh(trade)
                logger.info(
                    "[RealWallet] SELL_RECONCILED user=%s trade=%s mint=%s signature=%s status=%s attempt=%s",
                    user_id, trade_id, trade.contract, signature, trade.status, attempt,
                )
                return trade
        except Exception as exc:
            last_error = exc
            logger.exception(
                "[RealWallet] SELL_DB_FINALIZE_FAILED user=%s trade=%s signature=%s attempt=%s/%s: %s",
                user_id, trade_id, signature, attempt, DB_FINALIZE_RETRIES, exc,
            )
            if attempt < DB_FINALIZE_RETRIES:
                await asyncio.sleep(0.5 * attempt)

    logger.error(
        "[RealWallet] SELL_RECONCILIATION_PENDING user=%s trade=%s signature=%s error=%s",
        user_id, trade_id, signature, last_error,
    )
    return None


async def _reconcile_zero_balance(user_id: int, trade_id: int, current_price: float) -> RealTrade | None:
    """Close a position whose on-chain token balance is verified as zero
    while the ledger still shows it "open" with a nonzero remaining_quantity.

    This only runs after execute_real_sell's direct, authoritative
    getTokenAccountsByOwner check found nothing sellable on-chain for a
    trade the database still considers open. Previously this state was
    left untouched indefinitely: the position kept reporting a live cost
    basis and unrealized PnL for tokens the wallet no longer holds (most
    commonly a rug/freeze/transfer-hook token that drained the balance
    right after purchase), it permanently skewed the portfolio's realized/
    unrealized PnL, and every further sell or TP/SL attempt against it
    failed the same way with no resolution. Reconciling to verified
    on-chain truth and realizing the loss (rather than leaving a phantom
    "open" position) is the standard pattern professional systems use when
    the ledger and the chain disagree: the chain is authoritative.
    """
    for attempt in range(1, DB_FINALIZE_RETRIES + 1):
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(RealTrade).where(RealTrade.id == trade_id, RealTrade.user_id == user_id)
                )
                trade = result.scalar_one_or_none()
                if not trade:
                    return None

                token_quantity = float(trade.token_quantity or 0.0)
                remaining_quantity = float(trade.remaining_quantity or 0.0)
                lost_cost_basis_sol = (
                    float(trade.sol_spent or 0.0) * (remaining_quantity / token_quantity)
                    if token_quantity > 0 else float(trade.sol_spent or 0.0)
                )

                trade.current_price = current_price
                trade.exit_price = current_price
                trade.sol_received = 0.0
                trade.realized_pnl_sol = -lost_cost_basis_sol
                trade.pnl_pct = -100.0
                trade.remaining_quantity = 0.0
                trade.status = "closed_reconciled"
                trade.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)

                await session.commit()
                await session.refresh(trade)
                logger.warning(
                    "[RealWallet] POSITION_RECONCILED_ZERO_BALANCE user=%s trade=%s mint=%s "
                    "lost_cost_basis_sol=%s attempt=%s",
                    user_id, trade_id, trade.contract, lost_cost_basis_sol, attempt,
                )
                return trade
        except Exception as exc:
            logger.exception(
                "[RealWallet] RECONCILE_ZERO_BALANCE_DB_FAILED user=%s trade=%s attempt=%s/%s: %s",
                user_id, trade_id, attempt, DB_FINALIZE_RETRIES, exc,
            )
            if attempt < DB_FINALIZE_RETRIES:
                await asyncio.sleep(0.5 * attempt)

    logger.error(
        "[RealWallet] RECONCILE_ZERO_BALANCE_PENDING user=%s trade=%s",
        user_id, trade_id,
    )
    return None


async def execute_real_sell(
    user_id: int,
    trade_id: int,
    current_price: float,
    fraction: float = 1.0,
    slippage_bps: int = 150,
    priority_fee_tier: str = "auto",
) -> dict:
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"ok": False, "reason": "No active Real Wallet. Use /realwallet to set one up."}

    clamped_fraction = min(max(fraction, 0.0), 1.0)

    async with async_session() as session:
        claim = await session.execute(
            update(RealTrade)
            .where(RealTrade.id == trade_id, RealTrade.user_id == user_id, RealTrade.status == "open")
            .values(status="selling")
            .returning(
                RealTrade.contract,
                RealTrade.remaining_quantity,
                RealTrade.token_quantity,
                RealTrade.token_decimals,
                RealTrade.sol_spent,
            )
        )
        claimed_row = claim.first()
        await session.commit()

    if not claimed_row:
        async with async_session() as session:
            result = await session.execute(
                select(RealTrade.status).where(RealTrade.id == trade_id, RealTrade.user_id == user_id)
            )
            existing_status = result.scalar_one_or_none()
        if existing_status == "selling":
            return {"ok": False, "reason": SELL_ALREADY_IN_PROGRESS}
        return {"ok": False, "reason": "Trade not found or already closed."}

    contract, remaining_quantity, token_quantity, db_decimals, sol_spent = claimed_row

    async def _release_claim() -> None:
        async with async_session() as session:
            await session.execute(
                update(RealTrade)
                .where(RealTrade.id == trade_id, RealTrade.status == "selling")
                .values(status="open")
            )
            await session.commit()

    if remaining_quantity <= 0 or token_quantity <= 0:
        await _release_claim()
        return {"ok": False, "reason": "Nothing left to sell on this position."}

    try:
        chain_balance = await jupiter_swap.get_token_balance(wallet.public_key, contract)
    except SwapError as e:
        logger.error("Real sell on-chain balance preflight failed user=%s trade=%s mint=%s: %s", user_id, trade_id, contract, e)
        await _release_claim()
        return {"ok": False, "reason": f"Unable to verify current on-chain token balance before sell: {e}"}

    chain_raw = int(chain_balance["raw_amount"])
    chain_decimals = int(chain_balance["decimals"])
    chain_quantity = chain_raw / (10 ** chain_decimals)

    logger.info(
        "[RealWallet] SELL_PREFLIGHT user=%s trade=%s mint=%s db_remaining=%s db_decimals=%s chain_raw=%s chain_quantity=%s chain_decimals=%s fraction=%s token_accounts=%s",
        user_id, trade_id, contract, remaining_quantity, db_decimals,
        chain_raw, chain_quantity, chain_decimals, clamped_fraction,
        len(chain_balance.get("token_accounts", [])),
    )

    if chain_raw <= 0:
        reconciled_trade = await _reconcile_zero_balance(
            user_id=user_id, trade_id=trade_id, current_price=current_price,
        )
        return {
            "ok": False,
            "reason": (
                "No sellable token balance was found on-chain for this position. "
                "The position has been closed and its loss realized (on-chain balance verified zero)."
                if reconciled_trade is not None else
                "No sellable token balance was found on-chain for this position."
            ),
            "reconciled_closed": reconciled_trade is not None,
            "trade": reconciled_trade,
        }

    sell_raw_amount = int(chain_raw * clamped_fraction)
    if sell_raw_amount <= 0:
        await _release_claim()
        return {"ok": False, "reason": "Sell amount rounds to zero at the token's native precision."}

    if abs(chain_quantity - float(remaining_quantity or 0.0)) > max(1e-12, float(remaining_quantity or 0.0) * 0.01):
        logger.warning(
            "[RealWallet] SELL_BALANCE_MISMATCH user=%s trade=%s mint=%s db_remaining=%s chain_quantity=%s",
            user_id, trade_id, contract, remaining_quantity, chain_quantity,
        )

    try:
        quote = await jupiter_swap.get_quote(
            input_mint=contract,
            output_mint=WRAPPED_SOL_MINT,
            amount_lamports=sell_raw_amount,
            slippage_bps=slippage_bps,
        )
        priority_fee_lamports = PRIORITY_FEE_TIERS.get(priority_fee_tier, "auto")
        tx_b64 = await jupiter_swap.build_swap_transaction(
            quote, wallet.public_key, priority_fee_lamports=priority_fee_lamports
        )
        secret_bytes = decrypt_secret(wallet.encrypted_secret, wallet.encryption_nonce)
        try:
            send_result = await jupiter_swap.sign_send_and_confirm(tx_b64, secret_bytes)
        finally:
            del secret_bytes
    except SwapError as e:
        logger.warning("Real sell failed for user %s trade=%s on %s: %s", user_id, trade_id, contract, e)
        await _release_claim()
        return {"ok": False, "reason": str(e)}
    except Exception:
        logger.exception("Unexpected real sell error for user %s trade=%s on %s", user_id, trade_id, contract)
        await _release_claim()
        return {"ok": False, "reason": "Unexpected error while executing the sell."}

    if send_result["status"] == "failed":
        logger.warning(
            "Real sell reverted on-chain user=%s trade=%s mint=%s: %s",
            user_id, trade_id, contract, send_result["err"],
        )
        await _release_claim()
        return {"ok": False, "reason": f"Transaction was rejected on-chain (tx: {send_result['signature']})."}

    # From this point the swap is confirmed. Never report it as a failed sell
    # merely because database reconciliation has a transient problem.
    signature = send_result["signature"]
    sold_quantity = sell_raw_amount / (10 ** chain_decimals)

    # As with the buy leg: the quote's outAmount is a pre-trade estimate.
    # Realized PnL must be computed off what the wallet actually received,
    # read from the confirmed transaction's native-SOL balance delta (the
    # swap unwraps WSOL back to native SOL, so this is the real proceeds,
    # already net of the network/priority fee).
    quote_sol_received = float(quote.get("outAmount", 0)) / 1_000_000_000
    sol_received = quote_sol_received
    sol_source = "quote_estimate"
    try:
        fill = await jupiter_swap.get_confirmed_transaction_deltas(signature, wallet.public_key, contract)
        if fill["sol_delta_lamports"] > 0:
            sol_received = fill["sol_delta_lamports"] / 1_000_000_000
            sol_source = "onchain_confirmed"
        else:
            logger.warning(
                "[RealWallet] SELL_FILL_UNVERIFIED user=%s trade=%s mint=%s signature=%s "
                "sol_delta_lamports=%s falling back to quote estimate=%s",
                user_id, trade_id, contract, signature, fill["sol_delta_lamports"], quote_sol_received,
            )
    except SwapError as e:
        logger.warning(
            "[RealWallet] SELL_FILL_LOOKUP_FAILED user=%s trade=%s mint=%s signature=%s: %s "
            "(falling back to quote estimate)",
            user_id, trade_id, contract, signature, e,
        )
    logger.info(
        "[RealWallet] SELL_FILLED user=%s trade=%s mint=%s signature=%s sol_received=%s source=%s quote_estimate=%s",
        user_id, trade_id, contract, signature, sol_received, sol_source, quote_sol_received,
    )

    trade = await _finalize_sell_record(
        user_id=user_id,
        trade_id=trade_id,
        current_price=current_price,
        signature=signature,
        sold_quantity=sold_quantity,
        sol_received=sol_received,
    )

    if trade is None:
        return {
            "ok": True,
            "persistence_pending": True,
            "signature": signature,
            "confirmation": send_result["status"],
            "sol_received": sol_received,
            "trade": None,
            "reason": "Sell confirmed on-chain, but trade-history synchronization is pending.",
        }

    return {
        "ok": True,
        "trade": trade,
        "signature": signature,
        "confirmation": send_result["status"],
        "sol_received": sol_received,
        "persistence_pending": False,
    }
