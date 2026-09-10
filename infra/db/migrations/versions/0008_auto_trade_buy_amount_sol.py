"""rename auto_trade_policies.buy_amount_usdt to buy_amount_sol

Revision ID: 0008_auto_trade_buy_amount_sol
Revises: 0007_create_auto_trade_engine_tables

Buy Amount fix: the Auto-Trade Engine's per-trade size field was
labeled/stored as a USDT amount but the flow asks the user for it and
spends it as-is on-chain -- it was never actually converted at the
point of storage, only later divided by a fetched SOL/USD price in
domain/trading/auto_trade/orchestrator.py to size the swap. That
indirection (and its failure mode -- trades silently skipped whenever
the SOL/USD price lookup failed) is removed: the user now enters the
SOL amount directly, so this column is renamed to reflect what it
actually holds from here on.

Column type (Float, not null) is unchanged -- only the name and the
default. Written defensively (rename-if-present) in the same style as
0007, since live production state could not be verified from this
environment.

Existing values were entered and stored as dollar amounts (e.g. "10"
meaning $10). Re-using that same number as a SOL amount verbatim would
silently size a real on-chain buy ~any existing SOL/USD rate times
larger than the user ever configured -- an unsafe reinterpretation, not
a valid conversion this migration can make without a market price
lookup. Every existing row is reset to the new field's safe default
(DEFAULT_SOL_PER_TRADE, domain/trading/auto_trade/constants.py) instead;
affected users see their configured Buy Amount reset to 0.1 SOL and
should re-check /wallet after this deploys.
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_auto_trade_buy_amount_sol"
down_revision = "0007_create_auto_trade_engine_tables"
branch_labels = None
depends_on = None

_NEW_DEFAULT_SOL = "0.1"
_OLD_DEFAULT_USDT = "10.0"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "auto_trade_policies" not in inspector.get_table_names():
        return  # 0007 hasn't run / table missing -- nothing to rename.

    columns = {c["name"] for c in inspector.get_columns("auto_trade_policies")}
    if "buy_amount_usdt" in columns and "buy_amount_sol" not in columns:
        op.alter_column(
            "auto_trade_policies", "buy_amount_usdt",
            new_column_name="buy_amount_sol",
        )
    if "buy_amount_sol" in columns or "buy_amount_usdt" in columns:
        op.alter_column(
            "auto_trade_policies", "buy_amount_sol",
            server_default=_NEW_DEFAULT_SOL,
        )
        # Reset pre-existing dollar-denominated values to the new
        # field's safe SOL default -- see module docstring.
        op.execute(
            f"UPDATE auto_trade_policies SET buy_amount_sol = {_NEW_DEFAULT_SOL}"
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "auto_trade_policies" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("auto_trade_policies")}
    if "buy_amount_sol" in columns:
        # Same rationale in reverse: a SOL amount is not a valid USDT
        # amount either, so reset rather than reinterpret.
        op.execute(
            f"UPDATE auto_trade_policies SET buy_amount_sol = {_OLD_DEFAULT_USDT}"
        )
        op.alter_column(
            "auto_trade_policies", "buy_amount_sol",
            new_column_name="buy_amount_usdt",
        )
        op.alter_column(
            "auto_trade_policies", "buy_amount_usdt",
            server_default=_OLD_DEFAULT_USDT,
        )
