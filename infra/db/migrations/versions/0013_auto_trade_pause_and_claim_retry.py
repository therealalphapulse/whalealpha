"""Auto-Trade wallet-level pause (circuit breaker) + claim retry tracking.

Fixes a production incident where a deterministic rejection (e.g.
insufficient balance) was retried every scan tick (5s) with no backoff
for up to SIGNAL_LOOKBACK_MINUTES, flooding the user with repeat
"Auto-Trade Rejected" notifications for the same signal. See
domain/trading/auto_trade/claims.py and policy_service.py.

Note: as with prior Auto-Trade Engine migrations (0008 onward), this
file is not actually invoked by this repo's boot sequence -- the
idempotent migrate_auto_trade_schema() / migrate_auto_trade_claims_schema()
functions applied at worker startup are what runs against production.
Kept for history/parity with the rest of infra/db/migrations/.
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_auto_trade_pause_and_claim_retry"
down_revision = "0012_add_trail_global_enabled"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("auto_trade_policies", sa.Column("paused_until", sa.DateTime(), nullable=True))
    op.add_column("auto_trade_policies", sa.Column("paused_reason", sa.String(), nullable=True))
    op.add_column("auto_trade_claims", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("auto_trade_claims", sa.Column("last_reason", sa.String(), nullable=True))


def downgrade():
    op.drop_column("auto_trade_claims", "last_reason")
    op.drop_column("auto_trade_claims", "retry_count")
    op.drop_column("auto_trade_policies", "paused_reason")
    op.drop_column("auto_trade_policies", "paused_until")
