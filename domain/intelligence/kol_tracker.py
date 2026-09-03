import asyncio
import csv
import html
import io
import json
import logging
import re

import aiohttp
from sqlalchemy import select, update, text

from infra.db.session import async_session, engine
from config.settings import (
    KOL_PROVIDER_URL,
    KOL_PROVIDER_API_KEY,
    KOL_PROVIDER_FORMAT,
    KOL_PROVIDER_NAME,
)
from models.kol_wallet import KolWallet
from models.kol_subscription import KolAlertSubscription

logger = logging.getLogger("AlphaPulse.KOLProvider")

SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def is_valid_solana_address(address: str) -> bool:
    return bool(SOLANA_ADDRESS_RE.fullmatch(address or ""))


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else "N/A"


def _short(address: str, size: int = 6) -> str:
    if not address:
        return "N/A"
    if len(address) <= size * 2:
        return address
    return f"{address[:size]}...{address[-size:]}"


def _to_float(value):
    try:
        if value in ("", None):
            return None
        return float(value)
    except (ValueError, TypeError):
        return None


def _to_int(value):
    try:
        if value in ("", None):
            return None
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _to_bool(value, default: bool = True) -> bool:
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    text_value = str(value).strip().lower()

    if text_value in {"true", "1", "yes", "y", "active"}:
        return True

    if text_value in {"false", "0", "no", "n", "inactive", "disabled"}:
        return False

    return default


def _first_present(data: dict, keys: list[str], default=None):
    for key in keys:
        value = data.get(key)
        if value not in ("", None):
            return value
    return default


def _normalize_tags(value) -> str:
    if value is None:
        return ""

    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())

    return str(value).strip()


def _normalize_handle(value) -> str:
    if not value:
        return ""

    handle = str(value).strip()

    if handle and not handle.startswith("@"):
        handle = f"@{handle}"

    return handle


def _format_usd(value) -> str:
    if value is None:
        return "N/A"

    try:
        num = float(value)

        if num >= 1_000_000:
            return f"${num / 1_000_000:.2f}M"
        elif num >= 1_000:
            return f"${num / 1_000:.2f}K"
        else:
            return f"${num:,.2f}"
    except (ValueError, TypeError):
        return "N/A"


def _format_pct(value) -> str:
    if value is None:
        return "N/A"

    try:
        return f"{float(value):.1f}%"
    except (ValueError, TypeError):
        return "N/A"


async def migrate_kol_wallet_schema() -> None:
    """
    Adds new KOL provider columns safely.

    Needed because SQLAlchemy create_all() does not alter existing tables.
    Safe to run multiple times.
    """

    statements = [
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS x_username VARCHAR",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS tags TEXT",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS provider VARCHAR DEFAULT 'kol_provider'",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS provider_id VARCHAR",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS pnl_30d DOUBLE PRECISION",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS win_rate DOUBLE PRECISION",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS follower_count BIGINT",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS score DOUBLE PRECISION",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS provider_last_signature VARCHAR",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS provider_last_active VARCHAR",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS raw_data TEXT",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS last_signature VARCHAR",
        "ALTER TABLE kol_wallets ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP",
    ]

    try:
        async with engine.begin() as conn:
            for stmt in statements:
                await conn.execute(text(stmt))

        logger.info("✅ KOL wallet schema migration complete")
    except Exception as e:
        logger.error(f"KOL schema migration error (non-fatal): {e}")


async def fetch_provider_payload():
    """
    Fetch wallet list from external provider.

    Supports JSON and CSV. Uses optional API key.
    Returns None if provider is unavailable or not configured.
    """

    if not KOL_PROVIDER_URL:
        logger.info("KOL_PROVIDER_URL is not set. Provider sync disabled.")
        return None

    headers = {
        "Accept": "application/json,text/csv,text/plain,*/*"
    }

    if KOL_PROVIDER_API_KEY:
        headers["Authorization"] = f"Bearer {KOL_PROVIDER_API_KEY}"
        headers["X-API-Key"] = KOL_PROVIDER_API_KEY

    try:
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(KOL_PROVIDER_URL, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"KOL provider HTTP {resp.status}: {body[:300]}")
                    return None

                content_type = resp.headers.get("Content-Type", "").lower()
                body_text = await resp.text()

        if KOL_PROVIDER_FORMAT == "csv" or "csv" in content_type:
            reader = csv.DictReader(io.StringIO(body_text))
            return list(reader)

        parsed = json.loads(body_text)

        if isinstance(parsed, list):
            return parsed

        if isinstance(parsed, dict):
            return (
                parsed.get("wallets")
                or parsed.get("data")
                or parsed.get("results")
                or parsed.get("items")
                or []
            )

        return []

    except Exception as e:
        logger.error(f"KOL provider fetch failed: {e}")
        return None


def normalize_provider_wallet(raw: dict) -> dict | None:
    """
    Converts provider-specific fields into AlphaPulse standard fields.
    Returns None for invalid entries.
    """

    if not isinstance(raw, dict):
        return None

    wallet_address = str(
        _first_present(
            raw,
            [
                "wallet_address",
                "address",
                "wallet",
                "walletAddress",
                "solana_wallet",
                "solanaAddress",
            ],
            "",
        )
    ).strip()

    if not is_valid_solana_address(wallet_address):
        return None

    label = str(
        _first_present(
            raw,
            ["label", "name", "display_name", "displayName", "kol", "username"],
            "Unknown KOL",
        )
    ).strip()

    handle = _normalize_handle(
        _first_present(
            raw,
            ["handle", "x_username", "twitter", "twitter_username", "x", "username"],
            "",
        )
    )

    tags = _normalize_tags(
        _first_present(raw, ["tags", "tag_list", "categories", "labels"], "")
    )

    category = str(
        _first_present(raw, ["category", "type", "wallet_type"], "kol")
    ).strip()

    pnl_30d = _to_float(
        _first_present(raw, ["pnl_30d", "pnl30d", "pnl", "realized_pnl", "profit_30d"])
    )

    win_rate = _to_float(
        _first_present(raw, ["win_rate", "winrate", "winRate", "wr"])
    )

    follower_count = _to_int(
        _first_present(raw, ["follower_count", "followers", "x_followers"])
    )

    score = _to_float(
        _first_present(raw, ["score", "smart_score", "kol_score", "rank_score"])
    )

    provider_id = str(
        _first_present(raw, ["id", "provider_id", "wallet_id"], "")
    ).strip()

    source_url = str(
        _first_present(raw, ["source_url", "profile_url", "url", "x_url"], "")
    ).strip()

    verification_status = str(
        _first_present(raw, ["verification_status", "verified", "status"], "provider_synced")
    ).strip()

    active = _to_bool(_first_present(raw, ["active", "enabled", "is_active"], True))

    provider_last_signature = str(
        _first_present(
            raw,
            [
                "last_signature",
                "last_tx_hash",
                "last_transaction",
                "lastTransaction",
                "last_tx",
            ],
            "",
        )
    ).strip()

    provider_last_active = str(
        _first_present(
            raw,
            [
                "last_active",
                "last_active_at",
                "lastActive",
                "lastActiveAt",
                "last_seen",
                "last_trade_time",
            ],
            "",
        )
    ).strip()

    return {
        "wallet_address": wallet_address,
        "label": label,
        "handle": handle,
        "x_username": handle,
        "category": category,
        "tags": tags,
        "provider": KOL_PROVIDER_NAME or "kol_provider",
        "provider_id": provider_id,
        "source_url": source_url,
        "verification_status": verification_status,
        "pnl_30d": pnl_30d,
        "win_rate": win_rate,
        "follower_count": follower_count,
        "score": score,
        "active": active,
        "provider_last_signature": provider_last_signature,
        "provider_last_active": provider_last_active,
        "raw_data": json.dumps(raw, ensure_ascii=False)[:10000],
    }


async def get_enabled_kol_subscribers() -> list[int]:
    async with async_session() as session:
        result = await session.execute(
            select(KolAlertSubscription).where(KolAlertSubscription.enabled == True)
        )

        subscriptions = result.scalars().all()

    return [sub.user_id for sub in subscriptions]


async def notify_kol_subscribers(bot, wallet_data: dict, reason: str, signature: str | None = None) -> None:
    """
    Notify subscribers about KOL wallet activity.

    wallet_data is a plain dict to avoid detached ORM object issues.
    """

    subscribers = await get_enabled_kol_subscribers()

    if not subscribers:
        return

    label = wallet_data.get("label", "Unknown KOL")
    handle = wallet_data.get("x_username") or wallet_data.get("handle") or ""
    handle_text = f" {handle}" if handle else ""
    category = wallet_data.get("category") or "kol"
    tags = wallet_data.get("tags") or "N/A"
    wallet_address = wallet_data.get("wallet_address", "")
    pnl_30d = wallet_data.get("pnl_30d")
    win_rate = wallet_data.get("win_rate")

    wallet_url = f"https://solscan.io/account/{wallet_address}"

    text_msg = (
        "🚨 <b>KOL Wallet Activity</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🧠 <b>{_esc(label)}</b>{_esc(handle_text)}\n"
        f"🏷️ Category: <b>{_esc(category)}</b>\n"
        f"🏷️ Tags: <b>{_esc(tags)}</b>\n"
        f"📝 Wallet: <code>{_short(wallet_address, 8)}</code>\n\n"
        f"⚡ Activity: <b>{_esc(reason)}</b>\n"
        f"💰 30D PnL: <b>{_format_usd(pnl_30d)}</b>\n"
        f"🎯 Win Rate: <b>{_format_pct(win_rate)}</b>\n\n"
    )

    if signature:
        tx_url = f"https://solscan.io/tx/{signature}"
        text_msg += f"🔎 <a href=\"{tx_url}\">View Transaction</a>\n"

    text_msg += (
        f"👛 <a href=\"{wallet_url}\">View Wallet</a>\n\n"
        "⚠️ <i>Provider-synced wallet activity. Not financial advice.</i>"
    )

    for user_id in subscribers:
        try:
            await bot.send_message(chat_id=user_id, text=text_msg)
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Failed to notify KOL subscriber {user_id}: {e}")


async def sync_kol_wallets_from_provider(bot=None) -> dict:
    """
    Sync KOL wallets from external provider.

    - Adds new wallets
    - Updates existing wallets
    - Deactivates wallets removed by the provider
    - Alerts subscribers if provider reports new activity
    """

    payload = await fetch_provider_payload()

    if payload is None:
        return {
            "ok": False,
            "imported": 0,
            "active": 0,
            "alerts": 0,
            "reason": "provider_unavailable_or_not_configured",
        }

    normalized_wallets = []

    for raw in payload:
        normalized = normalize_provider_wallet(raw)
        if normalized:
            normalized_wallets.append(normalized)

    if not normalized_wallets:
        logger.warning("KOL provider returned no valid Solana wallets.")
        return {
            "ok": True,
            "imported": 0,
            "active": 0,
            "alerts": 0,
            "reason": "no_valid_wallets",
        }

    provider_name = KOL_PROVIDER_NAME or "kol_provider"
    seen_addresses = set()
    alert_candidates = []

    async with async_session() as session:
        for item in normalized_wallets:
            wallet_address = item["wallet_address"]
            seen_addresses.add(wallet_address)

            result = await session.execute(
                select(KolWallet).where(KolWallet.wallet_address == wallet_address)
            )

            existing = result.scalar_one_or_none()

            if existing:
                old_provider_sig = existing.provider_last_signature
                old_provider_active = existing.provider_last_active

                existing.label = item["label"]
                existing.handle = item["handle"]
                existing.x_username = item["x_username"]
                existing.category = item["category"]
                existing.tags = item["tags"]
                existing.provider = item["provider"]
                existing.provider_id = item["provider_id"]
                existing.source_url = item["source_url"]
                existing.verification_status = item["verification_status"]
                existing.pnl_30d = item["pnl_30d"]
                existing.win_rate = item["win_rate"]
                existing.follower_count = item["follower_count"]
                existing.score = item["score"]
                existing.active = item["active"]
                existing.raw_data = item["raw_data"]

                new_provider_sig = item["provider_last_signature"]
                new_provider_active = item["provider_last_active"]

                activity_changed = False
                activity_reason = ""

                # Prefer signature-based alerts
                if new_provider_sig:
                    if old_provider_sig and new_provider_sig != old_provider_sig:
                        activity_changed = True
                        activity_reason = "New provider transaction detected"

                    existing.provider_last_signature = new_provider_sig

                # Fallback to last-active timestamp changes
                elif new_provider_active:
                    if old_provider_active and new_provider_active != old_provider_active:
                        activity_changed = True
                        activity_reason = "Provider last-active time changed"

                existing.provider_last_active = new_provider_active

                if activity_changed and existing.active:
                    alert_candidates.append(
                        {
                            "wallet_data": {
                                "label": existing.label,
                                "handle": existing.handle,
                                "x_username": existing.x_username,
                                "category": existing.category,
                                "tags": existing.tags,
                                "wallet_address": existing.wallet_address,
                                "pnl_30d": existing.pnl_30d,
                                "win_rate": existing.win_rate,
                            },
                            "reason": activity_reason,
                            "signature": new_provider_sig or None,
                        }
                    )

            else:
                new_wallet = KolWallet(
                    label=item["label"],
                    handle=item["handle"],
                    x_username=item["x_username"],
                    wallet_address=item["wallet_address"],
                    category=item["category"],
                    tags=item["tags"],
                    provider=item["provider"],
                    provider_id=item["provider_id"],
                    source_url=item["source_url"],
                    verification_status=item["verification_status"],
                    pnl_30d=item["pnl_30d"],
                    win_rate=item["win_rate"],
                    follower_count=item["follower_count"],
                    score=item["score"],
                    active=item["active"],
                    provider_last_signature=item["provider_last_signature"],
                    provider_last_active=item["provider_last_active"],
                    raw_data=item["raw_data"],
                )

                session.add(new_wallet)

        # Deactivate provider wallets no longer returned by provider.
        if seen_addresses:
            await session.execute(
                update(KolWallet)
                .where(KolWallet.provider == provider_name)
                .where(KolWallet.wallet_address.notin_(seen_addresses))
                .values(active=False)
            )

        await session.commit()

    alert_count = 0

    if bot:
        for candidate in alert_candidates[:50]:
            try:
                await notify_kol_subscribers(
                    bot=bot,
                    wallet_data=candidate["wallet_data"],
                    reason=candidate["reason"],
                    signature=candidate["signature"],
                )
                alert_count += 1
            except Exception as e:
                logger.warning(f"KOL alert send failed: {e}")

    logger.info(
        f"KOL provider sync complete: imported={len(normalized_wallets)}, alerts={alert_count}"
    )

    return {
        "ok": True,
        "imported": len(normalized_wallets),
        "active": len([w for w in normalized_wallets if w["active"]]),
        "alerts": alert_count,
        "reason": "ok",
    }


async def kol_provider_sync_loop(bot, interval_seconds: int = 300) -> None:
    """
    Background provider sync loop.
    Runs forever. Never crashes the bot.
    """

    logger.info(f"🧠 KOL Provider Sync started. Interval={interval_seconds}s")

    while True:
        try:
            await sync_kol_wallets_from_provider(bot=bot)
        except Exception as e:
            logger.error(f"KOL provider sync loop error: {e}")

        await asyncio.sleep(interval_seconds)


async def get_active_kol_wallets() -> list[KolWallet]:
    async with async_session() as session:
        result = await session.execute(
            select(KolWallet)
            .where(KolWallet.active == True)
            .order_by(KolWallet.label.asc())
        )

        return result.scalars().all()


async def get_matching_kol_holders(holder_addresses: list[str]) -> list[dict]:
    """
    Cross-references a token's current top holder addresses against the
    provider-synced "smart money" KOL wallet table.

    Returns only wallets that are BOTH marked active AND actually present
    in holder_addresses (i.e. genuinely hold the token right now, per the
    same Helius snapshot used elsewhere on the card) — never a fabricated
    or estimated count. Empty list if none match or on any lookup error.
    """
    if not holder_addresses:
        return []

    async with async_session() as session:
        try:
            result = await session.execute(
                select(KolWallet).where(
                    KolWallet.wallet_address.in_(holder_addresses),
                    KolWallet.active == True,
                )
            )
            matches = result.scalars().all()
        except Exception as e:
            logger.warning(f"get_matching_kol_holders lookup failed: {e}")
            return []

    return [
        {
            "label": w.label,
            "handle": w.x_username or w.handle,
            "category": w.category or "smart_money",
            "win_rate": w.win_rate,
            "pnl_30d": w.pnl_30d,
            "wallet_address": w.wallet_address,
        }
        for w in matches
    ]


async def set_kol_alert_subscription(user_id: int, enabled: bool) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(KolAlertSubscription).where(KolAlertSubscription.user_id == user_id)
        )

        sub = result.scalar_one_or_none()

        if sub:
            sub.enabled = enabled
        else:
            sub = KolAlertSubscription(user_id=user_id, enabled=enabled)
            session.add(sub)

        await session.commit()


async def get_kol_alert_status(user_id: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(KolAlertSubscription).where(KolAlertSubscription.user_id == user_id)
        )

        sub = result.scalar_one_or_none()

    return bool(sub and sub.enabled)


def format_kol_wallets_list(wallets: list[KolWallet]) -> str:
    if not wallets:
        return (
            "📭 <b>No Active Provider KOL Wallets</b>\n\n"
            "Set <code>KOL_PROVIDER_URL</code> in Railway Variables, then redeploy or run /kol_sync."
        )

    text = (
        "🧠 <b>Provider-Synced KOL Wallets</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    for index, wallet in enumerate(wallets[:50], 1):
        handle = wallet.x_username or wallet.handle or ""
        handle_text = f" ({handle})" if handle else ""

        tags = wallet.tags or "N/A"

        text += (
            f"<b>{index}. {_esc(wallet.label)}</b>{_esc(handle_text)}\n"
            f"   📝 <code>{_short(wallet.wallet_address)}</code>\n"
            f"   🏷️ {_esc(wallet.category or 'kol')} • {_esc(tags)}\n"
            f"   💰 30D PnL: <b>{_format_usd(wallet.pnl_30d)}</b>\n"
            f"   🎯 Win Rate: <b>{_format_pct(wallet.win_rate)}</b>\n\n"
        )

    text += (
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total active wallets: <b>{len(wallets)}</b>"
    )

    if len(wallets) > 50:
        text += f"\nShowing first 50 of {len(wallets)}."

    return text


# ------------------------------------------------------------------
# Backward compatibility functions.
# These prevent crashes if old code still imports the old names.
# ------------------------------------------------------------------

async def seed_kol_wallets_from_json(filepath: str = "data/kol_wallets.json") -> int:
    logger.info("Static KOL JSON import is deprecated. Using provider sync instead.")
    result = await sync_kol_wallets_from_provider(bot=None)
    return result.get("imported", 0)


async def kol_alert_loop(bot, interval_seconds: int = 300) -> None:
    await kol_provider_sync_loop(bot=bot, interval_seconds=interval_seconds)
