import re
import json
import html
import logging
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery

from providers.marketdata.dexscreener import get_token_card_info
from providers.marketdata.goplus import check_token_security, format_security_report
from domain.admin.user_service import add_to_watchlist, get_or_create_user
from domain.intelligence.holders import get_holder_count
from domain.intelligence.solana_resolver import resolve_solana_address, format_resolution_message
from domain.intelligence.wallet_intelligence import build_wallet_intelligence_card
from domain.signals.signal_tracker import get_previous_signal_for_contract

from app_platform.commands.score import calculate_alpha_score
from app_platform.keyboards.token_actions import token_actions_keyboard

router = Router()
logger = logging.getLogger("AlphaPulse.AutoScan")

SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def normalize_address(text: str) -> str:
    """
    Allows pasted addresses that accidentally contain new lines or spaces.
    """
    return re.sub(r"\s+", "", text.strip())


def is_probable_solana_address(text: str) -> bool:
    return bool(SOLANA_ADDRESS_RE.fullmatch(text))


def esc(value) -> str:
    return html.escape(str(value)) if value is not None else "N/A"


def format_number(value) -> str:
    try:
        num = float(value)

        if num >= 1_000_000_000:
            return f"${num / 1_000_000_000:.2f}B"
        elif num >= 1_000_000:
            return f"${num / 1_000_000:.2f}M"
        elif num >= 1_000:
            return f"${num / 1_000:.2f}K"
        elif num > 0:
            return f"${num:,.2f}"
        else:
            return "$0"
    except (ValueError, TypeError):
        return "N/A"


def format_price(value) -> str:
    try:
        num = float(value)

        if num >= 1:
            return f"${num:,.4f}"
        elif num >= 0.000001:
            return f"${num:.8f}".rstrip("0").rstrip(".")
        elif num > 0:
            return f"${num:.12f}".rstrip("0").rstrip(".")
        else:
            return "$0"
    except (ValueError, TypeError):
        return "N/A"


def format_change(value) -> str:
    try:
        num = float(value)
        emoji = "🟢" if num > 0 else "🔴" if num < 0 else "⚪"
        return f"{emoji} {num:+.2f}%"
    except (ValueError, TypeError):
        return "⚪ N/A"


def format_age(pair_created_ms) -> str:
    try:
        timestamp_ms = int(pair_created_ms)
        created = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        now = datetime.now(timezone.utc)

        delta = now - created

        days = delta.days
        hours = delta.seconds // 3600
        minutes = delta.seconds // 60

        if days >= 365:
            return f"{days // 365}y"
        elif days >= 30:
            return f"{days // 30}mo"
        elif days >= 1:
            return f"{days}d"
        elif hours >= 1:
            return f"{hours}h"
        else:
            return f"{max(minutes, 1)}m"
    except Exception:
        return "N/A"


def _signaled_at_ago(signaled_at) -> str:
    """Same style as format_age() above, but for a SignalToken.signaled_at
    datetime column instead of a DexScreener pair_created_ms timestamp."""
    try:
        if signaled_at is None:
            return "N/A"
        if signaled_at.tzinfo is None:
            signaled_at = signaled_at.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - signaled_at

        days = delta.days
        hours = delta.seconds // 3600
        minutes = delta.seconds // 60

        if days >= 365:
            return f"{days // 365}y ago"
        elif days >= 30:
            return f"{days // 30}mo ago"
        elif days >= 1:
            return f"{days}d ago"
        elif hours >= 1:
            return f"{hours}h ago"
        else:
            return f"{max(minutes, 1)}m ago"
    except Exception:
        return "N/A"


def _format_x(value) -> str:
    try:
        return f"{float(value):.1f}x"
    except (TypeError, ValueError):
        return "N/A"


def build_previous_signal_banner(signal, requester_chat_id: int) -> tuple[str, int | None]:
    """
    Builds the "AlphaPulse already called this" banner for a token a
    user pasted independently, and figures out whether we can quote the
    original alert (Telegram reply preview) for THIS specific chat.

    Reuses the exact same message_ids_json reply mechanism already used
    by domain/signals/signal_tracker.py's send_milestone_alert() — if
    this chat received the original alert, its message id is in there
    and we can reply to it directly. If this chat never received the
    original alert (e.g. the user found the CA somewhere else and wasn't
    subscribed yet at signal time), there's nothing to reply to, so we
    just show the stats instead.

    Returns (banner_text, reply_to_message_id_or_None).
    """
    try:
        msg_ids = json.loads(signal.message_ids_json or "{}")
    except (TypeError, ValueError):
        msg_ids = {}

    reply_to_raw = msg_ids.get(str(requester_chat_id))
    reply_to = int(reply_to_raw) if reply_to_raw else None

    gain = signal.current_multiple or 1.0
    ath = signal.ath_multiple or 1.0
    pct_gain = (gain - 1) * 100
    pct_sign = "+" if pct_gain >= 0 else ""

    lines = [
        "📡 <b>Already Called by AlphaPulse!</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"💊 <b>{esc(signal.name or 'Unknown')}</b> · <b>${esc(signal.symbol or '???')}</b>",
        f"🕒 First called: <b>{_signaled_at_ago(signal.signaled_at)}</b>",
        f"💰 Entry MC: <b>{format_number(signal.entry_market_cap)}</b>",
        f"📊 Since entry: <b>{pct_sign}{pct_gain:.0f}%</b> ({_format_x(gain)})",
        f"🏔️ ATH: <b>{_format_x(ath)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]

    if reply_to:
        lines.append("⬆️ Tap the quoted message above to jump to AlphaPulse's original alert.")
    else:
        lines.append(
            "<i>The original alert wasn't sent to this chat, so it can't be quoted "
            "directly here — but the stats above are from AlphaPulse's own tracked signal.</i>"
        )

    return "\n".join(lines), reply_to


def build_security_line(
    security_data: dict | None,
    holder_count: int | None = None
) -> tuple[str, str]:
    """
    Returns:
    - short security line
    - holder count display
    """

    def is_on(value) -> bool:
        return str(value).lower() in {"1", "true", "yes", "enabled"}

    if holder_count is not None:
        holders_display = f"{holder_count:,}"
    elif security_data and security_data.get("holder_count"):
        holders_display = str(security_data.get("holder_count"))
    else:
        holders_display = "N/A"

    if not security_data:
        return "⚪ Security: GoPlus data unavailable", holders_display

    issues = []

    # Solana-specific authority risks
    if is_on(security_data.get("mintable")):
        issues.append("Mint Authority")
    if is_on(security_data.get("freezable")):
        issues.append("Freeze Authority")
    if is_on(security_data.get("metadata_mutable")):
        issues.append("Mutable Metadata")
    if is_on(security_data.get("balance_mutable_authority")):
        issues.append("Balance Authority")
    if is_on(security_data.get("transfer_fee_upgradable")):
        issues.append("Fee Authority")
    if is_on(security_data.get("non_transferable")):
        issues.append("Transfer Restricted")

    # Compatibility risks from older GoPlus fields
    if is_on(security_data.get("is_honeypot")):
        issues.append("Honeypot")
    if is_on(security_data.get("cannot_sell_all")):
        issues.append("Cannot Sell")
    if is_on(security_data.get("cannot_buy")):
        issues.append("Cannot Buy")
    if is_on(security_data.get("is_blacklisted")):
        issues.append("Blacklisted")
    if is_on(security_data.get("hidden_owner")):
        issues.append("Hidden Owner")
    if is_on(security_data.get("owner_change_balance")):
        issues.append("Owner Risk")

    top_holder = security_data.get("top_holder_percent")
    if isinstance(top_holder, (int, float)) and top_holder >= 20:
        issues.append(f"High Holder {top_holder:.1f}%")

    top_10 = security_data.get("top_10_holder_percent")
    if isinstance(top_10, (int, float)) and top_10 >= 50:
        issues.append(f"Top 10 Hold {top_10:.1f}%")

    if issues:
        return f"⚠️ Risk: {', '.join(issues[:4])}", holders_display

    return "✅ Security: No major flags", holders_display


def build_social_links(data: dict) -> str:
    links = []

    if data.get("website_url"):
        links.append(f'<a href="{esc(data["website_url"])}">WEB</a>')

    if data.get("twitter_url"):
        links.append(f'<a href="{esc(data["twitter_url"])}">X</a>')

    if data.get("telegram_url"):
        links.append(f'<a href="{esc(data["telegram_url"])}">TG</a>')

    return " | ".join(links) if links else "No socials found"


def format_auto_scan_report(
    data: dict,
    security_data: dict | None,
    holder_count: int | None = None
) -> str:
    name = esc(data.get("name", "Unknown"))
    symbol = esc(data.get("symbol", "???"))
    contract = esc(data.get("contract", ""))

    security_line, holders = build_security_line(
        security_data=security_data,
        holder_count=holder_count
    )

    if security_data:
        score_result = calculate_alpha_score(data, security_data)
        alpha_line = f"🧠 Alpha: <b>{score_result['total']}/100</b> • {esc(score_result['rating'])}"
    else:
        alpha_line = "🧠 Alpha: <b>Partial</b> • security unavailable"

    text = (
        f"💊🔁 <b>{name}</b> · <b>${symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{security_line}\n\n"

        f"🕒 Age: <b>{format_age(data.get('pair_created'))}</b>\n"
        f"💰 MC: <b>{format_number(data.get('market_cap'))}</b>\n"
        f"💎 FDV: <b>{format_number(data.get('fdv'))}</b>\n"
        f"💧 Liq: <b>{format_number(data.get('liquidity'))}</b>\n"
        f"📊 Vol: <b>{format_number(data.get('volume_24h'))}</b> [24h]\n"
        f"💵 Price: <b>{format_price(data.get('price'))}</b>\n"
        f"📈 24h: <b>{format_change(data.get('price_change_24h'))}</b>\n\n"

        f"🧾 Txns 1h: "
        f"🟢 {esc(data.get('txns_1h_buys'))} / "
        f"🔴 {esc(data.get('txns_1h_sells'))}\n"
        f"👥 Holders: <b>{esc(holders)}</b>\n"
        f"{alpha_line}\n\n"

        f"🔗 Links: {build_social_links(data)}\n\n"
        f"<code>{contract}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Powered by AlphaPulse"
    )

    return text


async def send_auto_scan_report(message: Message, contract: str):
    """
    Main automatic address scanner.

    Flow:
    0. If AlphaPulse already called this contract before, send a
       "previously called" banner first (quoting the original alert in
       this chat if possible) — purely additive, does not change
       anything below.
    1. Try DexScreener token scan.
    2. If DexScreener finds token pair, send token card.
    3. If no token pair, resolve address type.
    4. If wallet, send Wallet Intelligence Card.
    5. Otherwise, send resolver message.
    """

    try:
        previous_signal = await get_previous_signal_for_contract(contract)
    except Exception as e:
        logger.warning(f"Previous-signal lookup failed for {contract}: {e}")
        previous_signal = None

    if previous_signal:
        banner_text, reply_to = build_previous_signal_banner(
            previous_signal, message.chat.id
        )
        try:
            if reply_to:
                await message.answer(banner_text, reply_to_message_id=reply_to)
            else:
                await message.answer(banner_text)
        except Exception as e:
            # If the reply target message no longer exists/was deleted,
            # fall back to sending the banner without the reply link
            # rather than losing the "already called" notice entirely.
            logger.warning(f"Previous-signal banner reply failed for {contract}: {e}")
            try:
                await message.answer(banner_text)
            except Exception:
                pass

    data = await get_token_card_info(contract)

    # If DexScreener cannot find a token pair,
    # resolve what kind of Solana address this is.
    if not data:
        try:
            resolved = await resolve_solana_address(contract)
            address_kind = resolved.get("kind")

            if address_kind == "wallet":
                await message.answer("👛 Wallet detected. Building intelligence card...")

                wallet_card = await build_wallet_intelligence_card(
                    wallet_address=contract,
                    limit=10
                )

                await message.answer(wallet_card)
                return

            response = format_resolution_message(contract, resolved)
            await message.answer(response)
            return

        except Exception as e:
            logger.error(f"Address resolver failed for {contract}: {e}")

            await message.answer(
                "⚠️ <b>Address Detected, But No DEX Pair Found</b>\n\n"
                "AlphaPulse detected a Solana address, but could not find an active "
                "DexScreener pair or resolve the address type right now.\n\n"
                "Possible reasons:\n"
                "• Token is too new\n"
                "• Token has no active liquidity\n"
                "• Wallet/account is not initialized\n"
                "• Solana RPC or indexer is temporarily unavailable\n\n"
                f"<code>{contract}</code>"
            )
            return

    security_data = await check_token_security(contract)
    holder_count = await get_holder_count(contract)

    report = format_auto_scan_report(
        data=data,
        security_data=security_data,
        holder_count=holder_count
    )

    keyboard = token_actions_keyboard(
        contract=contract,
        pair_url=data.get("pair_url")
    )

    image_url = data.get("image_url")

    if image_url:
        try:
            await message.answer_photo(
                photo=image_url,
                caption=report,
                reply_markup=keyboard
            )
            return
        except Exception:
            # If Telegram rejects image/caption, fall back to text
            pass

    await message.answer(
        report,
        reply_markup=keyboard
    )


@router.message(F.text)
async def auto_detect_contract(message: Message):
    """
    Detect bare Solana contract/wallet addresses without requiring a command.

    Examples:
    User sends token contract:
    DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263

    User sends wallet:
    7x2LZWogFJhybS3RQF7jzbNrTVohnK8Kd6n7vXjauPXq
    """

    raw_text = message.text or ""
    contract = normalize_address(raw_text)

    if not is_probable_solana_address(contract):
        return

    await message.answer("🔎 AlphaPulse scanning address...")
    await send_auto_scan_report(message, contract)


@router.callback_query(F.data.startswith("autoscan:"))
async def handle_autoscan_buttons(callback: CallbackQuery):
    data = callback.data or ""

    try:
        _, action, contract = data.split(":", 2)
    except ValueError:
        await callback.answer("Invalid action.", show_alert=True)
        return

    if action == "track":
        await get_or_create_user(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name
        )

        success = await add_to_watchlist(
            user_id=callback.from_user.id,
            contract=contract
        )

        if success:
            await callback.answer("Added to your watchlist.", show_alert=True)
        else:
            await callback.answer("Already in your watchlist.", show_alert=True)

    elif action == "security":
        await callback.answer("Running security scan...")

        security_data = await check_token_security(contract)

        if not security_data:
            await callback.message.answer(
                "⚠️ Could not fetch security data.\n\n"
                "Possible reasons:\n"
                "• Token not indexed by GoPlus\n"
                "• API temporarily unavailable\n"
                "• System/native token"
            )
            return

        report = format_security_report(security_data, contract)
        await callback.message.answer(report)

    elif action == "score":
        await callback.answer("Calculating Alpha Score...")

        token_data = await get_token_card_info(contract)

        if not token_data:
            await callback.message.answer("⚠️ Token not found on DexScreener.")
            return

        security_data = await check_token_security(contract)
        result = calculate_alpha_score(token_data, security_data)
        bd = result["breakdown"]

        note = ""
        if not security_data:
            note = "\n⚠️ <i>Security data unavailable. Score may be incomplete.</i>\n"

        text = (
            f"🧠 <b>AlphaPulse Score</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📛 <b>{esc(token_data['name'])}</b> ({esc(token_data['symbol'])})\n\n"
            f"🏆 Score: <b>{result['total']}/100</b>\n"
            f"📊 Rating: <b>{esc(result['rating'])}</b>\n\n"
            f"💧 Liquidity: {bd['liquidity']['label']} "
            f"({bd['liquidity']['score']}/{bd['liquidity']['max']})\n"
            f"📊 Volume: {bd['volume']['label']} "
            f"({bd['volume']['score']}/{bd['volume']['max']})\n"
            f"🔒 Security: {bd['security']['label']} "
            f"({bd['security']['score']}/{bd['security']['max']})\n"
            f"📈 Activity: {bd['activity']['label']} "
            f"({bd['activity']['score']}/{bd['activity']['max']})\n"
            f"{note}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ Not financial advice."
        )

        await callback.message.answer(text)

    elif action == "refresh":
        await callback.answer("Refreshing scan...")
        await send_auto_scan_report(callback.message, contract)

    else:
        await callback.answer("Unknown action.", show_alert=True)
