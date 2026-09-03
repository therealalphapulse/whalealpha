import asyncio
import logging
import os
import random

from aiogram.types import BufferedInputFile

from domain.trading.paper.paper_engine import get_all_open_trades, close_paper_trade, check_and_apply_dca
from providers.marketdata.dexscreener import get_token_card_info
from domain.trading.pnl_image import generate_pnl_card_image, reaction_tier_for_pnl

try:
    from config.settings import PNL_MASCOT_DIR, PNL_MASCOT_PATH
except ImportError:
    PNL_MASCOT_DIR = None
    PNL_MASCOT_PATH = None

logger = logging.getLogger("AlphaPulse.PaperMonitor")

_VALID_MASCOT_EXT = (".png", ".jpg", ".jpeg", ".webp")


def _pick_mascot_path(pnl_pct: float = 0.0) -> str | None:
    """
    Picks a mascot image for the auto-generated PnL card, matched to the
    trade's outcome tier (see pnl_image.reaction_tier_for_pnl) so the
    reaction changes with the actual result instead of being the same
    image (or a purely random one) every time.

    Supports two custom-art layouts, checked in order:
      1. PNL_MASCOT_DIR/<tier>/  — a subfolder per tier (shocked, excited,
         happy, neutral, worried, crying — mega win / large profit / small
         profit / break-even / small loss / heavy loss). Drop as many
         illustrated character images as you want into each subfolder; one
         is picked at random from WITHIN the matching tier's subfolder each
         time, so real illustrated art (Photon-style or your own) rarely
         repeats. See README for the exact folder layout.
      2. PNL_MASCOT_DIR/ (flat)  — one pool of images shared across every
         tier, picked at random, for setups that don't have per-tier art.
    Falls back to PNL_MASCOT_PATH (single fixed image), and finally to
    None, which makes pnl_image.py draw one of several procedural faces
    (multiple variants per tier: shocked/excited/happy/neutral/worried/
    crying) built into this codebase, chosen at random.
    """
    tier = reaction_tier_for_pnl(pnl_pct)

    if PNL_MASCOT_DIR and os.path.isdir(PNL_MASCOT_DIR):
        tier_dir = os.path.join(PNL_MASCOT_DIR, tier)
        if os.path.isdir(tier_dir):
            tier_candidates = [
                os.path.join(tier_dir, f)
                for f in os.listdir(tier_dir)
                if f.lower().endswith(_VALID_MASCOT_EXT)
            ]
            if tier_candidates:
                return random.choice(tier_candidates)

        flat_candidates = [
            os.path.join(PNL_MASCOT_DIR, f)
            for f in os.listdir(PNL_MASCOT_DIR)
            if f.lower().endswith(_VALID_MASCOT_EXT)
        ]
        if flat_candidates:
            return random.choice(flat_candidates)

    if PNL_MASCOT_PATH and os.path.isfile(PNL_MASCOT_PATH):
        return PNL_MASCOT_PATH

    return None


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return default


def format_usd(value) -> str:
    try:
        num = float(value)
        if abs(num) >= 1000:
            return f"${num:,.2f}"
        return f"${num:.2f}"
    except Exception:
        return "N/A"


async def send_pnl_card(bot, result: dict, reason_label: str):
    """
    Instantly generates and sends a PnL card image the moment a TP/SL
    triggers — no manual "history" navigation required. The mascot graphic
    is dynamic (see _pick_mascot_path); falls back to a text-only card if
    image generation fails for any reason, so a TP/SL close is never
    silently dropped.
    """
    pnl_emoji = "🟢" if result.get("pnl_usd", 0) >= 0 else "🔴"
    pnl_sign = "+" if result.get("pnl_usd", 0) >= 0 else ""

    caption = (
        f"{reason_label} {pnl_emoji}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📛 <b>{result.get('name', 'Unknown')}</b> (${result.get('symbol', '???')})\n\n"
        f"💰 Entry: <b>${result.get('entry_price', 0):.8f}</b>\n"
        f"💎 Exit: <b>${result.get('exit_price', 0):.8f}</b>\n\n"
        f"📊 Invested: <b>{format_usd(result.get('usd_invested', 0))}</b>\n"
        f"💵 Return: <b>{format_usd(result.get('current_value', 0))}</b>\n\n"
        f"{pnl_emoji} PnL: <b>{pnl_sign}{format_usd(result.get('pnl_usd', 0))}</b>\n"
        f"{pnl_emoji} ROI: <b>{pnl_sign}{result.get('pnl_pct', 0):+.1f}%</b>\n\n"
        f"🕒 Hold Time: {result.get('holding_time', 'N/A')}\n"
        f"📋 Reason: {reason_label}\n\n"
        f"<code>{result.get('contract', '')}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 AlphaPulse Paper Trading"
    )

    card_data = {
        "name": result.get("name", "Unknown"),
        "symbol": result.get("symbol", "???"),
        "entry_price": result.get("entry_price", 0),
        "current_price": result.get("exit_price", 0),
        "usd_invested": result.get("usd_invested", 0),
        "pnl_usd": result.get("pnl_usd", 0),
        "pnl_pct": result.get("pnl_pct", 0),
        "status": reason_label.replace("✅ ", "").replace("🔴 ", "").upper(),
        "opened_at_str": result.get("opened_at_str", "N/A"),
        "mascot_path": _pick_mascot_path(result.get("pnl_pct", 0.0)),
    }

    try:
        png_bytes = await generate_pnl_card_image(card_data)
        if png_bytes:
            photo = BufferedInputFile(png_bytes, filename="pnl_card.png")
            await bot.send_photo(result.get("user_id"), photo, caption=caption)
            return
    except Exception as e:
        logger.warning(f"PnL card image generation/send failed, falling back to text: {e}")

    try:
        await bot.send_message(result.get("user_id"), caption)
    except Exception as e:
        logger.warning(f"PnL card send failed: {e}")


async def paper_monitor_loop(bot, interval_seconds: int = 30):
    logger.info("📊 Paper Trading Monitor Active")

    while True:
        try:
            trades = await get_all_open_trades()

            for trade in trades:
                await check_trade_tp_sl(bot, trade)
                await asyncio.sleep(0.5)

        except Exception as e:
            logger.error(f"Paper monitor loop error: {e}")

        await asyncio.sleep(interval_seconds)


async def check_trade_tp_sl(bot, trade):
    try:
        data = await get_token_card_info(trade.contract)
        if not data:
            return

        current_price = _to_float(data.get("price"))
        if current_price <= 0:
            return

        # DCA drawdown check first, reusing the price we already fetched
        # above — no extra API call. A fill updates trade.entry_price
        # (new weighted average) and trade.dca_fills in place, so the
        # TP/SL math right below always sees the post-fill state.
        try:
            dca_result = await check_and_apply_dca(trade, current_price)
            if dca_result and dca_result.get("ok"):
                trade.entry_price = dca_result["new_avg_entry_price"]
                trade.dca_fills = (trade.dca_fills or 0) + 1
                if bot:
                    try:
                        await bot.send_message(
                            trade.user_id,
                            f"🧬 <b>DCA Fill #{dca_result['fill_number']}</b> — {trade.symbol}\n"
                            f"Bought {format_usd(dca_result['fill_usd_amount'])} more @ "
                            f"${dca_result['fill_price']:.8f}\n"
                            f"New average entry: <b>${dca_result['new_avg_entry_price']:.8f}</b>",
                        )
                    except Exception as e:
                        logger.warning(f"DCA fill notification failed: {e}")
        except Exception as e:
            logger.error(f"DCA check error for trade {trade.id}: {e}")

        entry = trade.entry_price
        if entry <= 0:
            return

        change_pct = ((current_price - entry) / entry) * 100

        if trade.take_profit_pct and change_pct >= trade.take_profit_pct:
            result = await close_paper_trade(trade.id, current_price, "closed_tp")
            if result["ok"]:
                await send_pnl_card(bot, result, "✅ Take Profit")

        elif trade.stop_loss_pct and change_pct <= -trade.stop_loss_pct:
            result = await close_paper_trade(trade.id, current_price, "closed_sl")
            if result["ok"]:
                await send_pnl_card(bot, result, "🔴 Stop Loss")

    except Exception as e:
        logger.error(f"Monitor error for trade {trade.id}: {e}")
