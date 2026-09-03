"""
main.py

v4 (Bible §12/§15). v3's `main.py` did everything in one process: DB
init, RBAC/plan seeding, four schema migrations, two one-time resets,
initial KOL sync, Dispatcher construction, all 19 routers, and ~10
background loops, all sharing one event loop (audit §7/§11).

v4 splits that into `app_platform.gateway.bootstrap` (one-time startup
tasks), `app_platform.gateway.polling_entrypoint` /
`webhook_entrypoint` (Telegram-facing traffic), and
`workers.signal_trading_worker` / `workers.intelligence_worker`
(background loops) — see docker-compose.yml, which runs each as its own
service/process.

This file exists only so that `python main.py` (and therefore Railway's
existing `Procfile: worker: python main.py`, or any other platform
expecting one process) keeps working exactly as before, for anyone not
yet ready to move to the split multi-process topology: it runs bootstrap,
then the gateway, then both workers, all as concurrent tasks in a single
process — functionally equivalent to v3's `main.py`, built from the new
v4 modules rather than duplicating their logic.

New deployments should prefer `docker-compose.yml`'s multi-service
topology (Bible §3) — this file is the single-process fallback, not the
v4-recommended path.
"""

from __future__ import annotations

import asyncio
import logging

from infra.observability.logging_config import configure_logging
from infra.db.session import close_db
from app_platform.gateway.app import build_app, set_bot_commands
from app_platform.gateway.bootstrap import run_startup_tasks
from workers.signal_trading_worker import main as run_signal_trading_worker
from workers.intelligence_worker import main as run_intelligence_worker

logger = logging.getLogger("AlphaPulse.SingleProcess")


async def main() -> None:
    configure_logging()
    logger.info(
        "Starting AlphaPulse v4 in single-process mode (main.py). "
        "For horizontal scaling, run app_platform.gateway + workers/ as "
        "separate services instead — see docker-compose.yml."
    )

    # NOTE: Procfile's `release` step (Bible §8) already runs this exact
    # bootstrap once per deploy on platforms that support release phases
    # (Railway does). Calling it again here is intentionally redundant,
    # not a bug: every step inside run_startup_tasks() is idempotent by
    # design (ensure_owner_bootstrapped, ensure_default_plans_seeded, and
    # the migrate_*_schema() functions were all already safe to re-run in
    # v3), and this file must stay self-sufficient for any platform that
    # runs a bare `python main.py` with no separate release step at all.
    await run_startup_tasks()

    bot, dp = build_app()
    await bot.delete_webhook(drop_pending_updates=True)
    await set_bot_commands(bot)

    try:
        await asyncio.gather(
            dp.start_polling(bot),
            run_signal_trading_worker(),
            run_intelligence_worker(),
        )
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
