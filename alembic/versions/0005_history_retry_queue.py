"""Wallet-history retry queue (Phase 1 production hardening) — new columns.

Revision ID: 0005_history_retry_queue
Revises: 0004_hybrid_discovery
Create Date: 2026-08-05

Adds:
  * wallet_candidates.history_retry_count (Integer, default 0) — counts
    transient (429/5xx/network) wallet-history fetch failures only; see
    engines/discovery.evaluate_candidates and utils/http_retry.py.
  * wallet_candidates.next_retry_at (DateTime, nullable) — short
    exponential-backoff window a candidate becomes eligible for re-fetch
    again, independent of the multi-hour last_evaluated_at re-evaluation
    cutoff already in place.

No changes to any other table — scoped strictly to the discovery engine's
wallet-history retry queue, same convention as 0003/0004.

Same disclaimer as prior migrations: hand-authored against db/models.py, no
live Postgres to autogenerate against in this sandbox. Diff against
`alembic revision --autogenerate` before trusting this in production.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_history_retry_queue"
down_revision: str | None = "0004_hybrid_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "wallet_candidates",
        sa.Column("history_retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "wallet_candidates",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_wallet_candidates_next_retry_at", "wallet_candidates", ["next_retry_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_candidates_next_retry_at", table_name="wallet_candidates")
    op.drop_column("wallet_candidates", "next_retry_at")
    op.drop_column("wallet_candidates", "history_retry_count")
