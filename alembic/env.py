"""Alembic environment — async engine, models metadata."""
from __future__ import annotations

import asyncio
import os
import sys

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.config import settings  # noqa: E402
from app.models import Base  # noqa: E402

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = settings.DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_async_engine(_url())

    async def _run():
        async with engine.connect() as conn:
            await conn.run_sync(_do_run)

    def _do_run(sync_conn):
        context.configure(connection=sync_conn,
                          target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

    asyncio.run(_run())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
