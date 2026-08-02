"""Whale Wallet Discovery & Intelligence Engine — new columns + table.

Revision ID: 0003_discovery_engine
Revises: 0002_notify_alerts
Create Date: 2026-08-01

Adds:
  * whale_wallets: auto_discovered, discovery_source,
    consecutive_low_score_cycles, last_scored_at — see engines/discovery.py
    and db/models.py's WhaleWallet docstring for what each backs.
  * wallet_candidates: the discovery pipeline's staging table (candidates
    are evaluated here before ever touching whale_wallets).

Same disclaimer as 0001/0002: hand-authored against db/models.py, no live
Postgres to autogenerate against in this sandbox. Diff against
`alembic revision --autogenerate` before trusting this in production.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_discovery_engine"
down_revision: Union[str, None] = "0002_notify_alerts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

candidate_status_enum = postgresql.ENUM(
    "NEW", "EVALUATED", "PROMOTED", "REJECTED", name="candidatestatus", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    candidate_status_enum.create(bind, checkfirst=True)

    op.add_column(
        "whale_wallets",
        sa.Column("auto_discovered", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("whale_wallets", sa.Column("discovery_source", sa.String(), nullable=True))
    op.add_column(
        "whale_wallets",
        sa.Column("consecutive_low_score_cycles", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "whale_wallets", sa.Column("last_scored_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "wallet_candidates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("address", sa.String(), nullable=False, unique=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("status", candidate_status_enum, nullable=False, server_default="NEW"),
        sa.Column("discovered_from_token_mint", sa.String(), nullable=True),
        sa.Column("last_score", sa.Float(), nullable=True),
        sa.Column("last_confidence", sa.Float(), nullable=True),
        sa.Column("last_metrics", sa.JSON(), nullable=True),
        sa.Column("rejection_reason", sa.String(), nullable=True),
        sa.Column("evaluation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "promoted_wallet_id", sa.String(), sa.ForeignKey("whale_wallets.id"), nullable=True
        ),
    )
    op.create_index("ix_wallet_candidates_status", "wallet_candidates", ["status"])
    op.create_index(
        "ix_wallet_candidates_last_evaluated_at", "wallet_candidates", ["last_evaluated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_candidates_last_evaluated_at", table_name="wallet_candidates")
    op.drop_index("ix_wallet_candidates_status", table_name="wallet_candidates")
    op.drop_table("wallet_candidates")

    op.drop_column("whale_wallets", "last_scored_at")
    op.drop_column("whale_wallets", "consecutive_low_score_cycles")
    op.drop_column("whale_wallets", "discovery_source")
    op.drop_column("whale_wallets", "auto_discovered")

    bind = op.get_bind()
    candidate_status_enum.drop(bind, checkfirst=True)
