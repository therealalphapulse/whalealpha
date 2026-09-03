from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

from providers.marketdata.dexscreener import get_token_info
from providers.marketdata.goplus import check_token_security

router = Router()


def calculate_alpha_score(token_data: dict, security_data: dict) -> dict:
    """Calculate AlphaPulse score (0-100) based on multiple factors."""

    score = 0
    breakdown = {}

    # --- Liquidity Score (max 25 points) ---
    try:
        liquidity = float(token_data.get("liquidity", 0) or 0)
        if liquidity >= 1_000_000:
            liq_score = 25
            liq_label = "🟢 Very Strong"
        elif liquidity >= 100_000:
            liq_score = 20
            liq_label = "🟢 Strong"
        elif liquidity >= 50_000:
            liq_score = 15
            liq_label = "🟡 Moderate"
        elif liquidity >= 10_000:
            liq_score = 10
            liq_label = "🟠 Low"
        else:
            liq_score = 5
            liq_label = "🔴 Very Low"
    except (ValueError, TypeError):
        liq_score = 0
        liq_label = "⚪ Unknown"

    score += liq_score
    breakdown["liquidity"] = {"score": liq_score, "max": 25, "label": liq_label}

    # --- Volume Score (max 25 points) ---
    try:
        volume = float(token_data.get("volume_24h", 0) or 0)
        if volume >= 5_000_000:
            vol_score = 25
            vol_label = "🟢 Very High"
        elif volume >= 1_000_000:
            vol_score = 20
            vol_label = "🟢 High"
        elif volume >= 100_000:
            vol_score = 15
            vol_label = "🟡 Moderate"
        elif volume >= 10_000:
            vol_score = 10
            vol_label = "🟠 Low"
        else:
            vol_score = 5
            vol_label = "🔴 Very Low"
    except (ValueError, TypeError):
        vol_score = 0
        vol_label = "⚪ Unknown"

    score += vol_score
    breakdown["volume"] = {"score": vol_score, "max": 25, "label": vol_label}

    # --- Security Score (max 30 points) ---
    sec_score = 30
    sec_issues = []

    if security_data:
        if str(security_data.get("is_honeypot")) == "1":
            sec_score -= 30
            sec_issues.append("Honeypot")
        if str(security_data.get("cannot_sell_all")) == "1":
            sec_score -= 15
            sec_issues.append("Cannot Sell")
        if str(security_data.get("is_blacklisted")) == "1":
            sec_score -= 15
            sec_issues.append("Blacklisted")
        if str(security_data.get("hidden_owner")) == "1":
            sec_score -= 10
            sec_issues.append("Hidden Owner")
        if str(security_data.get("owner_change_balance")) == "1":
            sec_score -= 10
            sec_issues.append("Owner Can Mint")

        sec_score = max(0, sec_score)

    if sec_score >= 25:
        sec_label = "🟢 Safe"
    elif sec_score >= 15:
        sec_label = "🟡 Caution"
    else:
        sec_label = "🔴 Dangerous"

    if sec_issues:
        sec_label += f" ({', '.join(sec_issues)})"

    score += sec_score
    breakdown["security"] = {"score": sec_score, "max": 30, "label": sec_label}

    # --- Market Activity Score (max 20 points) ---
    try:
        change_24h = float(token_data.get("price_change_24h", 0) or 0)
        if change_24h > 20:
            act_score = 20
            act_label = "🟢 Hot"
        elif change_24h > 5:
            act_score = 16
            act_label = "🟢 Active"
        elif change_24h > -5:
            act_score = 12
            act_label = "🟡 Stable"
        elif change_24h > -20:
            act_score = 8
            act_label = "🟠 Declining"
        else:
            act_score = 4
            act_label = "🔴 Dumping"
    except (ValueError, TypeError):
        act_score = 10
        act_label = "⚪ Unknown"

    score += act_score
    breakdown["activity"] = {"score": act_score, "max": 20, "label": act_label}

    # Overall rating
    if score >= 80:
        rating = "🟢 BULLISH"
    elif score >= 60:
        rating = "🟡 NEUTRAL"
    elif score >= 40:
        rating = "🟠 CAUTIOUS"
    else:
        rating = "🔴 AVOID"

    return {
        "total": score,
        "rating": rating,
        "breakdown": breakdown,
    }


@router.message(Command("score"))
async def cmd_score(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ <b>Usage:</b> /score &lt;contract_address&gt;\n\n"
            "Example:\n"
            "<code>/score So11111111111111111111111111111111111111112</code>"
        )
        return

    contract = args[1].strip()
    await message.answer("⚡ Calculating Alpha Score...")

    token_data = await get_token_info(contract)
    if not token_data:
        await message.answer("⚠️ Token not found on DexScreener.")
        return

    security_data = await check_token_security(contract)

    result = calculate_alpha_score(token_data, security_data)
    bd = result["breakdown"]

    text = (
        f"⚡ <b>AlphaPulse Score</b> ⚡\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📛 <b>{token_data['name']}</b> ({token_data['symbol']})\n\n"
        f"🏆 <b>Score: {result['total']}/100</b>\n"
        f"📊 <b>Rating: {result['rating']}</b>\n\n"
        f"━━━━ <b>Breakdown</b> ━━━━\n\n"
        f"💧 <b>Liquidity:</b> {bd['liquidity']['label']} "
        f"({bd['liquidity']['score']}/{bd['liquidity']['max']})\n"
        f"📊 <b>Volume:</b> {bd['volume']['label']} "
        f"({bd['volume']['score']}/{bd['volume']['max']})\n"
        f"🔒 <b>Security:</b> {bd['security']['label']} "
        f"({bd['security']['score']}/{bd['security']['max']})\n"
        f"📈 <b>Activity:</b> {bd['activity']['label']} "
        f"({bd['activity']['score']}/{bd['activity']['max']})\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>This is not financial advice. Always DYOR.</i>\n"
        f"⚡ Powered by AlphaPulse"
    )

    await message.answer(text)
