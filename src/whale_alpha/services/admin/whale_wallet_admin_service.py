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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from whale_alpha.db.models import AuditLog, Role, WhaleWallet, WalletStatus
from whale_alpha.utils.logger import child_logger

log = child_logger("whaleWalletAdmin")


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
