"""create real_trailing_stops table + trailing-stop defaults on real_autobuy_filters

Revision ID: 0010_create_real_trailing_stops
Revises: 0009_robinhood_chain_wallet
Create Date: v4.2 Real Wallet Trailing Stop engine

Creates one brand-new, additive-only table for
domain/trading/real/real_trailing_stop_engine.py:

  real_trailing_stops - per-position trailing-stop state
                         (models/real_trailing_stop.py)

...and adds the trailing-stop default columns used to auto-attach a
trailing stop to Auto Buy on Signal positions (mirrors the existing
take_profit_pct/stop_loss_pct auto-attach columns already on
real_autobuy_filters):

  trailing_stop_enabled, trailing_activation_pct, trailing_pct,
  trailing_initial_stop_loss_pct, trailing_step_pct, trailing_profit_tiers

This migration does not touch real_trades, real_wallets, real_exit_rules,
real_limit_orders, auto_trade_*, or any other existing table. Written
defensively (create-if-missing, add-column-if-missing) in the same style
as 0007_create_auto_trade_engine_tables, since live production state
could not be verified from this environment. Also mirrored as
ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS statements in
domain/trading/real/robinhood_wallet.migrate_real_wallet_schema(), the
belt-and-suspenders runtime bootstrap this codebase already runs at
every worker startup for this table family.
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_create_real_trailing_stops"
down_revision = "0009_robinhood_chain_wallet"
branch_labels = None
depends_on = None


def _ensure_table(inspector, name, columns, unique_constraints=(), indexes=()):
    if name not in inspector.get_table_names():
        op.create_table(name, *columns, *unique_constraints)
        for index_name, index_cols in indexes:
            op.create_index(index_name, name, index_cols)
        return
    existing_columns = {c["name"] for c in inspector.get_columns(name)}
    for column in columns:
        if isinstance(column, sa.Column) and column.name not in existing_columns:
            op.add_column(name, sa.Column(column.name, column.type, nullable=True))
    existing_uniques = {uc["name"] for uc in inspector.get_unique_constraints(name)}
    for uc in unique_constraints:
        if isinstance(uc, sa.UniqueConstraint) and uc.name not in existing_uniques:
            op.create_unique_constraint(uc.name, name, [c.name for c in uc.columns])
    existing_indexes = {ix["name"] for ix in inspector.get_indexes(name)}
    for index_name, index_cols in indexes:
        if index_name not in existing_indexes:
            op.create_index(index_name, name, index_cols)


def _ensure_columns(inspector, table, columns):
    existing = {c["name"] for c in inspector.get_columns(table)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _ensure_table(
        inspector, "real_trailing_stops",
        columns=[
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("trade_id", sa.BigInteger(), nullable=False),
            sa.Column("entry_price", sa.Float(), nullable=False),
            sa.Column("activation_pct", sa.Float(), nullable=False),
            sa.Column("trail_pct", sa.Float(), nullable=False),
            sa.Column("initial_stop_loss_pct", sa.Float(), nullable=True),
            sa.Column("trailing_step_pct", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("profit_tiers_json", sa.Text(), nullable=True),
            sa.Column("sell_fraction", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("status", sa.String(), nullable=False, server_default="watching"),
            sa.Column("highest_price_seen", sa.Float(), nullable=True),
            sa.Column("current_stop_price", sa.Float(), nullable=True),
            sa.Column("last_price_seen", sa.Float(), nullable=True),
            sa.Column("armed_at", sa.DateTime(), nullable=True),
            sa.Column("last_checked_at", sa.DateTime(), nullable=True),
            sa.Column("tx_signature", sa.String(), nullable=True),
            sa.Column("trigger_reason", sa.String(), nullable=True),
            sa.Column("triggered_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        ],
        indexes=[
            ("ix_real_trailing_stops_user_id", ["user_id"]),
            ("ix_real_trailing_stops_trade_id", ["trade_id"]),
            ("ix_real_trailing_stops_status", ["status"]),
        ],
    )

    _ensure_columns(inspector, "real_autobuy_filters", [
        sa.Column("trailing_stop_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("trailing_activation_pct", sa.Float(), nullable=True),
        sa.Column("trailing_pct", sa.Float(), nullable=True),
        sa.Column("trailing_initial_stop_loss_pct", sa.Float(), nullable=True),
        sa.Column("trailing_step_pct", sa.Float(), nullable=True),
        sa.Column("trailing_profit_tiers", sa.String(), nullable=True),
    ])


def downgrade() -> None:
    # Deliberately conservative, same rationale as 0007: never drop a
    # table or columns that may hold live, real-money position state as
    # part of an automatic downgrade.
    pass
