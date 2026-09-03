"""baseline: mark current schema as Alembic's starting point

Revision ID: 0001_baseline
Revises:
Create Date: v4.0 foundation

NEW in v4 (Bible §7). This revision is deliberately a no-op. It exists so
Alembic has a starting point that matches AlphaPulse's *actual* live
schema — which was created over time by `Base.metadata.create_all()` on
every v3 boot plus the hand-written `migrate_*_schema()` functions
(`signal_tracker.py`, `kol_tracker.py`, `paper_engine.py`,
`solana_wallet.py`) — rather than re-describing that schema from scratch
and risking it drifting from what's actually deployed.

How to adopt this on an existing (already-running) AlphaPulse database:

    alembic -c infra/db/alembic.ini stamp 0001_baseline

This tells Alembic "the database is already at this revision" without
running anything — safe on a database that already has data, because
`upgrade()`/`downgrade()` below do nothing. Only fresh, empty databases
(new local/dev/CI setups) need `alembic upgrade head` to actually run
`init_db()`'s create_all path first (see infra/db/session.py), or apply
this same baseline via a first real migration once one is generated with
`alembic revision --autogenerate` against a live database — something
this sandbox has no live Postgres to do. Until that autogenerate pass has
been run and reviewed against production, the hand-written
`migrate_*_schema()` functions remain in place, unmodified, as v3 left
them (Bible §7's explicit two-step retirement plan) — this file does not
retire them.
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deliberately empty — see module docstring. Real schema changes start
    # from 0002 onward.
    pass


def downgrade() -> None:
    pass
