import asyncio
import logging
from datetime import datetime

from sqlalchemy import select, delete
from infra.db.session import async_session
from models.watchlist import Watchlist
from providers.marketdata.dexscreener import get_token_info
# v4: was `from app_platform.keyboards.token_actions import token_actions_keyboard`
# — same layering violation as pump_radar.py, same fix.
from domain.signals.keyboard_provider import build_token_actions_keyboard

logger = logging.getLogger("AlphaPulse.Alerts")

# In-memory cache to avoid spamming users
# Key: (user_id, contract) -> Value: last_known_price
_price_cache: dict[tuple[int, str], float] = {}


async def check_single_token(user_id: int, contract: str, bot) -> bool:
    """
    Check a single token for price changes.
    Returns True if an alert was sent.
    """
    global _price_cache
    
    try:
        data = await get_token_info(contract)
        if not data:
            return False

        current_price_str = data.get("price", "0")
        try:
            current_price = float(current_price_str)
        except (ValueError, TypeError):
            return False

        # Get last known price
        cache_key = (user_id, contract)
        last_price = _price_cache.get(cache_key)

        if last_price is None:
            # First time seeing this token -> just cache it, no alert
            _price_cache[cache_key] = current_price
            return False

        # Calculate price change
        if last_price == 0 or current_price == 0:
            _price_cache[cache_key] = current_price
            return False

        change_pct = ((current_price - last_price) / last_price) * 100

        # Decide if we should alert
        should_alert = False
        direction = ""
        
        if abs(change_pct) >= 5:  # 5% threshold
            should_alert = True
            direction = "🟢 PUMP" if change_pct > 0 else "🔴 DUMP"
            
            # Also update the cache
            _price_cache[cache_key] = current_price

        if should_alert:
            try:
                kb = build_token_actions_keyboard(contract, data.get("pair_url"))
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"{direction} <b>Alert</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📛 <b>{data['name']}</b> ({data['symbol']})\n"
                        f"💰 Price: <b>${current_price:.6f}</b>\n"
                        f"📊 Change: <b>{change_pct:+.2f}%</b>\n\n"
                        f"🔗 On-chain: <code>{contract[:20]}...</code>"
                    ),
                    reply_markup=kb,
                )
                logger.info(f"Alert sent to {user_id} for {contract}: {change_pct:.2f}%")
                return True
            except Exception as e:
                logger.error(f"Failed to send alert to {user_id}: {e}")
                return False

        # Update cache periodically even without alert
        _price_cache[cache_key] = current_price
        return False

    except Exception as e:
        logger.error(f"Error checking {contract} for user {user_id}: {e}")
        return False


async def alert_loop(bot):
    """
    Background task that runs forever.
    Every 30 seconds, checks all watched tokens.
    """
    logger.info("⚡ Alert Engine started")
    
    while True:
        try:
            # Get all watchlist entries
            async with async_session() as session:
                result = await session.execute(select(Watchlist))
                watchlist_items = result.scalars().all()

            if watchlist_items:
                logger.info(f"Checking {len(watchlist_items)} watchlist entries...")
                
                # Check each token
                for item in watchlist_items:
                    await check_single_token(
                        user_id=item.user_id,
                        contract=item.contract,
                        bot=bot
                    )
                    # Small delay between checks to avoid rate limits
                    await asyncio.sleep(0.5)

            # Wait 30 seconds before next scan
            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"Alert loop error: {e}")
            await asyncio.sleep(30)  # Still wait even if error
