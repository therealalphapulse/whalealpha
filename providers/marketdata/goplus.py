import logging

from config.settings import GOPLUS_API
from providers.marketdata._resilience import get_json

logger = logging.getLogger("AlphaPulse.GoPlus")


def _is_on(value) -> bool:
    return str(value).lower() in {"1", "true", "yes", "enabled"}


def _status(value, default: str = "unknown") -> str:
    """
    GoPlus Solana fields are sometimes:
    - "1"
    - "0"
    - {"status": "1"}
    - {"status": "0", "authority": [...]}
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return "1" if value else "0"

    if isinstance(value, dict):
        for key in ("status", "value", "enabled"):
            if key in value:
                return str(value[key])
        return default

    return str(value)


def _to_percent(value):
    try:
        num = float(value)

        # Some APIs return 0.147, others return 14.7
        if 0 <= num <= 1:
            num *= 100

        return round(num, 2)
    except (ValueError, TypeError):
        return None


def _holder_stats(holders: list) -> tuple[float | None, float | None]:
    """
    Returns:
    - top holder %
    - top 10 holders %
    """
    if not isinstance(holders, list) or not holders:
        return None, None

    percentages = []

    for holder in holders:
        if not isinstance(holder, dict):
            continue

        percent = (
            holder.get("percent")
            or holder.get("percentage")
            or holder.get("pct")
        )

        parsed = _to_percent(percent)
        if parsed is not None:
            percentages.append(parsed)

    if not percentages:
        return None, None

    percentages.sort(reverse=True)

    top_holder = percentages[0]
    top_10 = round(sum(percentages[:10]), 2)

    return top_holder, top_10


def _extract_token_data(payload: dict, contract_address: str) -> dict | None:
    result = payload.get("result") or {}

    if isinstance(result, dict):
        lower = contract_address.lower()

        token_data = (
            result.get(contract_address)
            or result.get(lower)
        )

        if token_data:
            return token_data

        # Some GoPlus Solana responses return the token object directly
        solana_keys = {
            "mintable",
            "freezable",
            "metadata_mutable",
            "balance_mutable_authority",
            "holders",
            "holder_count",
            "creator_address",
        }

        if any(key in result for key in solana_keys):
            return result

    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            return first

    return None


def _normalize_token_security(token_data: dict) -> dict:
    holders_raw = token_data.get("holders") or []
    if not isinstance(holders_raw, list):
        holders_raw = []

    top_holder_percent, top_10_holder_percent = _holder_stats(holders_raw)

    holder_count = (
        token_data.get("holder_count")
        or token_data.get("holders_count")
        or token_data.get("total_holders")
        or token_data.get("holder_number")
        or ""
    )

    # If exact holder count is unavailable but GoPlus returns top holder list
    if not holder_count and holders_raw:
        holder_count = f"Top {len(holders_raw)} listed"

    return {
        # Solana-specific risks
        "mintable": _status(token_data.get("mintable")),
        "freezable": _status(token_data.get("freezable")),
        "metadata_mutable": _status(token_data.get("metadata_mutable")),
        "balance_mutable_authority": _status(token_data.get("balance_mutable_authority")),
        "closable": _status(token_data.get("closable")),
        "default_account_state_upgradable": _status(token_data.get("default_account_state_upgradable")),
        "transfer_fee": _status(token_data.get("transfer_fee")),
        "transfer_fee_upgradable": _status(token_data.get("transfer_fee_upgradable")),
        "non_transferable": _status(token_data.get("non_transferable")),
        "trusted_token": _status(token_data.get("trusted_token"), default="0"),

        # Holder / creator data
        "holder_count": str(holder_count) if holder_count else "",
        "top_holder_percent": top_holder_percent,
        "top_10_holder_percent": top_10_holder_percent,
        "creator_percent": _to_percent(token_data.get("creator_percent")),
        "creator_balance": str(token_data.get("creator_balance", "")),
        "creator_address": str(token_data.get("creator_address", "")),
        "owner_address": str(token_data.get("owner_address", "")),
        "total_supply": str(token_data.get("total_supply", "N/A")),

        # Keep old fields for compatibility with existing score/security code
        "is_honeypot": str(token_data.get("is_honeypot", "0")),
        "is_blacklisted": str(token_data.get("is_blacklisted", "0")),
        "is_whitelisted": str(token_data.get("is_whitelisted", "0")),
        "is_proxy": str(token_data.get("is_proxy", "0")),
        "owner_change_balance": str(token_data.get("owner_change_balance", "0")),
        "hidden_owner": str(token_data.get("hidden_owner", "0")),
        "selfdestruct": str(token_data.get("selfdestruct", "0")),
        "external_call": str(token_data.get("external_call", "0")),
        "cannot_sell_all": str(token_data.get("cannot_sell_all", "0")),
        "cannot_buy": str(token_data.get("cannot_buy", "0")),
        "trading_cooldown": str(token_data.get("trading_cooldown", "0")),
    }


async def check_token_security(contract_address: str) -> dict | None:
    """
    Run Solana token security analysis using GoPlus.

    Uses Solana-specific endpoint first:
    /api/v1/solana/token_security

    Falls back to older endpoint shape if needed.
    """
    endpoints = [
        f"{GOPLUS_API}/solana/token_security",
        f"{GOPLUS_API}/token_security/solana",
    ]

    params = {"contract_addresses": contract_address}

    for url in endpoints:
        try:
            payload = await get_json(url, params=params, cache_ttl_seconds=30, timeout_seconds=10)

            if payload is None:
                logger.warning(f"GoPlus fetch failed for {url}")
                continue

            token_data = _extract_token_data(payload, contract_address)

            if not token_data:
                continue

            return _normalize_token_security(token_data)

        except Exception as e:
            logger.error(f"GoPlus error for {contract_address}: {e}")
            continue

    logger.info(f"No GoPlus security data found for {contract_address}")

    # Additive fallback (AlphaPulse Provider Integration Task, 2026-08-19):
    # only reached after both GoPlus endpoint attempts above have already
    # failed. Purely additive -- no-ops (returns None immediately) unless
    # RUGCHECK_API_KEY is configured, and never runs before or instead of
    # GoPlus. See providers/marketdata/rugcheck.py.
    try:
        from providers.marketdata.rugcheck import check_token_security as _rugcheck_fallback

        fallback_data = await _rugcheck_fallback(contract_address)
        if fallback_data:
            logger.info(f"RugCheck fallback supplied security data for {contract_address}")
            return fallback_data
    except Exception as e:
        logger.error(f"RugCheck fallback error for {contract_address}: {e}")

    return None


def _flag_line(label: str, value, danger_text: str, safe_text: str = "Disabled / Clean") -> str:
    if str(value).lower() == "unknown":
        return f"⚪ <b>{label}:</b> Unknown"

    if _is_on(value):
        return f"🔴 <b>{label}:</b> {danger_text}"

    return f"✅ <b>{label}:</b> {safe_text}"


def format_security_report(data: dict, contract: str) -> str:
    """
    Format Solana-aware security report.
    """

    holder_count = data.get("holder_count") or "N/A"

    top_holder = data.get("top_holder_percent")
    top_10 = data.get("top_10_holder_percent")
    creator_percent = data.get("creator_percent")

    top_holder_text = f"{top_holder:.2f}%" if isinstance(top_holder, (int, float)) else "N/A"
    top_10_text = f"{top_10:.2f}%" if isinstance(top_10, (int, float)) else "N/A"
    creator_text = f"{creator_percent:.2f}%" if isinstance(creator_percent, (int, float)) else "N/A"

    trusted = data.get("trusted_token")
    trusted_line = "✅ Trusted Token: Yes" if _is_on(trusted) else "⚪ Trusted Token: Not confirmed"

    report = (
        f"🔒 <b>Security Report</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Contract:</b> <code>{contract[:20]}...</code>\n\n"

        f"🧬 <b>Solana Authority Checks</b>\n"
        f"{_flag_line('Mint Authority', data.get('mintable'), 'Minting may still be possible')}\n"
        f"{_flag_line('Freeze Authority', data.get('freezable'), 'Accounts may be frozen')}\n"
        f"{_flag_line('Mutable Metadata', data.get('metadata_mutable'), 'Metadata can be changed')}\n"
        f"{_flag_line('Balance Authority', data.get('balance_mutable_authority'), 'Balances may be mutable')}\n"
        f"{_flag_line('Closable', data.get('closable'), 'Token account may be closable')}\n"
        f"{_flag_line('Transfer Fee', data.get('transfer_fee'), 'Transfer fee exists')}\n"
        f"{_flag_line('Fee Upgradable', data.get('transfer_fee_upgradable'), 'Fee settings may change')}\n"
        f"{_flag_line('Non-transferable', data.get('non_transferable'), 'Transfers may be restricted')}\n"
        f"{trusted_line}\n\n"

        f"👥 <b>Holder Analysis</b>\n"
        f"👥 Holders: <b>{holder_count}</b>\n"
        f"🏦 Top Holder: <b>{top_holder_text}</b>\n"
        f"🏦 Top 10 Holders: <b>{top_10_text}</b>\n"
        f"🛠 Creator Share: <b>{creator_text}</b>\n\n"

        f"📦 <b>Total Supply:</b> {data.get('total_supply', 'N/A')}\n"
    )

    return report
