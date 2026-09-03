"""add alert_delivered / alert_delivered_at to signal_tokens

Revision ID: 0004_add_signal_alert_delivered
Revises: 0003_validate_user_foreign_keys
Create Date: v4.0 production incident fix

Incident: both auto-buy paths -- paper, via
domain/signals/pump_radar.py's auto_buy_for_new_signal, and real money,
via domain/trading/real/real_automation_engine.py's independent poll of
this same table -- treated `SignalToken.status == "active"` as proof a
Signal Alert had reached a subscriber. That column is set to "active"
the instant create_signal_from_candidate() inserts the row, before the
alert-delivery loop in pump_radar_loop has run, and regardless of
whether delivery to every subscriber then failed. Result: capital could
move on a token nobody was ever actually alerted about.

This migration adds a column that is only ever set True once
pump_radar_loop has confirmed at least one subscriber genuinely
received the Signal Alert card this cycle (see
domain.signals.signal_tracker.mark_signal_alert_delivered). Both
auto-buy paths are updated in the same change to require it. No
existing hard-reject, scoring, holder/bundle, or risk-gate logic is
touched by this migration or that change -- this only adds a stricter,
additional condition for a signal to become buy-eligible.

Default is FALSE for both new rows and (server_default) all
pre-existing rows, so no historical signal is retroactively treated as
alerted.

Note: domain/signals/signal_tracker.py's migrate_signal_schema() also
adds these same two columns via idempotent
`ADD COLUMN IF NOT EXISTS` at worker startup, matching this
repository's existing dual-migration convention for the signal_tokens
table (see e.g. message_ids_json / highest_alerted_multiple, added the
same way). Running either path first makes the other a safe no-op.
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_add_signal_alert_delivered"
down_revision = "0003_validate_user_foreign_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "signal_tokens",
        sa.Column("alert_delivered", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "signal_tokens",
        sa.Column("alert_delivered_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("signal_tokens", "alert_delivered_at")
    op.drop_column("signal_tokens", "alert_delivered")
