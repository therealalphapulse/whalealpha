"""Seeds a handful of CLEARLY FAKE example wallets — port of scripts/seed.ts.

So the bot is runnable end-to-end locally. These are not real Solana
addresses with real trading history — replace with data from your licensed
discovery source before going anywhere near production.

Run with: `python -m scripts.seed` (from the project root, with DATABASE_URL set).
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from whale_alpha.config import get_env
from whale_alpha.db.models import Role, User, WalletStatus, WhaleWallet
from whale_alpha.db.session import create_engine, create_session_factory

EXAMPLE_WALLETS = [
    {
        "address": "11111111111111111111111111111111111111111",  # placeholder, not a real trading wallet
        "label": "EXAMPLE-Alpha-1 (fake seed data)",
        "score": 87,
        "confidence": 74,
        "roi_30d": 0.62,
        "win_rate": 0.71,
        "trade_success_rate": 0.68,
        "max_drawdown": 0.18,
        "trade_frequency_7d": 12,
        "wallet_age_days": 210,
        "avg_position_usd": 3200,
    },
    {
        "address": "22222222222222222222222222222222222222222",
        "label": "EXAMPLE-Beta-1 (fake seed data)",
        "score": 63,
        "confidence": 55,
        "roi_30d": 0.21,
        "win_rate": 0.58,
        "trade_success_rate": 0.55,
        "max_drawdown": 0.31,
        "trade_frequency_7d": 8,
        "wallet_age_days": 95,
        "avg_position_usd": 1200,
    },
]


async def main() -> None:
    env = get_env()
    engine = create_engine(env)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        result = await session.execute(select(User).where(User.telegram_id == "0"))
        admin = result.scalar_one_or_none()
        if admin is None:
            admin = User(telegram_id="0", role=Role.SUPERADMIN)
            session.add(admin)
            await session.commit()
            await session.refresh(admin)

        for w in EXAMPLE_WALLETS:
            existing = await session.execute(
                select(WhaleWallet).where(WhaleWallet.address == w["address"])
            )
            if existing.scalar_one_or_none() is not None:
                continue
            session.add(
                WhaleWallet(
                    **w,
                    status=WalletStatus.APPROVED,
                    added_by_admin_id=admin.id,
                )
            )
        await session.commit()

    print(f"Seeded {len(EXAMPLE_WALLETS)} example wallets (fake data) + 1 superadmin user.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
