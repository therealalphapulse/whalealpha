"""Whale wallet admin service — port of src/services/admin/whaleWalletAdminService.ts.

This service is the ONLY place whale wallets are created, approved, rejected,
or removed. Every bot command, HTTP route, and script must go through here
rather than touching the WhaleWallet table directly — that keeps the "users
can never add wallets" requirement enforced in one place instead of scattered
checks.

Defense in depth (porting requirement #5): `_assert_admin` re-checks the
actor's role independently of whatever gate the bot-layer middleware already
applied. Never trust a single layer of RBAC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from whale_alpha.db.models import AuditLog, Role, WalletStatus, WhaleWallet
from whale_alpha.engines.scoring import WalletMetrics
from whale_alpha.utils.logger import child_logger

log = child_logger("whaleWalletAdmin")

# Sentinel telegram_id for the system user the discovery engine acts as (see
# engines/discovery.py's _system_actor). Never a real Telegram chat id (those
# are always numeric), so it can't collide with a human account. Using a
# real User row (role=SUPERADMIN) rather than bypassing RBAC entirely keeps
# the "every whale_wallets write is an admin-role actor, audited" invariant
# literally true even for automated writes — see docs/ARCHITECTURE.md.
DISCOVERY_ENGINE_TELEGRAM_ID = "SYSTEM_DISCOVERY_ENGINE"


def _apply_metrics(wallet: WhaleWallet, metrics: WalletMetrics) -> None:
    """Copies a scoring.WalletMetrics onto the corresponding WhaleWallet
    stat columns. Shared by promote_candidate and update_metrics so the two
    discovery-engine write paths can't drift out of sync on which columns
    they populate.
    """
    wallet.roi_30d = metrics.roi_30d
    wallet.pnl_usd_30d = metrics.pnl_usd_30d
    wallet.win_rate = metrics.win_rate
    wallet.avg_hold_minutes = metrics.avg_hold_minutes
    wallet.avg_position_usd = metrics.avg_position_usd
    wallet.trade_frequency_7d = metrics.trade_frequency_7d
    wallet.wallet_age_days = metrics.wallet_age_days
    wallet.max_drawdown = metrics.max_drawdown
    wallet.trade_success_rate = metrics.trade_success_rate


class ForbiddenError(Exception):
    def __init__(self, message: str = "Admin privileges required") -> None:
        super().__init__(message)


@dataclass
class Actor:
    id: str
    role: Role


def _assert_admin(actor: Actor) -> None:
    if actor.role not in (Role.ADMIN, Role.SUPERADMIN):
        raise ForbiddenError()


class WhaleWalletAdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_wallet(self, actor: Actor, address: str, label: str | None = None) -> WhaleWallet:
        _assert_admin(actor)

        wallet = WhaleWallet(
            address=address,
            label=label,
            status=WalletStatus.PENDING_REVIEW,
            added_by_admin_id=actor.id,
        )
        self._session.add(wallet)
        await self._session.commit()
        await self._session.refresh(wallet)

        await self._audit(actor.id, "WHALE_WALLET_ADD", wallet.id, {"address": address})
        log.info("Whale wallet added", actor_id=actor.id, address=address)
        return wallet

    async def set_status(self, actor: Actor, wallet_id: str, status: WalletStatus) -> WhaleWallet:
        _assert_admin(actor)

        wallet = await self._session.get(WhaleWallet, wallet_id)
        if wallet is None:
            raise ValueError(f"WhaleWallet {wallet_id} not found")
        wallet.status = status
        await self._session.commit()
        await self._session.refresh(wallet)

        await self._audit(actor.id, "WHALE_WALLET_STATUS_CHANGE", wallet_id, {"status": status.value})
        return wallet

    async def update_score(
        self, actor: Actor, wallet_id: str, score: float, confidence: float
    ) -> WhaleWallet:
        _assert_admin(actor)

        wallet = await self._session.get(WhaleWallet, wallet_id)
        if wallet is None:
            raise ValueError(f"WhaleWallet {wallet_id} not found")
        wallet.score = score
        wallet.confidence = confidence
        await self._session.commit()
        await self._session.refresh(wallet)

        await self._audit(
            actor.id, "WHALE_WALLET_SCORE_UPDATE", wallet_id, {"score": score, "confidence": confidence}
        )
        return wallet

    async def promote_candidate(
        self,
        actor: Actor,
        *,
        address: str,
        label: str | None,
        score: float,
        confidence: float,
        metrics: WalletMetrics,
        source: str,
    ) -> WhaleWallet:
        """Creates a new WhaleWallet directly in APPROVED status with
        auto_discovered=True — the discovery engine's equivalent of a human
        admin's /addwhale followed immediately by /approvewhale. Only ever
        called for a candidate that already cleared engines/discovery.py's
        promotion bar (see should_promote_candidate), never on raw user
        input, so the "users can never add whale wallets" invariant still
        holds — this is the automated-admin path, not a user-facing one.
        """
        _assert_admin(actor)

        wallet = WhaleWallet(
            address=address,
            label=label,
            status=WalletStatus.APPROVED,
            added_by_admin_id=actor.id,
            auto_discovered=True,
            discovery_source=source,
            score=score,
            confidence=confidence,
            last_scored_at=datetime.now(UTC),
        )
        _apply_metrics(wallet, metrics)
        self._session.add(wallet)
        await self._session.commit()
        await self._session.refresh(wallet)

        await self._audit(
            actor.id,
            "WHALE_WALLET_AUTO_DISCOVERED",
            wallet.id,
            {"address": address, "score": score, "confidence": confidence, "source": source},
        )
        log.info("Whale wallet auto-discovered and promoted", address=address, score=score, source=source)
        return wallet

    async def update_metrics(
        self,
        actor: Actor,
        wallet_id: str,
        *,
        score: float,
        confidence: float,
        metrics: WalletMetrics,
        consecutive_low_score_cycles: int,
    ) -> WhaleWallet:
        """Re-scoring update for an already-tracked wallet — called by the
        discovery engine's periodic re-scoring pass, never by a user command.
        """
        _assert_admin(actor)

        wallet = await self._session.get(WhaleWallet, wallet_id)
        if wallet is None:
            raise ValueError(f"WhaleWallet {wallet_id} not found")

        wallet.score = score
        wallet.confidence = confidence
        wallet.consecutive_low_score_cycles = consecutive_low_score_cycles
        wallet.last_scored_at = datetime.now(UTC)
        _apply_metrics(wallet, metrics)
        await self._session.commit()
        await self._session.refresh(wallet)

        await self._audit(
            actor.id,
            "WHALE_WALLET_SCORE_UPDATE",
            wallet_id,
            {"score": score, "confidence": confidence, "consecutive_low_score_cycles": consecutive_low_score_cycles},
        )
        return wallet

    async def remove(self, actor: Actor, wallet_id: str) -> None:
        _assert_admin(actor)

        wallet = await self._session.get(WhaleWallet, wallet_id)
        if wallet is not None:
            await self._session.delete(wallet)
            await self._session.commit()
        await self._audit(actor.id, "WHALE_WALLET_REMOVE", wallet_id, {})

    async def list_approved(self, limit: int = 50) -> list[WhaleWallet]:
        """Read-only: available to any authenticated user, not just admins."""
        result = await self._session.execute(
            select(WhaleWallet)
            .where(WhaleWallet.status == WalletStatus.APPROVED)
            .order_by(WhaleWallet.score.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def _audit(self, actor_id: str, action: str, target_id: str, metadata: dict) -> None:
        self._session.add(
            AuditLog(
                actor_id=actor_id,
                action=action,
                target_type="WhaleWallet",
                target_id=target_id,
                metadata_=metadata,
            )
        )
        await self._session.commit()
