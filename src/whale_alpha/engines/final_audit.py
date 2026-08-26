"""Mandatory final evidence audit and release controller for Whale Alpha."""
from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from solders.pubkey import Pubkey
from whale_alpha.config import Env
from whale_alpha.engines.reversal_hunter import (
    Candle, ReversalAnalysis, _authority_flags, _fetch_bitquery_flow, _fetch_holder_profile,
    _fetch_market_overview, _fetch_ohlcv, _fetch_risk_positions, _fetch_security,
    _fetch_smart_money_token_list, _fetch_top_holders, _fetch_top_traders, _flow_evidence,
    _holder_evidence, _security_flags, detect_dip_consolidation_breakout,
)
from whale_alpha.integrations.token_hunter_market import _dict, _number, _pair_liquidity
from whale_alpha.utils.http_retry import get_provider_client

STRATEGY_VERSION = "whale-alpha-reversal-v1"
RULES_VERSION = "final-audit-v1"
SCORING_MODEL_VERSION = "reversal-100-v1"
AUDIT_MODE = "SELF-AUDITED"
STALE_MARKET_SECONDS = 60
STALE_FLOW_SECONDS = 180
STALE_HOLDER_SECONDS = 300
STALE_SECURITY_SECONDS = 300

@dataclass(frozen=True)
class FinalAuditResult:
    audit_id: str
    analysis_id: str
    snapshot_id: str
    strategy_version: str
    rules_version: str
    scoring_model_version: str
    audit_mode: str
    approved: bool
    final_score: float
    final_tier: str
    findings: tuple[str, ...]
    corrections: tuple[str, ...]
    evidence: dict[str, Any]


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _metric(name: str, raw: Any, unit: str, source: str, endpoint: str, token: str, pair: str | None, quote: str | None, fetched: datetime, observed: datetime | None, method: str, status: str, slot: int | None = None) -> dict[str, Any]:
    return {
        "metric_name": name, "raw_value": raw, "normalized_value": raw, "unit": unit,
        "source_name": source, "source_endpoint_or_category": endpoint, "token_address": token,
        "pair_address": pair, "quote_token": quote, "observed_at_utc": (observed or fetched).isoformat(),
        "fetched_at_utc": fetched.isoformat(), "blockchain_slot": slot, "calculation_method": method,
        "freshness_status": status, "validation_status": "VALIDATED" if status == "FRESH" else status,
    }


def _parse_mint_account(account: Any) -> tuple[int | None, float | None, tuple[str, ...]]:
    try:
        value = account.value if hasattr(account, "value") else account
        if value is None:
            return None, None, ("AUTHORITY_DATA_MISSING",)
        data = value.data if hasattr(value, "data") else value.get("data")
        if isinstance(data, tuple):
            raw = base64.b64decode(data[0])
        elif isinstance(data, list):
            raw = base64.b64decode(data[0])
        else:
            return None, None, ("AUTHORITY_DATA_MISSING",)
        if len(raw) < 82:
            return None, None, ("AUTHORITY_DATA_MISSING",)
        decimals = int(raw[44])
        supply = float(int.from_bytes(raw[36:44], "little"))
        return decimals, supply, tuple(_authority_flags(account))
    except Exception:
        return None, None, ("AUTHORITY_DATA_MISSING",)



async def _fresh_dex_pair(client: httpx.AsyncClient, env: Env, mint: str, pair_address: str | None) -> dict[str, Any] | None:
    provider = get_provider_client("whale_alpha_audit_dex", max_concurrency=1, failure_threshold=1, cooldown_seconds=30)
    result = await provider.get(client, f"{env.DISCOVERY_DEXSCREENER_API_BASE}/tokens/v1/solana/{mint}", max_retries=env.DISCOVERY_PROVIDER_MAX_RETRIES, base_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_BASE_SECONDS, max_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_MAX_SECONDS)
    if result.response is None or result.response.status_code >= 400:
        return None
    try:
        rows = result.response.json()
    except (ValueError, TypeError):
        return None
    if not isinstance(rows, list):
        return None
    matches = [r for r in rows if isinstance(r, dict) and r.get("chainId") == "solana" and _dict(r.get("baseToken")).get("address") == mint]
    if pair_address:
        matches = [r for r in matches if r.get("pairAddress") == pair_address]
    if not matches:
        return None
    return max(matches, key=lambda r: _pair_liquidity(r))


def _closed(candles: list[Candle], now_ts: int) -> list[Candle]:
    return [c for c in candles if c.ts + 300 <= now_ts]


def _score(pattern: Any, flow: Any, onchain: Any, liquidity_quality: float, smart_quality: float) -> float:
    price = min(100.0, 55 + min(pattern.dip_pct, 50) * 0.8 + min(pattern.breakout_volume_5m_mult, 4) * 4 + min(pattern.breakout_volume_15m_mult, 4) * 4)
    holder = max(0.0, 100 - onchain.top10_pct * 1.8 - onchain.largest_wallet_pct * 2.0 - onchain.dev_hold_pct * 2.0 - onchain.tagged_risk_pct * 0.8)
    return round(price * 0.30 + liquidity_quality * 0.20 + holder * 0.20 + smart_quality * 0.15 + 100.0 * 0.15, 2)


def _tier(score: float) -> str:
    if score >= 93: return "HIGH-CONVICTION WATCH"
    if score >= 87: return "STRONG WATCH"
    if score >= 80: return "EARLY WATCH"
    return "NO SIGNAL"


async def run_final_release_audit(client: httpx.AsyncClient, env: Env, analysis: ReversalAnalysis, connection: Any | None, now: datetime) -> FinalAuditResult:
    audit_id, analysis_id, snapshot_id = _id("audit"), _id("analysis"), _id("snapshot")
    mint = analysis.snapshot.mint
    pair = analysis.snapshot.pair_address
    findings: list[str] = list(analysis.hard_rejects)
    corrections: list[str] = []
    fetched = datetime.now(UTC)
    evidence: dict[str, Any] = {
        "analysis_id": analysis_id, "snapshot_id": snapshot_id, "audit_id": audit_id,
        "strategy_version": STRATEGY_VERSION, "rules_version": RULES_VERSION,
        "scoring_model_version": SCORING_MODEL_VERSION, "audit_mode": AUDIT_MODE,
        "token_address": mint, "pair_address": pair, "chain": "solana",
    }

    # Phase B/C: fresh data fetch and independent recalculation.
    dex = await _fresh_dex_pair(client, env, mint, pair)
    overview, trades, candles, profile, holders, positions, top_traders, security, bitquery, smart_rows = await __import__("asyncio").gather(
        _fetch_market_overview(client, env, mint), _fetch_trade_data(client, env, mint), _fetch_ohlcv(client, env, mint, now),
        _fetch_holder_profile(client, env, mint), _fetch_top_holders(client, env, mint), _fetch_risk_positions(client, env, mint),
        _fetch_top_traders(client, env, mint), _fetch_security(client, env, mint), _fetch_bitquery_flow(client, env, mint, pair),
        _fetch_smart_money_token_list(client, env),
    )
    if dex is None: findings.append("FRESH_DEX_PAIR_DATA_MISSING")
    if overview is None or trades is None: findings.append("FRESH_MARKET_DATA_MISSING")
    if not candles: findings.append("FRESH_OHLCV_MISSING")
    if profile is None or not holders or not positions: findings.append("FRESH_HOLDER_DATA_MISSING")
    if security is None: findings.append("FRESH_SECURITY_DATA_MISSING")
    if bitquery is None: findings.append("FRESH_BITQUERY_FLOW_MISSING")
    if smart_rows is None: findings.append("FRESH_SMART_MONEY_DATA_MISSING")

    # Identity / pair reconciliation.
    quote = None
    if dex:
        base = _dict(dex.get("baseToken")); q = _dict(dex.get("quoteToken")); quote = q.get("address") if isinstance(q.get("address"), str) else None
        if dex.get("chainId") != "solana": findings.append("CHAIN_ID_MISMATCH")
        if dex.get("pairAddress") != pair: findings.append("PAIR_IDENTITY_CHANGED")
        if base.get("address") != mint: findings.append("TOKEN_IDENTITY_CHANGED")
        if not quote: findings.append("QUOTE_TOKEN_MISSING")
        if not dex.get("dexId"): findings.append("DEX_VENUE_MISSING")
        if dex.get("marketCap") is None: findings.append("VERIFIED_MARKET_CAP_MISSING")
        if dex.get("marketCap") is not None and dex.get("fdv") is not None and dex.get("marketCap") == dex.get("fdv") and dex.get("marketCap") != 0:
            # Equal MC/FDV is not inherently wrong; keep as evidence rather than rejection.
            corrections.append("Market cap and FDV were kept as separate fields; no FDV substitution is permitted.")
    else:
        findings.append("IDENTITY_UNVERIFIED")

    decimals = supply = None
    authority_flags: tuple[str, ...] = ("AUTHORITY_DATA_MISSING",)
    slot = None
    if connection is not None:
        try:
            ai = await connection.get_account_info(Pubkey.from_string(mint))
            slot = getattr(ai, "context", None)
            slot = getattr(slot, "slot", None)
            decimals, supply, authority_flags = _parse_mint_account(ai)
        except Exception:
            findings.append("AUTHORITY_READ_FAILED")
    else:
        findings.append("AUTHORITY_DATA_MISSING")
    findings.extend(x for x in authority_flags if x != "")
    if decimals is None or supply is None: findings.append("SUPPLY_DECIMALS_UNVERIFIED")
    if not dex or not dex.get("marketCap"): findings.append("MARKET_CAP_UNVERIFIED")

    # Freshness: current market data must be recent; on-chain state is observed at fetch time.
    last_trade = None
    if overview:
        for k in ("lastTradeUnixTime", "last_trade_unix_time", "lastTradeTime"):
            v = overview.get(k)
            if v is not None:
                try: last_trade = int(float(v)); break
                except (TypeError, ValueError): pass
    if last_trade is not None and int(fetched.timestamp()) - last_trade > STALE_MARKET_SECONDS: findings.append("MARKET_DATA_STALE")
    if last_trade is None: findings.append("MARKET_OBSERVATION_TIME_MISSING")

    closed = _closed(candles, int(fetched.timestamp()))
    pattern = detect_dip_consolidation_breakout(closed, int(fetched.timestamp())) if closed else None
    if pattern is None: findings.append("AUDIT_PATTERN_RECALCULATION_FAILED")
    elif not pattern.breakout_confirmed: findings.append("BREAKOUT_NOT_CONFIRMED")

    flow_raw = None
    # Reuse only freshly fetched flow inputs; this recomputes ratios independently of the draft score.
    from whale_alpha.engines.reversal_hunter import _flow_from_trade_data
    flow_raw = _flow_from_trade_data(trades)
    flow = _flow_evidence(flow_raw, closed, smart_rows or [], top_traders or [], mint, bitquery) if flow_raw is not None and bitquery is not None else None
    if flow is None: findings.append("AUDIT_FLOW_RECALCULATION_FAILED")
    else:
        if flow.buy_sell_ratio < env.WHALE_ALPHA_BUY_SELL_RATIO_MIN or not flow.net_buy_pressure: findings.append("AUDIT_NET_BUY_PRESSURE_FAILED")
        if flow.volume_5m <= 0 or flow.volume_15m <= 0: findings.append("AUDIT_VOLUME_MISSING")
        if flow.top_trader_status == "SELLING": findings.append("TOP_TRADER_DISTRIBUTION")
        if flow.smart_money_status == "SELLING": findings.append("SMART_MONEY_DISTRIBUTION")
        if flow_raw and bitquery:
            bq_ratio = (bitquery[0] / bitquery[1]) if bitquery[1] > 0 else float("inf") if bitquery[0] > 0 else 0.0
            primary_ratio = (flow_raw[2] / flow_raw[3]) if flow_raw[3] > 0 else float("inf") if flow_raw[2] > 0 else 0.0
            if (primary_ratio >= env.WHALE_ALPHA_BUY_SELL_RATIO_MIN) != (bq_ratio >= env.WHALE_ALPHA_BUY_SELL_RATIO_MIN):
                findings.append("BUY_SELL_SOURCE_CONFLICT")

    on_raw = _holder_evidence(profile, holders, positions)
    security_flags = _security_flags(security)
    if on_raw is None: findings.append("AUDIT_HOLDER_RECALCULATION_FAILED")
    else:
        if on_raw[0] > env.WHALE_ALPHA_MAX_TOP10_PCT or on_raw[1] > env.WHALE_ALPHA_MAX_SINGLE_WALLET_PCT or on_raw[2] > env.WHALE_ALPHA_MAX_DEV_HOLD_PCT or on_raw[3] > env.WHALE_ALPHA_MAX_TAGGED_RISK_PCT:
            findings.append("CONCENTRATION_HARD_REJECT")
        if not on_raw[4]: findings.append("TAGGED_RISK_WALLETS_NET_DISTRIBUTING")
        if analysis.onchain and (abs(on_raw[0]-analysis.onchain.top10_pct) > 2 or abs(on_raw[1]-analysis.onchain.largest_wallet_pct) > 2):
            findings.append("HOLDER_CONCENTRATION_SOURCE_CONFLICT")
    findings.extend(security_flags)

    # Supply methodology and zero-tolerance supply reconciliation.
    if overview:
        circ = next((_number(overview.get(k)) for k in ("circulatingSupply", "circulating_supply") if _number(overview.get(k)) is not None), None)
        if circ is None: findings.append("CIRCULATING_SUPPLY_METHOD_UNKNOWN")
        provider_supply = next((_number(overview.get(k)) for k in ("totalSupply", "total_supply", "supply") if _number(overview.get(k)) is not None), None)
        if provider_supply is not None and supply is not None and provider_supply != supply:
            findings.append("SUPPLY_SOURCE_CONFLICT")
    else:
        findings.append("CIRCULATING_SUPPLY_METHOD_UNKNOWN")

    # Source reconciliation against the original draft and fresh provider measurements.
    if dex:
        fresh_price = _number(dex.get("priceUsd")); fresh_liq = _pair_liquidity(dex); fresh_mc = _number(dex.get("marketCap"))
        old_price, old_liq, old_mc = analysis.snapshot.price_usd, analysis.snapshot.liquidity_usd, analysis.snapshot.market_cap_usd
        if fresh_price is None or fresh_liq <= 0 or fresh_mc is None: findings.append("FRESH_MARKET_FIELDS_INVALID")
        if fresh_price and old_price and abs(fresh_price-old_price)/old_price > 0.02: findings.append("PRICE_SOURCE_CONFLICT")
        if fresh_liq and old_liq and abs(fresh_liq-old_liq)/old_liq > 0.05: findings.append("LIQUIDITY_SOURCE_CONFLICT")
        if fresh_mc and old_mc and abs(fresh_mc-old_mc)/old_mc > 0.10: findings.append("MARKET_CAP_SOURCE_CONFLICT")
        if overview:
            birdeye_mc = next((_number(overview.get(k)) for k in ("marketCap","market_cap","market_cap_usd") if _number(overview.get(k)) is not None), None)
            if birdeye_mc and fresh_mc and abs(birdeye_mc-fresh_mc)/fresh_mc > 0.10: findings.append("MARKET_CAP_PROVIDER_CONFLICT")
            birdeye_liq = next((_number(overview.get(k)) for k in ("liquidity","liquidity_usd") if _number(overview.get(k)) is not None), None)
            if birdeye_liq and fresh_liq and abs(birdeye_liq-fresh_liq)/fresh_liq > 0.05: findings.append("LIQUIDITY_PROVIDER_CONFLICT")

        if fresh_liq > 0 and fresh_mc and not (env.WHALE_ALPHA_MIN_LIQ_MC_RATIO <= fresh_liq/fresh_mc <= env.WHALE_ALPHA_MAX_LIQ_MC_RATIO): findings.append("LIQUIDITY_MC_RATIO_OUT_OF_BAND")
        if fresh_mc and not (env.WHALE_ALPHA_MIN_MC_USD <= fresh_mc <= env.WHALE_ALPHA_MAX_MC_USD): findings.append("MARKET_CAP_OUT_OF_BAND")
        if fresh_liq and not (env.WHALE_ALPHA_MIN_LIQ_USD <= fresh_liq <= env.WHALE_ALPHA_MAX_LIQ_USD): findings.append("LIQUIDITY_OUT_OF_BAND")
        pair_created = dex.get("pairCreatedAt")
        if pair_created:
            age_h = (fetched.timestamp()*1000 - float(pair_created))/3_600_000
            if age_h < env.WHALE_ALPHA_MIN_PAIR_AGE_HOURS or age_h > env.WHALE_ALPHA_MAX_PAIR_AGE_DAYS*24: findings.append("PAIR_AGE_OUT_OF_BAND")
        else: findings.append("PAIR_AGE_DATA_MISSING")
        # Volume-quality hard checks.
        if flow and fresh_liq > 0:
            if flow.volume_5m / fresh_liq > env.WHALE_ALPHA_MAX_5M_VOLUME_TO_LIQUIDITY: findings.append("VOLUME_TOO_LARGE_FOR_LIQUIDITY")
            if flow.volume_15m / fresh_liq > env.WHALE_ALPHA_MAX_15M_VOLUME_TO_LIQUIDITY: findings.append("VOLUME_TOO_LARGE_FOR_LIQUIDITY")

    if dex and overview:
        # Token symbol/name must agree when both providers expose them.
        base = _dict(dex.get("baseToken"))
        for key, expected in (("symbol", analysis.snapshot.symbol), ("name", analysis.snapshot.name)):
            actual = base.get(key)
            if actual and expected and str(actual) != str(expected): findings.append(f"{key.upper()}_IDENTITY_CONFLICT")

    if analysis.hard_rejects:
        corrections.append("Primary draft contained hard rejects; release remains blocked regardless of score.")
    if analysis.score != 0 and pattern and analysis.pattern and pattern != analysis.pattern:
        corrections.append("Market-structure evidence was independently recalculated from freshly fetched closed candles.")

    smart_quality = 100 if flow and flow.smart_money_status == "SUPPORTIVE" and flow.top_trader_status == "SUPPORTIVE" else 85 if flow and flow.smart_money_status == "SUPPORTIVE" else 75 if flow and flow.top_trader_status == "SUPPORTIVE" else 55
    liq_quality = 100.0 if dex and _pair_liquidity(dex) > 0 and _number(dex.get("marketCap")) and env.WHALE_ALPHA_MIN_LIQ_MC_RATIO <= _pair_liquidity(dex)/_number(dex.get("marketCap")) <= env.WHALE_ALPHA_MAX_LIQ_MC_RATIO else 0.0
    final_score = _score(pattern, flow, type("OnChain", (), {"top10_pct": on_raw[0], "largest_wallet_pct": on_raw[1], "dev_hold_pct": on_raw[2], "tagged_risk_pct": on_raw[3]})(), liq_quality, smart_quality) if pattern and flow and on_raw else 0.0
    if final_score < env.WHALE_ALPHA_MIN_CONFIDENCE: findings.append("CONFIDENCE_BELOW_THRESHOLD")

    evidence["metrics"] = {
        "price_usd": _metric("price_usd", _number(dex.get("priceUsd")) if dex else None, "USD", "DexScreener", "tokens/v1/solana/{mint}", mint, pair, quote, fetched, fetched, "direct_pair_price", "FRESH" if dex else "MISSING"),
        "market_cap_usd": _metric("market_cap_usd", _number(dex.get("marketCap")) if dex else None, "USD", "DexScreener", "tokens/v1/solana/{mint}", mint, pair, quote, fetched, fetched, "direct_marketCap_only_no_FDV_fallback", "FRESH" if dex and dex.get("marketCap") is not None else "MISSING"),
        "fdv_usd": _metric("fdv_usd", _number(dex.get("fdv")) if dex else None, "USD", "DexScreener", "tokens/v1/solana/{mint}", mint, pair, quote, fetched, fetched, "direct_fdv_field_separate_from_market_cap", "FRESH" if dex and dex.get("fdv") is not None else "MISSING"),
        "liquidity_usd": _metric("liquidity_usd", _pair_liquidity(dex) if dex else None, "USD", "DexScreener", "tokens/v1/solana/{mint}", mint, pair, quote, fetched, fetched, "pair_liquidity_usd", "FRESH" if dex else "MISSING"),
        "decimals": _metric("decimals", decimals, "integer", "Solana", "mint_account", mint, pair, quote, fetched, fetched, "SPL_Mint_layout_offset_44", "FRESH" if decimals is not None else "MISSING", slot),
        "total_supply": _metric("total_supply", supply, "base_units", "Solana", "mint_account", mint, pair, quote, fetched, fetched, "SPL_Mint_layout_supply_offset_36", "FRESH" if supply is not None else "MISSING", slot),
        "top10_holder_pct": _metric("top10_holder_pct", on_raw[0] if on_raw else None, "%", "Birdeye", "holder-profile/holder", mint, pair, quote, fetched, fetched, "fresh_holder_recalculation", "FRESH" if on_raw else "MISSING"),
        "buy_sell_ratio": _metric("buy_sell_ratio", flow.buy_sell_ratio if flow else None, "ratio", "Birdeye+Bitquery", "trade-data+DEXTradeByTokens", mint, pair, quote, fetched, fetched, "fresh_cross_source_flow_recalculation", "FRESH" if flow else "MISSING"),
    }
    if evidence["metrics"].get("total_supply") and supply is not None and decimals is not None:
        evidence["metrics"]["total_supply"]["normalized_value"] = supply / (10 ** decimals)
        evidence["metrics"]["total_supply"]["unit"] = "tokens"
    evidence["circulating_supply_method"] = "provider_defined_marketCap" if dex and dex.get("marketCap") is not None else None
    evidence["market_cap_method"] = "DexScreener marketCap field; FDV is never substituted"
    evidence["pattern"] = pattern.__dict__ if pattern else None
    evidence["flow"] = flow.__dict__ if flow else None
    evidence["onchain"] = {"holder": on_raw, "security_flags": security_flags, "authority_flags": authority_flags}
    evidence["release_gate"] = {"findings": sorted(set(findings)), "corrections": corrections}
    approved = not findings and final_score >= env.WHALE_ALPHA_MIN_CONFIDENCE and pattern is not None and flow is not None and on_raw is not None and dex is not None
    return FinalAuditResult(audit_id, analysis_id, snapshot_id, STRATEGY_VERSION, RULES_VERSION, SCORING_MODEL_VERSION, AUDIT_MODE, approved, final_score, _tier(final_score) if approved else "NO SIGNAL", tuple(sorted(set(findings))), tuple(dict.fromkeys(corrections)), evidence)
