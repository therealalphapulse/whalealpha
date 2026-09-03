"""
One-off reset for the quota-governor deadlock fix (domain/signals/quota.py).

Deletes the two stuck SystemFlag rows written by the old, buggy logic:

  - conviction_score_cutoff          (frozen at 80.0)
  - conviction_cutoff_last_adjusted  (stamped with today's date, which makes
    maybe_adjust_cutoff() skip re-evaluating until the next UTC day)

Leaves conviction_qualify_history alone — it's harmless and the fixed code
will now actually populate it with real data going forward.

Safe to run more than once (rows just won't exist the second time). Reads
the same DATABASE_URL / async_session your app already uses, so run it in
the same environment as production (e.g. `railway run python
scripts/reset_quota_cutoff.py`), not on a laptop pointed at a different DB.

Usage:
    python3 scripts/reset_quota_cutoff.py
"""

import asyncio
import logging

from sqlalchemy import delete

from infra.db.session import async_session
from models.system_flag import SystemFlag

logger = logging.getLogger("AlphaPulse.ResetQuotaCutoff")

KEYS_TO_CLEAR = [
    "conviction_score_cutoff",
    "conviction_cutoff_last_adjusted",
]


async def main() -> None:
    async with async_session() as session:
        result = await session.execute(
            delete(SystemFlag).where(SystemFlag.key.in_(KEYS_TO_CLEAR))
        )
        await session.commit()

    deleted = result.rowcount if result.rowcount is not None else "unknown number of"
    print(f"Cleared {deleted} SystemFlag row(s): {KEYS_TO_CLEAR}")
    print(
        "Next call to maybe_adjust_cutoff() will treat this as a fresh "
        "evaluation instead of skipping until the next UTC day."
    )


if __name__ == "__main__":
    asyncio.run(main())
