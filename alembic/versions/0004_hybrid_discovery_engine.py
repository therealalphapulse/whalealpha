"""Hybrid Wallet Discovery Engine (Phase 1 refactor) — new columns + table.

Revision ID: 0004_hybrid_discovery
Revises: 0003_discovery_engine
Create Date: 2026-08-03

Adds:
  * wallet_candidates: behavior_scores (JSON), labels (ARRAY[String]) — see
    engines/behavior_scoring.py and engines/wallet_labels.py.
  * wallet_relationships: new table backing engines/wallet_graph.py's
    Wallet Graph Expansion (Priority 4). Deliberately a plain table with a
    unique (wallet_address, related_address) pair rather than a graph
    database, per the architecture requirements.

No changes to any other table — this migration is scoped strictly to the
discovery engine, same as 0003.

Same disclaimer as prior migrations: hand-authored against db/models.py, no
live Postgres to autogenerate against in this sandbox. Diff against
`alembic revision --autogenerate` before trusting this in production.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_hybrid_discovery"
down_revision: Union[str, None] = "0003_discovery_engine"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("wallet_candidates", sa.Column("behavior_scores", sa.JSON(), nullable=True))
    op.add_column(
        "wallet_candidates",
        sa.Column(
            "labels",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
    )

    op.create_table(
        "wallet_relationships",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("wallet_address", sa.String(), nullable=False),
        sa.Column("related_address", sa.String(), nullable=False),
        sa.Column("relationship_type", sa.String(), nullable=False),
        sa.Column("co_occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "shared_token_mints",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("strength", sa.Float(), nullable=False, server_default="0"),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("wallet_address", "related_address", name="uq_wallet_relationships_pair"),
    )
    op.create_index(
        "ix_wallet_relationships_wallet_address", "wallet_relationships", ["wallet_address"]
    )
    op.create_index(
        "ix_wallet_relationships_related_address", "wallet_relationships", ["related_address"]
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_relationships_related_address", table_name="wallet_relationships")
    op.drop_index("ix_wallet_relationships_wallet_address", table_name="wallet_relationships")
    op.drop_table("wallet_relationships")

    op.drop_column("wallet_candidates", "labels")
    op.drop_column("wallet_candidates", "behavior_scores")
