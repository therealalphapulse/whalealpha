"""
app_platform/gateway/bootstrap.py

NEW in v4 (Bible §8 Infrastructure — "separate a release step from the
run step"). v3's `main.py` ran RBAC owner bootstrap, premium plan
seeding, four hand-written schema migrations, two one-time resets, and
the initial KOL sync sequentially, inline, every time the single process
started (audit §7/§8).

v4 keeps every one of those calls exactly as they were (none of this
logic changes) but factors them out so they can run once, deliberately,
as a release-phase step (`python -m app_platform.gateway.bootstrap`,
wired as the Docker `release` process in Procfile) — decoupled from
`dp.start_polling()`/webhook serving, and safe to run exactly once even
when N Bot Gateway replicas start up around the same time (unlike v3,
where every process ran this on every boot).

Cleanup note: the two one-time migrations that used to live here —
`reset_signal_history_once()` (domain/signals/signal_tracker.py) and
`reset_stale_default_balances_once()` (domain/trading/paper/paper_engine.py)
— have both already run to completion in production (their SystemFlag
rows are permanently set), so they were no-ops on every subsequent boot
anyway. They're removed here rather than left in place because each one
unconditionally deletes/rewrites real user data (all SignalToken/
SignalEvent rows, all paper portfolio balances) the very first time it
ever runs against ANY database that doesn't yet have its flag — including
a brand-new one. Keeping dead one-time migrations like this around is a
landmine for any future fresh database (disaster recovery, a new
environment, a provider migration), so once a one-time migration has
served its purpose it should be deleted, not left "safely" no-op'ing
behind a flag check. The two underlying functions still exist in their
original modules if historical reference is ever needed — only the
calls from this bootstrap sequence are removed.
"""

from __future__ import annotations

import asyncio
import logging

from infra.db.session import init_db
from domain.admin.admin_rbac import ensure_owner_bootstrapped
from domain.payments.premium_plans import ensure_default_plans_seeded
from domain.signals.signal_tracker import migrate_signal_schema
from domain.intelligence.kol_tracker import migrate_kol_wallet_schema, sync_kol_wallets_from_provider
from domain.trading.paper.paper_engine import migrate_paper_trade_schema
from domain.trading.real.solana_wallet import migrate_real_wallet_schema
from domain.signals.pump_radar import subscribe_all_users_to_pump_alerts

logger = logging.getLogger("AlphaPulse.Bootstrap")


async def run_startup_tasks() -> None:
    # v4 (Bible §7): once the Alembic baseline (infra/db/migrations/) is
    # verified against the live schema, init_db() and the migrate_*_schema()
    # calls below are retired per the two-step plan in the Bible. Until
    # then they remain, unchanged, exactly as safe/unsafe as they were in
    # v3 — this bootstrap step does not change their behavior, only when
    # and how often they run.
    await init_db()

    for label, coro in [
        ("RBAC owner bootstrap", ensure_owner_bootstrapped()),
        ("Premium plan seeding", ensure_default_plans_seeded()),
        ("Signal schema migration", migrate_signal_schema()),
        ("KOL schema migration", migrate_kol_wallet_schema()),
        ("Paper trade schema migration", migrate_paper_trade_schema()),
        ("Real wallet schema migration", migrate_real_wallet_schema()),
        ("Bulk pump alert auto-subscribe", subscribe_all_users_to_pump_alerts()),
        ("Initial KOL provider sync", sync_kol_wallets_from_provider(bot=None)),
    ]:
        try:
            await coro
        except Exception as e:
            logger.error("%s failed (non-fatal): %s", label, e)

    logger.info("Bootstrap complete.")


if __name__ == "__main__":
    from infra.observability.logging_config import configure_logging

    configure_logging()
    asyncio.run(run_startup_tasks())
