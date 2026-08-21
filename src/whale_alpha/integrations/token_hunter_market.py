"""DexScreener enrichment for the token hunter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import httpx

from whale_alpha.config import Env
from whale_alpha.utils.http_retry import TTLCache, get_provider_client
from whale_alpha.utils.logger import child_logger

log = child_logger("tokenHunterMarket")
_cache: TTLCache[dict[str, Any]] = TTLCache(ttl_seconds=20, max_entries=512)


@dataclass(frozen=True)
class TokenMarketSnapshot:
    mint: str
    name: str | None
    symbol: str | None
    pair_address: str | None
    dex_id: str | None
    created_at_ms: int | None
    price_usd: float | None
    market_cap_usd: float | None
    liquidity_usd: float | None
    volume_5m_usd: float
    volume_1h_usd: float
    buys_5m: int
    sells_5m: int
    buys_1h: int
    sells_1h: int
    price_change_5m_pct: float
    price_change_1h_pct: float
    metadata_present: bool
    source: str = "dexscreener"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _pair_liquidity(pair: dict[str, Any]) -> float:
    liquidity = pair.get("liquidity")
    value = _number(liquidity.get("usd"), 0.0) if isinstance(liquidity, dict) else 0.0
    return value if value is not None else 0.0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _select_pair(pairs: list[Any], mint: str) -> dict[str, Any] | None:
    solana = [
        p
        for p in pairs
        if isinstance(p, dict)
        and p.get("chainId") == "solana"
        and (p.get("baseToken") or {}).get("address") == mint
    ]
    if not solana:
        solana = [
            p
            for p in pairs
            if isinstance(p, dict)
            and p.get("chainId") == "solana"
            and (
                ((p.get("baseToken") or {}).get("address") == mint)
                or ((p.get("quoteToken") or {}).get("address") == mint)
            )
        ]
    return max(solana, key=_pair_liquidity) if solana else None


async def enrich_tokens(
    client: httpx.AsyncClient, env: Env, mints: list[str]
) -> dict[str, TokenMarketSnapshot]:
    """Batch-enrich up to 30 Solana mints per DEX Screener request."""
    if not env.DISCOVERY_DEXSCREENER_ENABLED:
        return {}
    unique = list(dict.fromkeys(mints))[:30]
    if not unique:
        return {}
    cached: dict[str, TokenMarketSnapshot] = {}
    missing: list[str] = []
    for mint in unique:
        hit = _cache.get(mint)
        if hit is not None:
            cached[mint] = TokenMarketSnapshot(**hit)
        else:
            missing.append(mint)
    if not missing:
        return cached
    provider = get_provider_client(
        "dexscreener",
        max_concurrency=env.TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY,
        failure_threshold=env.DISCOVERY_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds=env.DISCOVERY_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
    )
    result = await provider.get(
        client,
        f"{env.DISCOVERY_DEXSCREENER_API_BASE}/tokens/v1/solana/{','.join(missing)}",
        max_retries=env.DISCOVERY_PROVIDER_MAX_RETRIES,
        base_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_BASE_SECONDS,
        max_backoff_seconds=env.DISCOVERY_PROVIDER_RETRY_MAX_SECONDS,
    )
    if result.response is None or result.response.status_code >= 400:
        return cached
    try:
        payload = result.response.json()
    except ValueError:
        return cached
    pairs = payload if isinstance(payload, list) else []
    by_mint: dict[str, list[Any]] = {mint: [] for mint in missing}
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        base = _dict(pair.get("baseToken"))
        mint_value = base.get("address")
        if isinstance(mint_value, str) and mint_value in by_mint:
            by_mint[mint_value].append(pair)
    for mint, mint_pairs in by_mint.items():
        pair = _select_pair(mint_pairs, mint)
        if pair is None:
            continue
        base = _dict(pair.get("baseToken"))
        txns = _dict(pair.get("txns"))
        volume = _dict(pair.get("volume"))
        changes = _dict(pair.get("priceChange"))
        info = _dict(pair.get("info"))
        socials = info.get("socials") or []
        websites = info.get("websites") or []
        m5 = _dict(txns.get("m5"))
        h1 = _dict(txns.get("h1"))
        snapshot = TokenMarketSnapshot(
            mint=mint,
            name=base.get("name") if isinstance(base.get("name"), str) else None,
            symbol=base.get("symbol") if isinstance(base.get("symbol"), str) else None,
            pair_address=pair.get("pairAddress") if isinstance(pair.get("pairAddress"), str) else None,
            dex_id=pair.get("dexId") if isinstance(pair.get("dexId"), str) else None,
            created_at_ms=_int(pair.get("pairCreatedAt")) or None,
            price_usd=_number(pair.get("priceUsd")),
            market_cap_usd=_number(pair.get("marketCap"), _number(pair.get("fdv"))),
            liquidity_usd=_pair_liquidity(pair),
            volume_5m_usd=_number(volume.get("m5"), 0.0) or 0.0,
            volume_1h_usd=_number(volume.get("h1"), 0.0) or 0.0,
            buys_5m=_int(m5.get("buys")),
            sells_5m=_int(m5.get("sells")),
            buys_1h=_int(h1.get("buys")),
            sells_1h=_int(h1.get("sells")),
            price_change_5m_pct=_number(changes.get("m5"), 0.0) or 0.0,
            price_change_1h_pct=_number(changes.get("h1"), 0.0) or 0.0,
            metadata_present=bool(socials or websites or base.get("name") or base.get("symbol")),
        )
        _cache.set(mint, snapshot.as_dict())
        cached[mint] = snapshot
    return cached


async def enrich_token(client: httpx.AsyncClient, env: Env, mint: str) -> TokenMarketSnapshot | None:
    return (await enrich_tokens(client, env, [mint])).get(mint)
