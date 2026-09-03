"""
Real Wallet creation, import, and lifecycle management.

Handles turning a brand-new or user-supplied Solana keypair into an
encrypted RealWallet row and the reverse operations.
"""

import base58
import logging
from datetime import datetime, timezone
from sqlalchemy import select, text

from solders.keypair import Keypair

from infra.db.session import async_session, engine
from models.real_wallet import RealWallet
from infra.kms.wallet_crypto import encrypt_secret, decrypt_secret

logger = logging.getLogger("AlphaPulse.SolanaWallet")

PRIORITY_FEE_TIERS = {"auto": "auto", "fast": 100_000, "turbo": 500_000}
DEFAULT_PRIORITY_TIER = "auto"
DEFAULT_SLIPPAGE_BPS = 150
SLIPPAGE_PRESETS_BPS = [50, 100, 150, 300, 500]

DEFAULT_AUTO_DAILY_CAP_SOL = 1.0
AUTO_DAILY_CAP_PRESETS_SOL = [0.5, 1.0, 2.0, 5.0, 10.0]
DEFAULT_DAILY_AUTO_BUY_LIMIT = 5


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class WalletImportError(ValueError):
    """Raised when a user-supplied private key can't be parsed/validated."""


async def get_real_wallet(user_id: int) -> RealWallet | None:
    async with async_session() as session:
        result = await session.execute(select(RealWallet).where(RealWallet.user_id == user_id, RealWallet.is_active == True))
        return result.scalar_one_or_none()


async def create_wallet(user_id: int) -> RealWallet:
    existing = await get_real_wallet(user_id)
    if existing:
        raise WalletImportError("You already have an active Real Wallet. Disconnect it first if you want to create a new one.")
    keypair = Keypair()
    secret_bytes = bytes(keypair)
    public_key = str(keypair.pubkey())
    encrypted_secret, nonce = encrypt_secret(secret_bytes)
    async with async_session() as session:
        wallet = RealWallet(user_id=user_id, public_key=public_key, encrypted_secret=encrypted_secret, encryption_nonce=nonce, source="created")
        session.add(wallet)
        await session.commit()
        await session.refresh(wallet)
        logger.info(f"Created real wallet for user {user_id}: {public_key}")
        return wallet


def _parse_private_key_input(raw: str) -> bytes:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parts = [int(x.strip()) for x in raw[1:-1].split(",") if x.strip()]
        except ValueError:
            raise WalletImportError("That doesn't look like a valid key array.")
        if len(parts) != 64:
            raise WalletImportError("Key array must contain exactly 64 numbers.")
        return bytes(parts)
    try:
        decoded = base58.b58decode(raw)
    except Exception:
        raise WalletImportError("Couldn't read that key. Paste your base58 private key exactly as exported from your wallet app, with nothing else in the message.")
    if len(decoded) not in (64, 32):
        raise WalletImportError(f"Decoded key is {len(decoded)} bytes; expected 32 or 64. Make sure you copied the private key, not the public address.")
    return decoded


async def import_wallet(user_id: int, raw_private_key: str) -> RealWallet:
    existing = await get_real_wallet(user_id)
    if existing:
        raise WalletImportError("You already have an active Real Wallet. Disconnect it first if you want to import a different one.")
    secret_bytes = _parse_private_key_input(raw_private_key)
    try:
        keypair = Keypair.from_seed(secret_bytes) if len(secret_bytes) == 32 else Keypair.from_bytes(secret_bytes)
    except Exception:
        raise WalletImportError("That key couldn't be loaded as a valid Solana keypair.")
    public_key = str(keypair.pubkey())
    encrypted_secret, nonce = encrypt_secret(bytes(keypair))
    async with async_session() as session:
        wallet = RealWallet(user_id=user_id, public_key=public_key, encrypted_secret=encrypted_secret, encryption_nonce=nonce, source="imported")
        session.add(wallet)
        await session.commit()
        await session.refresh(wallet)
        logger.info(f"Imported real wallet for user {user_id}: {public_key}")
        return wallet


async def export_wallet_secret(user_id: int) -> str:
    wallet = await get_real_wallet(user_id)
    if not wallet:
        raise WalletImportError("No active Real Wallet found.")
    secret_bytes = decrypt_secret(wallet.encrypted_secret, wallet.encryption_nonce)
    return base58.b58encode(secret_bytes).decode()


async def disconnect_wallet(user_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(select(RealWallet).where(RealWallet.user_id == user_id, RealWallet.is_active == True))
        wallet = result.scalar_one_or_none()
        if not wallet:
            return False
        await session.delete(wallet)
        await session.commit()
        logger.info(f"Disconnected real wallet for user {user_id}")
        return True


async def set_auto_trading(user_id: int, enabled: bool) -> bool:
    async with async_session() as session:
        result = await session.execute(select(RealWallet).where(RealWallet.user_id == user_id, RealWallet.is_active == True))
        wallet = result.scalar_one_or_none()
        if not wallet:
            return False
        wallet.auto_trading_enabled = enabled
        await session.commit()
        return True


async def migrate_real_wallet_schema() -> None:
    """Idempotent production migration for Real Wallet automation settings."""
    statements = [
        f"ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS slippage_bps INTEGER DEFAULT {DEFAULT_SLIPPAGE_BPS}",
        f"ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS priority_fee_tier VARCHAR DEFAULT '{DEFAULT_PRIORITY_TIER}'",
        f"ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_max_daily_spend_sol FLOAT DEFAULT {DEFAULT_AUTO_DAILY_CAP_SOL}",
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_daily_spent_sol FLOAT DEFAULT 0.0",
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_daily_spent_date VARCHAR",
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_daily_buy_count BIGINT DEFAULT 0",
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_daily_buy_count_date VARCHAR",
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_kill_switch BOOLEAN DEFAULT FALSE",
        "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'manual'",
        "ALTER TABLE real_autobuy_filters ADD COLUMN IF NOT EXISTS auto_buy_amount_usdt FLOAT",
        "ALTER TABLE real_autobuy_filters ADD COLUMN IF NOT EXISTS take_profit_pct FLOAT",
        "ALTER TABLE real_autobuy_filters ADD COLUMN IF NOT EXISTS stop_loss_pct FLOAT",
        f"ALTER TABLE real_autobuy_filters ADD COLUMN IF NOT EXISTS daily_auto_buy_limit BIGINT DEFAULT {DEFAULT_DAILY_AUTO_BUY_LIMIT}",
        "ALTER TABLE real_autobuy_filters ADD COLUMN IF NOT EXISTS auto_buy_signal_source VARCHAR DEFAULT 'both'",
    ]
    try:
        async with engine.begin() as conn:
            for stmt in statements:
                await conn.execute(text(stmt))
        logger.info("Real wallet schema migration complete")
    except Exception as e:
        logger.error(f"Real wallet schema migration error (non-fatal): {e}")


async def get_wallet_settings(user_id: int) -> dict:
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"slippage_bps": DEFAULT_SLIPPAGE_BPS, "priority_fee_tier": DEFAULT_PRIORITY_TIER}
    return {"slippage_bps": wallet.slippage_bps or DEFAULT_SLIPPAGE_BPS, "priority_fee_tier": wallet.priority_fee_tier or DEFAULT_PRIORITY_TIER}


async def set_wallet_slippage(user_id: int, slippage_bps: int) -> bool:
    async with async_session() as session:
        result = await session.execute(select(RealWallet).where(RealWallet.user_id == user_id, RealWallet.is_active == True))
        wallet = result.scalar_one_or_none()
        if not wallet:
            return False
        wallet.slippage_bps = slippage_bps
        await session.commit()
        return True


async def set_wallet_priority_tier(user_id: int, tier: str) -> bool:
    if tier not in PRIORITY_FEE_TIERS:
        return False
    async with async_session() as session:
        result = await session.execute(select(RealWallet).where(RealWallet.user_id == user_id, RealWallet.is_active == True))
        wallet = result.scalar_one_or_none()
        if not wallet:
            return False
        wallet.priority_fee_tier = tier
        await session.commit()
        return True


async def get_automation_status(user_id: int) -> dict | None:
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return None
    spent_today = wallet.auto_daily_spent_sol or 0.0
    if wallet.auto_daily_spent_date != _today_str():
        spent_today = 0.0
    cap = wallet.auto_max_daily_spend_sol if wallet.auto_max_daily_spend_sol is not None else DEFAULT_AUTO_DAILY_CAP_SOL
    buy_count = wallet.auto_daily_buy_count or 0
    if wallet.auto_daily_buy_count_date != _today_str():
        buy_count = 0
    return {
        "auto_trading_enabled": bool(wallet.auto_trading_enabled),
        "kill_switch": bool(wallet.auto_kill_switch),
        "daily_cap_sol": cap,
        "spent_today_sol": spent_today,
        "remaining_today_sol": max(0.0, cap - spent_today),
        "daily_auto_buy_count": buy_count,
    }


async def set_auto_kill_switch(user_id: int, enabled: bool) -> bool:
    async with async_session() as session:
        result = await session.execute(select(RealWallet).where(RealWallet.user_id == user_id, RealWallet.is_active == True))
        wallet = result.scalar_one_or_none()
        if not wallet:
            return False
        wallet.auto_kill_switch = enabled
        await session.commit()
        return True


async def set_auto_daily_cap(user_id: int, cap_sol: float) -> bool:
    if cap_sol <= 0:
        return False
    async with async_session() as session:
        result = await session.execute(select(RealWallet).where(RealWallet.user_id == user_id, RealWallet.is_active == True))
        wallet = result.scalar_one_or_none()
        if not wallet:
            return False
        wallet.auto_max_daily_spend_sol = cap_sol
        await session.commit()
        return True


async def register_auto_spend(user_id: int, sol_amount: float) -> dict:
    today = _today_str()
    async with async_session() as session:
        result = await session.execute(text("""
            UPDATE real_wallets
            SET auto_daily_spent_sol = CASE WHEN auto_daily_spent_date = :today THEN COALESCE(auto_daily_spent_sol, 0.0) + :amount ELSE :amount END,
                auto_daily_spent_date = :today
            WHERE user_id = :user_id AND is_active = true AND auto_kill_switch = false AND auto_trading_enabled = true
              AND (CASE WHEN auto_daily_spent_date = :today THEN COALESCE(auto_daily_spent_sol, 0.0) ELSE 0.0 END + :amount) <= COALESCE(auto_max_daily_spend_sol, :default_cap)
            RETURNING id
        """), {"today": today, "amount": sol_amount, "user_id": user_id, "default_cap": DEFAULT_AUTO_DAILY_CAP_SOL})
        reserved = result.first()
        await session.commit()
    if reserved:
        return {"ok": True}
    wallet = await get_real_wallet(user_id)
    if not wallet:
        return {"ok": False, "reason": "No active Real Wallet."}
    if wallet.auto_kill_switch:
        return {"ok": False, "reason": "Automation kill switch is ON for this wallet."}
    if not wallet.auto_trading_enabled:
        return {"ok": False, "reason": "Automation is OFF for this wallet."}
    spent_today = wallet.auto_daily_spent_sol or 0.0
    if wallet.auto_daily_spent_date != today:
        spent_today = 0.0
    cap = wallet.auto_max_daily_spend_sol if wallet.auto_max_daily_spend_sol is not None else DEFAULT_AUTO_DAILY_CAP_SOL
    return {"ok": False, "reason": f"Daily automated-spend cap reached ({spent_today:.4f}/{cap:.4f} SOL used today)."}


async def release_auto_spend(user_id: int, sol_amount: float) -> None:
    today = _today_str()
    async with async_session() as session:
        await session.execute(text("""
            UPDATE real_wallets SET auto_daily_spent_sol = GREATEST(0.0, COALESCE(auto_daily_spent_sol, 0.0) - :amount)
            WHERE user_id = :user_id AND is_active = true AND auto_daily_spent_date = :today
        """), {"user_id": user_id, "amount": sol_amount, "today": today})
        await session.commit()


async def register_auto_buy(user_id: int, limit: int) -> dict:
    """Atomically reserve one signal-driven auto-buy slot for this UTC day."""
    if not 1 <= int(limit) <= 20:
        return {"ok": False, "reason": "Daily auto-buy limit must be between 1 and 20."}
    today = _today_str()
    async with async_session() as session:
        result = await session.execute(text("""
            UPDATE real_wallets
            SET auto_daily_buy_count = CASE WHEN auto_daily_buy_count_date = :today THEN COALESCE(auto_daily_buy_count, 0) + 1 ELSE 1 END,
                auto_daily_buy_count_date = :today
            WHERE user_id = :user_id AND is_active = true AND auto_kill_switch = false AND auto_trading_enabled = true
              AND (CASE WHEN auto_daily_buy_count_date = :today THEN COALESCE(auto_daily_buy_count, 0) ELSE 0 END) < :limit
            RETURNING id
        """), {"today": today, "user_id": user_id, "limit": int(limit)})
        reserved = result.first()
        await session.commit()
    if reserved:
        return {"ok": True}
    return {"ok": False, "reason": f"Daily auto-buy limit reached ({int(limit)} buys today)."}


async def release_auto_buy(user_id: int) -> None:
    """Refund one auto-buy slot when the reserved swap fails."""
    today = _today_str()
    async with async_session() as session:
        await session.execute(text("""
            UPDATE real_wallets
            SET auto_daily_buy_count = GREATEST(0, COALESCE(auto_daily_buy_count, 0) - 1)
            WHERE user_id = :user_id AND is_active = true AND auto_daily_buy_count_date = :today
        """), {"user_id": user_id, "today": today})
        await session.commit()
