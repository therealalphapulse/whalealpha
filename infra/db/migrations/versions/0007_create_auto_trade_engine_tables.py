"""create auto_trade_* tables for the new, isolated Auto-Trade Engine

Revision ID: 0007_create_auto_trade_engine_tables
Revises: 0006_add_robinhood_token_watch
Create Date: v4.1 Auto-Trade Engine (Trojan-inspired, isolated from
    the existing Real Wallet AutoBuy/AutoTrade implementation)

Creates four brand-new, additive-only tables for
domain/trading/auto_trade/:

  auto_trade_policies   - per-user policy/config (models/auto_trade_policy.py)
  auto_trade_claims     - idempotency claims keyed on (user_id, signal_id)
                           (models/auto_trade_claim.py)
  auto_trade_positions  - position + trade state machine (models/auto_trade_position.py)
  auto_trade_executions - trade ledger / audit trail (models/auto_trade_execution.py)

This migration does not touch real_trades, real_wallets,
real_autobuy_filters, auto_buy_claims, or any other existing table.
Written defensively (create-if-missing, add-column-if-missing) in the
same style as 0005_create_auto_buy_claims, since live production state
could not be verified from this environment.
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_create_auto_trade_engine_tables"
down_revision = "0006_auto_trading_activation_boundary"
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


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _ensure_table(
        inspector, "auto_trade_policies",
        columns=[
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("auto_trade_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("auto_trade_enabled_at", sa.DateTime(), nullable=True),
            sa.Column("kill_switch", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("allowed_signal_tiers", sa.String(), nullable=True),
            sa.Column("min_score", sa.Float(), nullable=True),
            sa.Column("buy_amount_usdt", sa.Float(), nullable=False, server_default="10.0"),
            sa.Column("take_profit_pct", sa.Float(), nullable=True, server_default="50.0"),
            sa.Column("stop_loss_pct", sa.Float(), nullable=True, server_default="30.0"),
            sa.Column("trailing_stop_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("trailing_stop_pct", sa.Float(), nullable=True),
            sa.Column("daily_trade_limit", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("max_open_positions", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("max_total_exposure_usdt", sa.Float(), nullable=True),
            sa.Column("max_position_size_usdt", sa.Float(), nullable=True),
            sa.Column("slippage_bps", sa.Integer(), nullable=False, server_default="150"),
            sa.Column("priority_fee_tier", sa.String(), nullable=False, server_default="auto"),
            sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default="120"),
            sa.Column("allow_multiple_positions_same_token", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("daily_spent_usdt", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("daily_spent_date", sa.String(), nullable=True),
            sa.Column("daily_trade_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("daily_trade_count_date", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        ],
        unique_constraints=[sa.UniqueConstraint("user_id", name="uq_auto_trade_policies_user_id")],
        indexes=[("ix_auto_trade_policies_user_id", ["user_id"])],
    )

    _ensure_table(
        inspector, "auto_trade_claims",
        columns=[
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("signal_id", sa.BigInteger(), nullable=False),
            sa.Column("contract", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        ],
        unique_constraints=[sa.UniqueConstraint("user_id", "signal_id", name="uq_auto_trade_claims_user_signal")],
        indexes=[
            ("ix_auto_trade_claims_user_id", ["user_id"]),
            ("ix_auto_trade_claims_signal_id", ["signal_id"]),
            ("ix_auto_trade_claims_contract", ["contract"]),
        ],
    )

    _ensure_table(
        inspector, "auto_trade_positions",
        columns=[
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("signal_id", sa.BigInteger(), nullable=True),
            sa.Column("claim_id", sa.BigInteger(), nullable=True),
            sa.Column("contract", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=True),
            sa.Column("symbol", sa.String(), nullable=True),
            sa.Column("state", sa.String(), nullable=False, server_default="TRADE_INTENT_CREATED"),
            sa.Column("policy_snapshot_json", sa.Text(), nullable=True),
            sa.Column("signal_price", sa.Float(), nullable=True),
            sa.Column("entry_price", sa.Float(), nullable=True),
            sa.Column("requested_usdt", sa.Float(), nullable=True),
            sa.Column("sol_spent", sa.Float(), nullable=True),
            sa.Column("token_quantity", sa.Float(), nullable=True),
            sa.Column("remaining_quantity", sa.Float(), nullable=True),
            sa.Column("token_decimals", sa.Integer(), nullable=True),
            sa.Column("buy_tx_signature", sa.String(), nullable=True),
            sa.Column("buy_fees_sol", sa.Float(), nullable=True, server_default="0.0"),
            sa.Column("highest_observed_price", sa.Float(), nullable=True),
            sa.Column("trailing_trigger_price", sa.Float(), nullable=True),
            sa.Column("total_cost_basis_sol", sa.Float(), nullable=True),
            sa.Column("realized_proceeds_sol", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("realized_pnl_sol", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("realized_pnl_pct", sa.Float(), nullable=True),
            sa.Column("total_fees_sol", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("average_exit_price", sa.Float(), nullable=True),
            sa.Column("exit_reason", sa.String(), nullable=True),
            sa.Column("last_error", sa.String(), nullable=True),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("opened_at", sa.DateTime(), nullable=True),
            sa.Column("closed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        ],
        indexes=[
            ("ix_auto_trade_positions_user_id", ["user_id"]),
            ("ix_auto_trade_positions_signal_id", ["signal_id"]),
            ("ix_auto_trade_positions_claim_id", ["claim_id"]),
            ("ix_auto_trade_positions_contract", ["contract"]),
            ("ix_auto_trade_positions_state", ["state"]),
        ],
    )

    _ensure_table(
        inspector, "auto_trade_executions",
        columns=[
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("position_id", sa.BigInteger(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("side", sa.String(), nullable=False),
            sa.Column("contract", sa.String(), nullable=False),
            sa.Column("requested_amount", sa.Float(), nullable=True),
            sa.Column("actual_amount", sa.Float(), nullable=True),
            sa.Column("requested_price", sa.Float(), nullable=True),
            sa.Column("actual_price", sa.Float(), nullable=True),
            sa.Column("quote_amount", sa.Float(), nullable=True),
            sa.Column("fees_sol", sa.Float(), nullable=True, server_default="0.0"),
            sa.Column("transaction_signature", sa.String(), nullable=True),
            sa.Column("provider", sa.String(), nullable=False, server_default="jupiter"),
            sa.Column("route", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="submitted"),
            sa.Column("error", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        ],
        indexes=[
            ("ix_auto_trade_executions_position_id", ["position_id"]),
            ("ix_auto_trade_executions_user_id", ["user_id"]),
            ("ix_auto_trade_executions_transaction_signature", ["transaction_signature"]),
        ],
    )


def downgrade() -> None:
    # Deliberately conservative, same rationale as 0005: never drop
    # tables that may hold live, real-money position/ledger data as
    # part of an automatic downgrade.
    pass
