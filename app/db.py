"""Async PostgreSQL engine + session factory."""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

log = logging.getLogger(__name__)

_engine = None
_factory = None


def get_engine(database_url: str):
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            database_url,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=300,
        )
    return _engine


def get_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(
            get_engine(database_url), class_=AsyncSession, expire_on_commit=False
        )
    return _factory


async def bump_file_downloads(file_db_id: int) -> None:
    """+1 download counter for a file (fire-and-forget safe)."""
    from app.config import settings  # lazy: avoids import cycles

    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            await session.execute(
                text("UPDATE files SET downloads = downloads + 1 "
                     "WHERE id = :id"),
                {"id": file_db_id},
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - counter must never break delivery
        log.debug("bump downloads failed: %s", exc)
