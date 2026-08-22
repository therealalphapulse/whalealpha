"""User-facing token scanner built on Whale Alpha's production market feed."""

from __future__ import annotations

from datetime import UTC, datetime

from whale_alpha.integrations.token_hunter_market import TokenMarketSnapshot


def _money(value: float | None) -> str:
    if value is None:
        return "N/A"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


def _price(value: float | None) -> str:
    if value is None:
        return "N/A"
    if value >= 1:
        return f"${value:,.4f}"
    if value >= 0.01:
        return f"${value:.6f}"
    return f"${value:.10f}".rstrip("0")


def _age(created_at_ms: int | None, now_ms: int) -> str:
    if not created_at_ms:
        return "Unknown"
    minutes = max(0, (now_ms - created_at_ms) / 60_000)
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _risk_flags(snapshot: TokenMarketSnapshot) -> list[str]:
    flags: list[str] = []
    liquidity = snapshot.liquidity_usd or 0
    if liquidity <= 0:
        flags.append("No tracked liquidity")
    elif liquidity < 5_000:
        flags.append("Very low liquidity")
    elif liquidity < 10_000:
        flags.append("Low liquidity")
    if snapshot.volume_5m_usd <= 0 or snapshot.buys_5m + snapshot.sells_5m == 0:
        flags.append("No recent 5m activity")
    elif snapshot.sells_5m > snapshot.buys_5m * 2:
        flags.append("Heavy 5m sell pressure")
    if not snapshot.metadata_present:
        flags.append("Limited token metadata")
    return flags


def build_scan_card(snapshot: TokenMarketSnapshot, *, now_ms: int | None = None) -> str:
    if now_ms is None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
    symbol = snapshot.symbol or "UNKNOWN"
    name = snapshot.name or "Unknown Token"
    flags = _risk_flags(snapshot)
    risk = "🟢 HEALTHY" if not flags else ("🟠 CAUTION" if len(flags) < 3 else "🔴 HIGH RISK")
    net_5m = snapshot.buys_5m - snapshot.sells_5m
    net_1h = snapshot.buys_1h - snapshot.sells_1h
    return (
        "🔎 <b>WHALE ALPHA • TOKEN SCANNER</b>\n"
        "<i>Live market intelligence • no signal filter applied</i>\n\n"
        f"🪙 <b>${symbol}</b> — {name}\n"
        f"🎯 Status: {risk}\n"
        f"⏱ Age: {_age(snapshot.created_at_ms, now_ms)}\n\n"
        "📊 <b>MARKET SNAPSHOT</b>\n"
        f"• Price: <code>{_price(snapshot.price_usd)}</code>\n"
        f"• Market cap: <b>{_money(snapshot.market_cap_usd)}</b>\n"
        f"• Liquidity: <b>{_money(snapshot.liquidity_usd)}</b>\n"
        f"• Volume 5m: <b>{_money(snapshot.volume_5m_usd)}</b>\n"
        f"• Volume 1h: <b>{_money(snapshot.volume_1h_usd)}</b>\n\n"
        "📈 <b>MOMENTUM</b>\n"
        f"• 5m: <b>{snapshot.price_change_5m_pct:+.2f}%</b>\n"
        f"• 1h: <b>{snapshot.price_change_1h_pct:+.2f}%</b>\n"
        f"• 5m flow: {snapshot.buys_5m} buys / {snapshot.sells_5m} sells ({net_5m:+d})\n"
        f"• 1h flow: {snapshot.buys_1h} buys / {snapshot.sells_1h} sells ({net_1h:+d})\n\n"
        "🧠 <b>RISK CHECK</b>\n"
        + ("• No immediate market-data warnings\n" if not flags else "".join(f"• {flag}\n" for flag in flags))
        + "\n"
        + f"🔗 DEX: <b>{snapshot.dex_id or 'N/A'}</b>\n"
        + f"🔐 Mint: <code>{snapshot.mint}</code>\n"
        + f"💧 Pair: <code>{snapshot.pair_address or 'N/A'}</code>\n\n"
        "⚠️ <i>Scanner data is market intelligence, not a buy recommendation.</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Whale Alpha • Token Intelligence"
    )
