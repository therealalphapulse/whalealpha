"""add wallet_consensus_signals and robinhood_discovery_signals tables

Revision ID: 0005_add_multi_discovery_signals
Revises: 0004_add_signal_alert_delivered
Create Date: WhaleAlpha multi-discovery signal engine

Adds the two new signal/evidence + dedupe-cooldown tables backing:

  * Discovery Engine A (Solana Profitable Wallet Consensus) --
    wallet_consensus_signals. Wallet identity and purchase evidence
    themselves are NOT new tables -- they already exist as
    premium_wallets / premium_wallet_trades and are extended in place
    (see the accompanying code change to
    domain.intelligence.premium_wallet_scorer's classification tags).
    This table only stores the CONSENSUS EVENT itself: which token,
    which distinct wallets/transactions crossed the threshold, and the
    resulting signal's dedupe/cooldown/re-arm state.

  * Discovery Engine B (Robinhood Chain Token Discovery) --
    robinhood_discovery_signals. Fully independent of the table above;
    no foreign key or shared row ever links the two.

Both are purely additive (new tables only) and safely reversible.
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_add_multi_discovery_signals"
down_revision = "0004_add_signal_alert_delivered"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_consensus_signals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("token_contract", sa.String(), nullable=False),
        sa.Column("token_symbol", sa.String(), nullable=True),
        sa.Column("token_name", sa.String(), nullable=True),
        sa.Column("chain", sa.String(), nullable=False, server_default="solana"),
        sa.Column("wallet_count", sa.Integer(), nullable=False),
        sa.Column("wallet_addresses_json", sa.Text(), nullable=True),
        sa.Column("wallet_classifications_json", sa.Text(), nullable=True),
        sa.Column("transaction_evidence_json", sa.Text(), nullable=True),
        sa.Column("coordinated_cluster_json", sa.Text(), nullable=True),
        sa.Column("avg_wallet_reputation", sa.Float(), nullable=True),
        sa.Column("observation_window_minutes", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("snapshot_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("times_alerted", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_alerted_at", sa.DateTime(), nullable=True),
        sa.Column("last_alerted_at", sa.DateTime(), nullable=True),
        sa.Column("cooldown_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("token_contract", name="uq_wallet_consensus_signals_token_contract"),
    )
    op.create_index(
        "ix_wallet_consensus_signals_token_contract",
        "wallet_consensus_signals",
        ["token_contract"],
    )

    op.create_table(
        "robinhood_discovery_signals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("token_contract", sa.String(), nullable=False),
        sa.Column("pair_address", sa.String(), nullable=True),
        sa.Column("token_symbol", sa.String(), nullable=True),
        sa.Column("token_name", sa.String(), nullable=True),
        sa.Column("chain", sa.String(), nullable=False, server_default="robinhood"),
        sa.Column("discovery_source", sa.String(), nullable=False),
        sa.Column("discovery_score", sa.Float(), nullable=False),
        sa.Column("score_breakdown_json", sa.Text(), nullable=True),
        sa.Column("reasons_json", sa.Text(), nullable=True),
        sa.Column("snapshot_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("times_alerted", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_alerted_at", sa.DateTime(), nullable=True),
        sa.Column("last_alerted_at", sa.DateTime(), nullable=True),
        sa.Column("cooldown_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("token_contract", name="uq_robinhood_discovery_signals_token_contract"),
    )
    op.create_index(
        "ix_robinhood_discovery_signals_token_contract",
        "robinhood_discovery_signals",
        ["token_contract"],
    )


def downgrade() -> None:
    op.drop_index("ix_robinhood_discovery_signals_token_contract", table_name="robinhood_discovery_signals")
    op.drop_table("robinhood_discovery_signals")
    op.drop_index("ix_wallet_consensus_signals_token_contract", table_name="wallet_consensus_signals")
    op.drop_table("wallet_consensus_signals")
