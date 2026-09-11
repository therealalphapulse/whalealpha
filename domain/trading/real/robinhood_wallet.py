"""Robinhood Chain EVM wallet lifecycle and automation safety state."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from eth_account import Account
from sqlalchemy import select, text, and_, not_
from infra.db.session import async_session, engine
from models.real_wallet import RealWallet
from infra.kms.wallet_crypto import encrypt_secret, decrypt_secret
from config.settings import ROBINHOOD_EVM_CHAIN_ID

logger = logging.getLogger("WhaleAlpha.RobinhoodWallet")
DEFAULT_PRIORITY_TIER = "auto"
PRIORITY_FEE_TIERS = {"auto": "auto", "fast": 2, "turbo": 5}
DEFAULT_SLIPPAGE_BPS = 150
SLIPPAGE_PRESETS_BPS = [50, 100, 150, 300, 500]
DEFAULT_AUTO_DAILY_CAP_ETH = 1.0
AUTO_DAILY_CAP_PRESETS_ETH = [0.05, 0.1, 0.25, 0.5, 1.0]
# Compatibility aliases for existing engine field names. Values are ETH on Robinhood Chain.
DEFAULT_AUTO_DAILY_CAP_SOL = DEFAULT_AUTO_DAILY_CAP_ETH
AUTO_DAILY_CAP_PRESETS_SOL = AUTO_DAILY_CAP_PRESETS_ETH

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

class WalletImportError(ValueError):
    pass

async def get_real_wallet(user_id: int) -> RealWallet | None:
    async with async_session() as session:
        result = await session.execute(select(RealWallet).where(RealWallet.user_id == user_id, RealWallet.is_active == True, RealWallet.chain_id == ROBINHOOD_EVM_CHAIN_ID))
        return result.scalar_one_or_none()

async def _clear_stale_wallet_row(user_id: int) -> None:
    """real_wallets.user_id is UNIQUE (models/real_wallet.py) -- one row
    per user, period, regardless of chain_id/is_active. migrate_real_wallet_schema()
    deactivates (is_active=False) any pre-Robinhood-migration row for the
    wrong chain, but never deletes it, so a user who had a wallet before
    this chain migration still has that dead row sitting in the table.
    create_wallet()/import_wallet() always INSERT a new row rather than
    reusing one, so for that user every future create/import hits the
    user_id UNIQUE constraint at the DB layer -- surfacing to the person
    as a generic 'something went wrong importing that key' error, with
    the real IntegrityError only visible in the logs.

    Deletes any row for this user_id that is NOT the current active
    Robinhood Chain wallet. Never touches an active Robinhood Chain
    wallet -- that case is already caught earlier by create_wallet()'s /
    import_wallet()'s own "you already have an active wallet" check, so
    by the time this runs there is nothing left to preserve.
    """
    async with async_session() as session:
        result = await session.execute(
            select(RealWallet).where(
                RealWallet.user_id == user_id,
                not_(and_(RealWallet.is_active == True, RealWallet.chain_id == ROBINHOOD_EVM_CHAIN_ID)),
            )
        )
        stale_rows = result.scalars().all()
        if not stale_rows:
            return
        for row in stale_rows:
            await session.delete(row)
        await session.commit()
        logger.info("Cleared %d stale real_wallets row(s) for user=%s before create/import", len(stale_rows), user_id)


async def create_wallet(user_id: int) -> RealWallet:
    existing = await get_real_wallet(user_id)
    if existing:
        raise WalletImportError("You already have an active Robinhood Chain wallet. Disconnect it first if you want to create a new one.")
    await _clear_stale_wallet_row(user_id)
    account = Account.create()
    secret_bytes = bytes(account.key)
    encrypted_secret, nonce = encrypt_secret(secret_bytes)
    async with async_session() as session:
        wallet = RealWallet(user_id=user_id, public_key=account.address, encrypted_secret=encrypted_secret, encryption_nonce=nonce, source="created", chain_id=ROBINHOOD_EVM_CHAIN_ID, network="robinhood")
        session.add(wallet)
        await session.commit()
        await session.refresh(wallet)
        logger.info("Created Robinhood Chain wallet user=%s address=%s", user_id, account.address)
        return wallet

def _parse_private_key_input(raw: str) -> bytes:
    value = raw.strip()
    if value.startswith("0x"):
        value = value[2:]
    if len(value) != 64:
        raise WalletImportError("Invalid private key. Send the 64-hex-character EVM private key (with or without 0x).")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise WalletImportError("Invalid private key. Expected hexadecimal EVM private key.") from exc

async def import_wallet(user_id: int, raw_private_key: str) -> RealWallet:
    existing = await get_real_wallet(user_id)
    if existing:
        raise WalletImportError("You already have an active Robinhood Chain wallet. Disconnect it first if you want to import a different one.")
    secret_bytes = _parse_private_key_input(raw_private_key)
    await _clear_stale_wallet_row(user_id)
    try:
        account = Account.from_key(secret_bytes)
    except Exception as exc:
        raise WalletImportError("That key is not a valid EVM private key.") from exc
    encrypted_secret, nonce = encrypt_secret(secret_bytes)
    async with async_session() as session:
        wallet = RealWallet(user_id=user_id, public_key=account.address, encrypted_secret=encrypted_secret, encryption_nonce=nonce, source="imported", chain_id=ROBINHOOD_EVM_CHAIN_ID, network="robinhood")
        session.add(wallet)
        await session.commit()
        await session.refresh(wallet)
        logger.info("Imported Robinhood Chain wallet user=%s address=%s", user_id, account.address)
        return wallet

async def export_wallet_secret(user_id: int) -> str:
    wallet = await get_real_wallet(user_id)
    if not wallet:
        raise WalletImportError("No active Robinhood Chain wallet found.")
    secret = decrypt_secret(wallet.encrypted_secret, wallet.encryption_nonce)
    return "0x" + secret.hex()

async def disconnect_wallet(user_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(select(RealWallet).where(RealWallet.user_id == user_id, RealWallet.is_active == True))
        wallet = result.scalar_one_or_none()
        if not wallet:
            return False
        wallet.is_active = False
        await session.commit()
        logger.info("Disconnected wallet user=%s", user_id)
        return True

async def set_auto_trading(user_id: int, enabled: bool) -> bool:
    async with async_session() as session:
        result = await session.execute(select(RealWallet).where(RealWallet.user_id == user_id, RealWallet.is_active == True, RealWallet.chain_id == ROBINHOOD_EVM_CHAIN_ID))
        wallet = result.scalar_one_or_none()
        if not wallet: return False
        wallet.auto_trading_enabled = enabled
        await session.commit(); return True

async def migrate_real_wallet_schema() -> None:
    statements = [
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS chain_id INTEGER",
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS network VARCHAR",
        f"ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS slippage_bps INTEGER DEFAULT {DEFAULT_SLIPPAGE_BPS}",
        f"ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS priority_fee_tier VARCHAR DEFAULT '{DEFAULT_PRIORITY_TIER}'",
        f"ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_max_daily_spend_sol FLOAT DEFAULT {DEFAULT_AUTO_DAILY_CAP_ETH}",
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_daily_spent_sol FLOAT DEFAULT 0.0",
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_daily_spent_date VARCHAR",
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_daily_buy_count BIGINT DEFAULT 0",
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_daily_buy_count_date VARCHAR",
        "ALTER TABLE real_wallets ADD COLUMN IF NOT EXISTS auto_kill_switch BOOLEAN DEFAULT FALSE",
        "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'manual'",
        "ALTER TABLE real_autobuy_filters ADD COLUMN IF NOT EXISTS auto_buy_amount_usdt FLOAT",
        "ALTER TABLE real_autobuy_filters ADD COLUMN IF NOT EXISTS take_profit_pct FLOAT",
        "ALTER TABLE real_autobuy_filters ADD COLUMN IF NOT EXISTS stop_loss_pct FLOAT",
        "ALTER TABLE real_autobuy_filters ADD COLUMN IF NOT EXISTS daily_auto_buy_limit BIGINT DEFAULT 5",
        "ALTER TABLE real_autobuy_filters ADD COLUMN IF NOT EXISTS auto_buy_signal_source VARCHAR DEFAULT 'both'",
        "UPDATE real_wallets SET is_active = FALSE WHERE is_active = TRUE AND (chain_id IS NULL OR chain_id <> 4663)",
    ]
    try:
        async with engine.begin() as conn:
            for stmt in statements: await conn.execute(text(stmt))
        logger.info("Robinhood Chain wallet schema migration complete")
    except Exception as e:
        logger.error("Robinhood wallet schema migration error: %s", e)

async def get_wallet_settings(user_id: int) -> dict:
    wallet = await get_real_wallet(user_id)
    if not wallet: return {"slippage_bps": DEFAULT_SLIPPAGE_BPS, "priority_fee_tier": DEFAULT_PRIORITY_TIER}
    return {"slippage_bps": wallet.slippage_bps or DEFAULT_SLIPPAGE_BPS, "priority_fee_tier": wallet.priority_fee_tier or DEFAULT_PRIORITY_TIER}

async def set_wallet_slippage(user_id: int, slippage_bps: int) -> bool:
    async with async_session() as session:
        result = await session.execute(select(RealWallet).where(RealWallet.user_id == user_id, RealWallet.is_active == True, RealWallet.chain_id == ROBINHOOD_EVM_CHAIN_ID)); wallet=result.scalar_one_or_none()
        if not wallet: return False
        wallet.slippage_bps=slippage_bps; await session.commit(); return True

async def set_wallet_priority_tier(user_id: int, tier: str) -> bool:
    if tier not in PRIORITY_FEE_TIERS: return False
    async with async_session() as session:
        result=await session.execute(select(RealWallet).where(RealWallet.user_id==user_id, RealWallet.is_active==True, RealWallet.chain_id==ROBINHOOD_EVM_CHAIN_ID)); wallet=result.scalar_one_or_none()
        if not wallet: return False
        wallet.priority_fee_tier=tier; await session.commit(); return True

async def get_automation_status(user_id: int) -> dict | None:
    wallet=await get_real_wallet(user_id)
    if not wallet: return None
    spent=wallet.auto_daily_spent_sol or 0.0
    if wallet.auto_daily_spent_date != _today_str(): spent=0.0
    cap=wallet.auto_max_daily_spend_sol if wallet.auto_max_daily_spend_sol is not None else DEFAULT_AUTO_DAILY_CAP_ETH
    count=wallet.auto_daily_buy_count or 0
    if wallet.auto_daily_buy_count_date != _today_str(): count=0
    return {"auto_trading_enabled":bool(wallet.auto_trading_enabled),"kill_switch":bool(wallet.auto_kill_switch),"daily_cap_sol":cap,"spent_today_sol":spent,"remaining_today_sol":max(0.0,cap-spent),"daily_auto_buy_count":count}

async def set_auto_kill_switch(user_id:int, enabled:bool)->bool:
    async with async_session() as session:
        result=await session.execute(select(RealWallet).where(RealWallet.user_id==user_id,RealWallet.is_active==True,RealWallet.chain_id==ROBINHOOD_EVM_CHAIN_ID)); wallet=result.scalar_one_or_none()
        if not wallet:return False
        wallet.auto_kill_switch=enabled; await session.commit(); return True

async def set_auto_daily_cap(user_id:int, cap_sol:float)->bool:
    if cap_sol<=0:return False
    async with async_session() as session:
        result=await session.execute(select(RealWallet).where(RealWallet.user_id==user_id,RealWallet.is_active==True,RealWallet.chain_id==ROBINHOOD_EVM_CHAIN_ID)); wallet=result.scalar_one_or_none()
        if not wallet:return False
        wallet.auto_max_daily_spend_sol=cap_sol; await session.commit(); return True

async def register_auto_spend(user_id:int, sol_amount:float)->dict:
    today=_today_str()
    async with async_session() as session:
        result=await session.execute(text("""UPDATE real_wallets SET auto_daily_spent_sol=CASE WHEN auto_daily_spent_date=:today THEN COALESCE(auto_daily_spent_sol,0)+:amount ELSE :amount END, auto_daily_spent_date=:today WHERE user_id=:user_id AND is_active=true AND chain_id=:chain_id AND auto_kill_switch=false AND auto_trading_enabled=true AND (CASE WHEN auto_daily_spent_date=:today THEN COALESCE(auto_daily_spent_sol,0) ELSE 0 END + :amount) <= COALESCE(auto_max_daily_spend_sol,:default_cap) RETURNING id"""),{"today":today,"amount":sol_amount,"user_id":user_id,"chain_id":ROBINHOOD_EVM_CHAIN_ID,"default_cap":DEFAULT_AUTO_DAILY_CAP_ETH})
        reserved=result.first(); await session.commit()
    if reserved:return {"ok":True}
    wallet=await get_real_wallet(user_id)
    if not wallet:return {"ok":False,"reason":"No active Robinhood Chain wallet."}
    if wallet.auto_kill_switch:return {"ok":False,"reason":"Automation kill switch is ON for this wallet."}
    if not wallet.auto_trading_enabled:return {"ok":False,"reason":"Automation is OFF for this wallet."}
    spent=wallet.auto_daily_spent_sol or 0.0
    if wallet.auto_daily_spent_date!=today:spent=0.0
    cap=wallet.auto_max_daily_spend_sol if wallet.auto_max_daily_spend_sol is not None else DEFAULT_AUTO_DAILY_CAP_ETH
    return {"ok":False,"reason":f"Daily automated-spend cap reached ({spent:.4f}/{cap:.4f} ETH used today)."}

async def release_auto_spend(user_id:int, sol_amount:float)->None:
    async with async_session() as session:
        await session.execute(text("UPDATE real_wallets SET auto_daily_spent_sol=GREATEST(0.0,COALESCE(auto_daily_spent_sol,0)-:amount) WHERE user_id=:user_id AND is_active=true AND chain_id=:chain_id AND auto_daily_spent_date=:today"),{"user_id":user_id,"chain_id":ROBINHOOD_EVM_CHAIN_ID,"amount":sol_amount,"today":_today_str()}); await session.commit()

async def register_auto_buy(user_id:int, limit:int)->dict:
    if not 1<=int(limit)<=20:return {"ok":False,"reason":"Daily auto-buy limit must be between 1 and 20."}
    today=_today_str()
    async with async_session() as session:
        result=await session.execute(text("""UPDATE real_wallets SET auto_daily_buy_count=CASE WHEN auto_daily_buy_count_date=:today THEN COALESCE(auto_daily_buy_count,0)+1 ELSE 1 END, auto_daily_buy_count_date=:today WHERE user_id=:user_id AND is_active=true AND chain_id=:chain_id AND auto_kill_switch=false AND auto_trading_enabled=true AND (CASE WHEN auto_daily_buy_count_date=:today THEN COALESCE(auto_daily_buy_count,0) ELSE 0 END)<:limit RETURNING id"""),{"today":today,"user_id":user_id,"chain_id":ROBINHOOD_EVM_CHAIN_ID,"limit":int(limit)}); reserved=result.first(); await session.commit()
    return {"ok":True} if reserved else {"ok":False,"reason":f"Daily auto-buy limit reached ({int(limit)} buys today)."}

async def release_auto_buy(user_id:int)->None:
    async with async_session() as session:
        await session.execute(text("UPDATE real_wallets SET auto_daily_buy_count=GREATEST(0,COALESCE(auto_daily_buy_count,0)-1) WHERE user_id=:user_id AND is_active=true AND chain_id=:chain_id AND auto_daily_buy_count_date=:today"),{"user_id":user_id,"chain_id":ROBINHOOD_EVM_CHAIN_ID,"today":_today_str()}); await session.commit()
