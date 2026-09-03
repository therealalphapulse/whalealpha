import html
import logging

from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from infra.db.session import async_session
from models.tracked_wallet import TrackedWallet
from providers.rpc.helius import get_wallet_transactions

logger = logging.getLogger("AlphaPulse.Whales")


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else "N/A"


def _short(value: str, length: int = 12) -> str:
    if not value:
        return "N/A"

    if len(value) <= length:
        return value

    return value[:length] + "..."


def _format_amount(value) -> str:
    try:
        num = float(value)

        if num == 0:
            return "N/A"
        elif num >= 1_000_000_000:
            return f"{num / 1_000_000_000:.2f}B"
        elif num >= 1_000_000:
            return f"{num / 1_000_000:.2f}M"
        elif num >= 1_000:
            return f"{num / 1_000:.2f}K"
        elif num >= 1:
            return f"{num:,.4f}".rstrip("0").rstrip(".")
        else:
            return f"{num:.8f}".rstrip("0").rstrip(".")

    except (ValueError, TypeError):
        return "N/A"


async def add_tracked_wallet(user_id: int, wallet_address: str, label: str = None) -> bool:
    """
    Add a wallet to track. Returns False if already exists.
    """

    wallet_address = wallet_address.strip().strip(",.;")

    async with async_session() as session:
        try:
            new_wallet = TrackedWallet(
                user_id=user_id,
                wallet_address=wallet_address,
                label=label or "Whale"
            )

            session.add(new_wallet)
            await session.commit()
            return True

        except IntegrityError:
            await session.rollback()
            return False


async def remove_tracked_wallet(user_id: int, wallet_address: str) -> bool:
    """
    Remove a tracked wallet.
    """

    wallet_address = wallet_address.strip().strip(",.;")

    async with async_session() as session:
        result = await session.execute(
            delete(TrackedWallet).where(
                TrackedWallet.user_id == user_id,
                TrackedWallet.wallet_address == wallet_address
            )
        )

        await session.commit()
        return result.rowcount > 0


async def get_tracked_wallets(user_id: int) -> list:
    """
    Get all tracked wallets for a user.
    """

    async with async_session() as session:
        result = await session.execute(
            select(TrackedWallet).where(TrackedWallet.user_id == user_id)
        )

        return result.scalars().all()


async def get_wallet_activity(wallet_address: str, limit: int = 5) -> list[dict]:
    """
    Get recent wallet activity.
    """

    wallet_address = wallet_address.strip().strip(",.;")
    transactions = await get_wallet_transactions(wallet_address, limit=limit)
    return transactions


async def get_all_tracked_wallets() -> list:
    """
    Get all tracked wallets across all users.
    """

    async with async_session() as session:
        result = await session.execute(select(TrackedWallet))
        return result.scalars().all()


async def get_matching_tracked_whales(holder_addresses: list[str]) -> list[dict]:
    """
    Cross-references a token's current top holder addresses against every
    user-added tracked wallet (/track), regardless of which user added it.

    Only returns wallets that genuinely appear in holder_addresses right
    now — never an estimate. The same address tracked by several users is
    de-duplicated and its distinct labels merged. Empty list if none match
    or on any lookup error.
    """
    if not holder_addresses:
        return []

    async with async_session() as session:
        try:
            result = await session.execute(
                select(TrackedWallet).where(
                    TrackedWallet.wallet_address.in_(holder_addresses)
                )
            )
            matches = result.scalars().all()
        except Exception as e:
            logger.warning(f"get_matching_tracked_whales lookup failed: {e}")
            return []

    merged: dict[str, set] = {}
    for w in matches:
        merged.setdefault(w.wallet_address, set()).add(w.label or "Whale")

    return [
        {"wallet_address": addr, "labels": sorted(labels)}
        for addr, labels in merged.items()
    ]


def format_wallet_activity(wallet_address: str, transactions: list[dict]) -> str:
    """
    Format wallet activity into a readable Telegram message.
    """

    wallet_address = wallet_address.strip().strip(",.;")

    if not transactions:
        return (
            "📭 <b>No Recent Activity</b>\n\n"
            f"📝 <code>{_esc(wallet_address[:20])}...</code>\n\n"
            "Possible reasons:\n"
            "• Wallet has no recent transactions\n"
            "• Helius API key missing or rate limited\n"
            "• Helius enhanced transaction endpoint temporarily unavailable"
        )

    text = (
        "🐋 <b>Wallet Activity</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📝 Wallet:\n<code>{_esc(wallet_address)}</code>\n\n"
    )

    for i, tx in enumerate(transactions[:8], 1):
        action = tx.get("type", "TX")
        token = tx.get("token", "UNKNOWN")
        amount = tx.get("amount", 0)
        signature = tx.get("signature", "")
        description = tx.get("description", "")

        if action == "IN":
            emoji = "🟢"
            label = "IN"
        elif action == "OUT":
            emoji = "🔴"
            label = "OUT"
        else:
            emoji = "⚪"
            label = "TX"

        token_display = _short(str(token), 16)
        amount_display = _format_amount(amount)

        text += (
            f"<b>{i}.</b> {emoji} <b>{label}</b>\n"
            f"   🪙 Token/Type: <code>{_esc(token_display)}</code>\n"
            f"   💰 Amount: <b>{_esc(amount_display)}</b>\n"
        )

        if description:
            clean_description = _esc(description)

            if len(clean_description) > 120:
                clean_description = clean_description[:120] + "..."

            text += f"   🧾 {clean_description}\n"

        if signature:
            solscan_url = f"https://solscan.io/tx/{signature}"
            text += f"   🔎 <a href=\"{solscan_url}\">View Tx</a>\n"

        text += "\n"

    text += "━━━━━━━━━━━━━━━━━━━━━\n⚡ Powered by AlphaPulse"

    return text


def format_all_wallets(wallets: list) -> str:
    """
    Format tracked wallets list.
    """

    if not wallets:
        return (
            "📭 <b>No Tracked Wallets</b>\n\n"
            "Use <code>/track &lt;wallet_address&gt;</code> to start tracking whales."
        )

    text = (
        "🐋 <b>Tracked Wallets</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    for i, wallet in enumerate(wallets, 1):
        label = wallet.label or "Whale"
        address = wallet.wallet_address

        text += (
            f"<b>{i}.</b> {_esc(label)}\n"
            f"   📝 <code>{_esc(address[:20])}...</code>\n\n"
        )

    text += (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total: {len(wallets)} wallets\n"
        f"⚡ Powered by AlphaPulse"
    )

    return text
