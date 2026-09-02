"""User-facing token scanner built on Whale Alpha's production market feed."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

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


def build_expert_alert_card(
    snapshot: TokenMarketSnapshot,
    *,
    score: float | None = None,
    risk_level: str | None = None,
    risk_flags: tuple[str, ...] | list[str] = (),
    age_minutes: float | None = None,
    market_regime: str | None = None,
    trend: str | None = None,
    detected_at: datetime | None = None,
) -> str:
    """Render Whale Alpha's compact Telegram intelligence card."""
    symbol = escape(snapshot.symbol or "UNKNOWN")
    name = escape(snapshot.name or "Unknown Token")
    mint = escape(snapshot.mint)
    dex = escape(getattr(snapshot, "dex_id", None) or "N/A")
    pair = escape(getattr(snapshot, "pair_address", None) or "N/A")
    age = _age(getattr(snapshot, "created_at_ms", None), int((detected_at or datetime.now(UTC)).timestamp() * 1000)) if age_minutes is None else f"{max(age_minutes, 0):.0f}m"
    mc = _money(snapshot.market_cap_usd)
    liq = _money(snapshot.liquidity_usd)
    vol = _money(snapshot.volume_1h_usd)
    buy_sell = snapshot.buys_5m + snapshot.sells_5m
    buy_ratio = (snapshot.buys_5m / buy_sell * 100) if buy_sell else 0.0
    risk = (risk_level or "UNKNOWN").upper()
    risk_icon = "🟢" if risk == "LOW" else "🟡" if risk == "MEDIUM" else "🔴" if risk == "HIGH" else "⚪"
    flags = list(risk_flags)[:3]
    warning = " · ".join(escape(f.replace("_", " ").title()) for f in flags) if flags else "No immediate market-data warnings"
    score_line = f"{score:.0f}/100" if score is not None else "N/A"
    regime_line = f"{escape(market_regime)} · {escape(trend or 'N/A')}" if market_regime else "N/A"
    price_change = f"{snapshot.price_change_1h_pct:+.1f}%"
    top_line = "⚠️ High concentration data unavailable"
    if score is not None and score >= 85:
        top_line = "💎 Strong Alpha Score"
    elif score is not None and score >= 75:
        top_line = "⚡ Elevated Alpha Score"

    return (
        f"🔷 <b>Whale Alpha · ${symbol}</b>\n"
        f"<i>{name}</i>\n\n"
        f"🚨 <i>{risk_icon} {escape(risk)} Risk</i>\n"
        f"⚡ Score: <b>{score_line}</b>\n"
        f"🌐 Market: <b>{regime_line}</b>\n\n"
        f"⏱ Age: <b>{escape(age)}</b>\n"
        f"💰 MC: <b>{escape(mc)}</b>\n"
        f"💧 Liq: <b>{escape(liq)}</b>\n"
        f"📊 Vol: <b>{escape(vol)}</b> [1h]\n"
        f"🧪 Fake: <b>N/A</b>\n\n"
        f"🦅 Dex: <b>{dex}</b>  ❌ Ads: <b>N/A</b> ⚡\n"
        f"⚡ Flow: <b>{snapshot.buys_5m}</b> buys / <b>{snapshot.sells_5m}</b> sells\n"
        f"👥 Hodls: <b>N/A</b> | Top: <b>N/A</b> ⚠️\n"
        f"└ High: <b>N/A</b>\n\n"
        f"📦 Bundles: <b>N/A</b>\n"
        f"🛠 Dev: <b>N/A</b>\n"
        f"│ Bundled: <b>N/A</b> 🤍 | Sold: <b>N/A</b> 🟢\n"
        f"└ Airdrop: <b>N/A</b> 🤍\n\n"
        f"💸 <b>{escape(price_change)}</b> from 1h market momentum!\n\n"
        f"🧠 <b>Alpha Read:</b> {escape(top_line)}\n"
        f"⚠️ <b>Risk Check:</b> {warning}\n\n"
        f"<code>{mint}</code>\n"
        f"📊 DEX: <code>{pair}</code>\n\n"
        f"<i>Whale Alpha • Live Token Intelligence</i>"
    )


def build_scan_card(snapshot: TokenMarketSnapshot, *, now_ms: int | None = None) -> str:
    return build_expert_alert_card(snapshot, detected_at=datetime.fromtimestamp((now_ms or int(datetime.now(UTC).timestamp() * 1000)) / 1000, tz=UTC))
