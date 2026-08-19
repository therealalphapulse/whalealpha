"""Async SQLAlchemy engine/session factory.

Analogous to instantiating `new PrismaClient()` and calling `$connect()` in the
original `src/index.ts`. Railway injects `DATABASE_URL` for the managed
Postgres service — we normalize the scheme to the async driver
(`postgresql+asyncpg://`) if a plain `postgresql://` URL is supplied, since
that's what Railway's `${{Postgres.DATABASE_URL}}` reference produces.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from whale_alpha.config import Env


def _to_async_dsn(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


def create_engine(env: Env) -> AsyncEngine:
    return create_async_engine(_to_async_dsn(env.DATABASE_URL), pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker[AsyncSession](engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
