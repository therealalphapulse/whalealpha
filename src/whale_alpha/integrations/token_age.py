"""Production-safe Solana token creation-time resolution."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from solders.pubkey import Pubkey

from whale_alpha.config import Env
from whale_alpha.integrations.token_hunter_market import enrich_tokens
from whale_alpha.integrations.solana_connection import _rate_limited_rpc_call
from whale_alpha.utils.logger import child_logger

log = child_logger("tokenAge")


@dataclass(frozen=True)
class TokenAgeResolution:
    created_at_ms: int | None
    source: str
    age_seconds: float | None


def parse_timestamp_ms(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            raw = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                raw = float(text)
            except ValueError:
                normalized = text.replace("Z", "+00:00")
                dt = datetime.fromisoformat(normalized)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return int(dt.astimezone(UTC).timestamp() * 1000)
        else:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    if raw <= 0:
        return None
    return int(raw if raw >= 10**12 else raw * 1000)


def calculate_age_seconds(created_at_ms: int | None, now: datetime) -> float | None:
    if created_at_ms is None:
        return None
    created = datetime.fromtimestamp(created_at_ms / 1000, tz=UTC)
    age = (now.astimezone(UTC) - created).total_seconds()
    if age < 0:
        return None
    return age


def _provider_timestamp(snapshot: Any, now: datetime) -> int | None:
    created = parse_timestamp_ms(snapshot.created_at_ms)
    if created is None:
        return None
    if calculate_age_seconds(created, now) is None:
        return None
    return created


async def _onchain_first_seen(connection: Any, mint: str) -> int | None:
    """Best-effort first-seen timestamp from bounded RPC signature history.

    The mint account's oldest retained signature is used as a conservative
    first-seen proxy. We never invent a timestamp when RPC has no usable
    signature/block time.
    """
    pubkey = Pubkey.from_string(mint)
    before = None
    oldest = None
    for _ in range(3):
        response = await _rate_limited_rpc_call(
            connection.get_signatures_for_address,
            pubkey,
            before=before,
            limit=1000,
            min_interval_seconds=0.12,
            max_retries=2,
        )
        values = list(response.value or [])
        if not values:
            break
        oldest = values[-1]
        if len(values) < 1000:
            break
        before = oldest.signature
    if oldest is None:
        return None
    block_time = getattr(oldest, "block_time", None)
    if block_time is None:
        try:
            block_time_response = await _rate_limited_rpc_call(
                connection.get_block_time,
                oldest.slot,
                min_interval_seconds=0.12,
                max_retries=2,
            )
            block_time = block_time_response.value
        except Exception as err:  # noqa: BLE001
            log.debug("On-chain token age block time lookup failed", mint=mint, err=str(err))
            return None
    return parse_timestamp_ms(block_time)


async def resolve_token_ages(
    client: Any,
    env: Env,
    candidates: list[Any],
    now: datetime,
    connection: Any | None = None,
) -> dict[str, TokenAgeResolution]:
    """Resolve ages in provider -> DexScreener -> on-chain order."""
    resolutions: dict[str, TokenAgeResolution] = {}
    missing: list[str] = []
    for candidate in candidates:
        mint = candidate.snapshot.mint
        created = _provider_timestamp(candidate.snapshot, now)
        if created is None:
            missing.append(mint)
            continue
        age = calculate_age_seconds(created, now)
        resolutions[mint] = TokenAgeResolution(created, "provider", age)

    dex_snapshots = await enrich_tokens(client, env, missing) if missing else {}
    still_missing: list[str] = []
    for mint in missing:
        snapshot = dex_snapshots.get(mint)
        created = _provider_timestamp(snapshot, now) if snapshot is not None else None
        if created is None:
            still_missing.append(mint)
            continue
        age = calculate_age_seconds(created, now)
        resolutions[mint] = TokenAgeResolution(created, "dexscreener", age)

    if connection is not None and still_missing:
        sem = asyncio.Semaphore(max(1, min(env.TOKEN_HUNTER_PROVIDER_MAX_CONCURRENCY, 5)))

        async def one(mint: str) -> tuple[str, int | None]:
            async with sem:
                try:
                    return mint, await _onchain_first_seen(connection, mint)
                except Exception as err:  # noqa: BLE001
                    log.debug("On-chain token age lookup failed", mint=mint, err=str(err))
                    return mint, None

        for mint, created in await asyncio.gather(*(one(mint) for mint in still_missing)):
            if created is None or calculate_age_seconds(created, now) is None:
                continue
            resolutions[mint] = TokenAgeResolution(created, "onchain", calculate_age_seconds(created, now))

    for candidate in candidates:
        resolution = resolutions.get(candidate.snapshot.mint)
        log.info(
            "TOKEN AGE RESOLUTION",
            mint=candidate.snapshot.mint,
            source=resolution.source if resolution else "unknown",
            created_at=datetime.fromtimestamp(resolution.created_at_ms / 1000, tz=UTC).isoformat()
            if resolution and resolution.created_at_ms
            else None,
            age_seconds=resolution.age_seconds if resolution else None,
            age_minutes=resolution.age_seconds / 60 if resolution and resolution.age_seconds is not None else None,
            result="PASS" if resolution else "UNKNOWN",
        )
    return resolutions
