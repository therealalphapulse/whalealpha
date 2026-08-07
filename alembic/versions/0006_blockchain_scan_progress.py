"""Blockchain-first discovery engine (Phase 1 refactor) — new table.

Revision ID: 0006_blockchain_scan_progress
Revises: 0005_history_retry_queue
Create Date: 2026-08-07

Adds:
  * discovery_scan_progress — persists the last Solana slot the block
    scanner (integrations/chain_scanner.py) has fully processed, one row
    per cluster (mainnet-beta/devnet/testnet), so the scanner can page
    through recent blocks in small bounded batches and resume exactly
    where it left off after a restart instead of re-scanning or silently
    skipping slots. See db/models.DiscoveryScanProgress and
    engines/discovery.discover_candidates.

No changes to any other table, and no changes to wallet_candidates,
whale_wallets, signals, trades, or any table the signal/trading/admin
surfaces depend on — scoped strictly to the new discovery-source
checkpoint, same convention as 0003/0004/0005.

Same disclaimer as prior migrations: hand-authored against db/models.py, no
live Postgres to autogenerate against in this sandbox. Diff against
`alembic revision --autogenerate` before trusting this in production.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_blockchain_scan_progress"
down_revision: Union[str, None] = "0005_history_retry_queue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discovery_scan_progress",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("cluster", sa.String(), nullable=False),
        sa.Column("last_processed_slot", sa.Integer(), nullable=False),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cluster"),
    )


def downgrade() -> None:
    op.drop_table("discovery_scan_progress")
