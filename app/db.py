"""Async PostgreSQL engine + session factory."""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

log = logging.getLogger(__name__)

_engine = None
_factory = None
_engine_url: str | None = None


def _fix_sslmode(database_url: str) -> tuple[str, dict]:
    """Strip ``?sslmode=`` (a psycopg2-ism asyncpg rejects) and translate it
    to an SSL context asyncpg understands.

    Voroa-style DATABASE_URLs carry ``?sslmode=require``; passing that
    through makes asyncpg raise
    ``TypeError: connect() got an unexpected keyword argument 'sslmode'``.
    """
    import ssl as _ssl
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
    u = urlparse(database_url)
    q = dict(parse_qsl(u.query))
    sslmode = q.pop("sslmode", "").lower()
    connect_args: dict = {}
    if sslmode in ("require", "verify-ca", "verify-full"):
        ctx = _ssl.create_default_context()
        if sslmode == "require":
            # libpq "require" = encrypt, don't verify the cert.
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
        connect_args["ssl"] = ctx
    url = urlunparse(u._replace(query=urlencode(q)))
    return url, connect_args


def get_engine(database_url: str):
    global _engine, _engine_url
    if _engine is None:
        url, connect_args = _fix_sslmode(database_url)
        _engine = create_async_engine(
            url,
            connect_args=connect_args,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        _engine_url = database_url
    elif database_url != _engine_url:
        # Cached by design (single DB per process) — but a different URL
        # here is almost certainly a config bug; say so loudly.
        log.warning("get_engine called with a different DATABASE_URL; "
                    "reusing the cached engine")
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
