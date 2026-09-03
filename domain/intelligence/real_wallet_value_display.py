"""Real Wallet portfolio display enhancements.

Adds explicit SOL value, token-only value, and total wallet value in both
USDT and SOL-equivalent units without changing trading, balance, or pricing
logic. The existing portfolio renderer remains the source of per-token
USDT valuations.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger("AlphaPulse.RealWalletValueDisplay")

_WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
_INSTALLED = False


def install() -> None:
    global _INSTALLED
    if _INSTALLED or "app_platform.commands.real_wallet" not in sys.modules:
        return

    rw = sys.modules["app_platform.commands.real_wallet"]
    if getattr(rw, "_alphapulse_wallet_value_display", False):
        _INSTALLED = True
        return

    from domain.intelligence.wallet_portfolio import (
        enrich_wallet_tokens,
        fetch_wallet_fungible_tokens,
        format_usd,
        format_token_amount,
        build_wallet_portfolio_report as _original_report,
    )

    original_fetch = rw._fetch_portfolio_value_safe

    async def _fetch_portfolio_value_display(wallet_address: str):
        try:
            tokens = await fetch_wallet_fungible_tokens(wallet_address)
            if tokens is None:
                return None
            enriched = await enrich_wallet_tokens(tokens)
            sol = next((t for t in enriched if t.get("mint") == _WRAPPED_SOL_MINT or t.get("symbol") == "SOL"), None)
            sol_balance = float(sol.get("amount", 0.0)) if sol else 0.0
            sol_price = float(sol.get("price", 0.0)) if sol and sol.get("priced") else 0.0
            sol_value = float(sol.get("value", 0.0)) if sol and sol.get("priced") else 0.0
            token_value = sum(
                float(t.get("value", 0.0))
                for t in enriched
                if t is not sol and t.get("priced")
            )
            total_value = sol_value + token_value
            total_sol_equivalent = (total_value / sol_price) if sol_price > 0 else None
            return {
                "total_value_usd": total_value,
                "sol_balance": sol_balance,
                "sol_price_usd": sol_price,
                "sol_value_usd": sol_value,
                "token_value_usd": token_value,
                "total_sol_equivalent": total_sol_equivalent,
                "tokens": enriched,
            }
        except Exception:
            logger.exception("Real Wallet value display fetch failed; using existing portfolio fallback")
            try:
                fallback = await original_fetch(wallet_address)
                if fallback is None:
                    return None
                return {"total_value_usd": fallback}
            except Exception:
                return None

    def _menu_text_display(public_key: str, sol_balance: float | None, portfolio_value, auto_enabled: bool) -> str:
        auto_line = "🟢 ON" if auto_enabled else "⚪ OFF (manual trading only)"
        if isinstance(portfolio_value, dict):
            actual_sol = portfolio_value.get("sol_balance")
            sol_value = portfolio_value.get("sol_value_usd")
            token_value = portfolio_value.get("token_value_usd")
            total_usd = portfolio_value.get("total_value_usd")
            total_sol = portfolio_value.get("total_sol_equivalent")
            sol_line = f"{actual_sol:.4f} SOL" if actual_sol is not None else "—"
            sol_usdt_line = format_usd(sol_value) if sol_value is not None else "—"
            token_usdt_line = format_usd(token_value) if token_value is not None else "—"
            total_usdt_line = format_usd(total_usd) if total_usd is not None else "—"
            total_sol_line = f"{total_sol:.4f} SOL" if total_sol is not None else "—"
        else:
            sol_line = f"{sol_balance:.4f} SOL" if sol_balance is not None else "—"
            sol_usdt_line = "—"
            token_usdt_line = "—"
            total_usdt_line = format_usd(portfolio_value) if portfolio_value is not None else "—"
            total_sol_line = "—"

        return (
            "💼 <b>AlphaPulse Real Wallet</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👛 <b>Address</b> <i>(tap to copy)</i>:\n<code>{public_key}</code>\n\n"
            f"◎ <b>SOL Balance:</b> {sol_line}\n"
            f"💵 <b>SOL Value:</b> {sol_usdt_line} USDT\n"
            f"🪙 <b>Tokens Value:</b> {token_usdt_line} USDT\n"
            f"💰 <b>Total Wallet Value:</b> {total_usdt_line} USDT\n"
            f"   ≈ <b>{total_sol_line}</b>\n"
            f"🤖 <b>Automation:</b> {auto_line}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Manage your wallet below."
        )

    async def _portfolio_report_display(wallet_address: str, limit: int = 50, user_id: int | None = None) -> str:
        text = await _original_report(wallet_address, limit=limit, user_id=user_id)
        try:
            tokens = await fetch_wallet_fungible_tokens(wallet_address, limit=limit)
            if tokens is None:
                return text
            enriched = await enrich_wallet_tokens(tokens)
            sol = next((t for t in enriched if t.get("mint") == _WRAPPED_SOL_MINT or t.get("symbol") == "SOL"), None)
            sol_price = float(sol.get("price", 0.0)) if sol and sol.get("priced") else 0.0
            total_usd = sum(float(t.get("value", 0.0)) for t in enriched if t.get("priced"))
            total_sol = (total_usd / sol_price) if sol_price > 0 else None
            token_usd = sum(float(t.get("value", 0.0)) for t in enriched if t is not sol and t.get("priced"))
            summary = (
                f"◎ SOL Balance: <b>{float(sol.get('amount', 0.0)):.4f} SOL</b>\n"
                f"💵 SOL Value: <b>{format_usd(float(sol.get('value', 0.0))) if sol else 'N/A'} USDT</b>\n"
                f"🪙 Tokens Value: <b>{format_usd(token_usd)} USDT</b>\n"
                f"💰 Total Wallet Value: <b>{format_usd(total_usd)} USDT</b>"
                + (f" ≈ <b>{total_sol:.4f} SOL</b>\n" if total_sol is not None else "\n")
                + "━━━━━━━━━━━━━━━━━━━━━\n"
            )
            marker = "💼 <b>Real Wallet Portfolio</b>\n━━━━━━━━━━━━━━━━━━━━━\n"
            if marker in text:
                return text.replace(marker, marker + summary, 1)
        except Exception:
            logger.exception("Could not add SOL/USDT wallet summary to portfolio report")
        return text

    rw._fetch_portfolio_value_safe = _fetch_portfolio_value_display
    rw._menu_text = _menu_text_display
    rw._alphapulse_wallet_value_display = True

    # The existing portfolio report already renders every priced holding
    # individually in USDT. Wrap it only to add the explicit SOL/USDT total
    # summary; token-level values remain unchanged.
    import app_platform.commands.real_wallet as _rw_module
    _rw_module.build_wallet_portfolio_report = _portfolio_report_display
    _INSTALLED = True
    logger.info("[RealWallet] SOL + token USDT valuation display installed")
