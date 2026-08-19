"""In-memory whale-event buffer + ingestion — port of src/engines/monitor/walletMonitor.ts.

A production deployment with 500-1500 tracked wallets should back this with
Redis (sorted sets keyed by token_mint, scored by timestamp) rather than
process memory, so it survives restarts and works across horizontally-scaled
workers. Swap the implementation here without touching callers — carried over
verbatim as a TODO from the original.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from whale_alpha.db.models import TradeSide, WalletEvent, WalletStatus, WhaleWallet
from whale_alpha.engines.signal import WhaleAccumulationEvent
from whale_alpha.utils.logger import child_logger

log = child_logger("walletMonitor")

_MAX_AGE = timedelta(hours=6)  # 6h retention, matches the TS maxAgeMs


class EventBuffer:
    def __init__(self) -> None:
        self._events: list[WhaleAccumulationEvent] = []
        self._lock = threading.Lock()

    def push(self, event: WhaleAccumulationEvent) -> None:
        with self._lock:
            self._events.append(event)
            self._prune()

    def recent(self) -> list[WhaleAccumulationEvent]:
        with self._lock:
            self._prune()
            return list(self._events)

    def _prune(self) -> None:
        cutoff = datetime.now(UTC) - _MAX_AGE
        self._events = [e for e in self._events if e.observed_at >= cutoff]


# Module-level singleton, mirroring the TS `export const eventBuffer`.
event_buffer = EventBuffer()


async def ingest_wallet_buy_event(
    session: AsyncSession,
    *,
    wallet_address: str,
    token_mint: str,
    amount_tokens: float,
    amount_usd: float,
    tx_signature: str,
) -> None:
    """Call this from your indexer's webhook/subscription handler whenever a
    tracked wallet buys a token. This function only persists + buffers the
    event — it does NOT trigger a trade. Signal evaluation (and therefore any
    possibility of auto-trading) happens separately and only after multiple
    wallets cluster on the same token.

    TODO(integration): wire this up to Helius webhooks / a geyser plugin /
    your own indexer. Nothing in this repo invents on-chain data — this is the
    ingestion seam, carried over verbatim from the original.
    """
    result = await session.execute(select(WhaleWallet).where(WhaleWallet.address == wallet_address))
    wallet = result.scalar_one_or_none()
    if wallet is None or wallet.status != WalletStatus.APPROVED:
        log.debug("Ignoring event from untracked/non-approved wallet", address=wallet_address)
        return

    event = WalletEvent(
        wallet_id=wallet.id,
        token_mint=token_mint,
        side=TradeSide.BUY,
        amount_tokens=amount_tokens,
        amount_usd=amount_usd,
        tx_signature=tx_signature,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)

    event_buffer.push(
        WhaleAccumulationEvent(
            wallet_id=wallet.id,
            wallet_score=wallet.score,
            token_mint=token_mint,
            amount_usd=amount_usd,
            observed_at=event.observed_at,
        )
    )

    wallet.last_active_at = event.observed_at
    await session.commit()
