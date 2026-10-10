"""Multi-DB sharding — sequential fill.

The ``files`` table (the only large table) is sharded across N PostgreSQL
databases. Shards fill **sequentially**: all writes go to the first shard
whose size is under ``SHARD_SIZE_MB``; when it fills, writes rotate to the
next empty shard. Adding capacity later = append a URL to ``DATABASE_URLS``;
no rebalancing, no downtime.

Reads fan out to every shard in parallel and merge in Python. File identity
across shards uses a *global id*::

    global_id = shard_index * SHARD_ID_MULT + local_id

``SHARD_ID_MULT`` (100M) is far above any per-shard row count, and old
single-DB ids (< 100M) decode to ``(shard=0, id)`` automatically, so every
existing callback/token carrying a plain ``files.id`` keeps working.

All *small* tables (users, groups, saved_files, requests, logs, settings,
jobs, …) live on shard 0 only and keep using ``settings.DATABASE_URL``
(which must be the first entry of ``DATABASE_URLS``).
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db import _fix_sslmode

log = logging.getLogger(__name__)

#: Local ``files.id`` values are always far below this; the multiplier
#: must exceed the maximum rows any single shard will ever hold.
SHARD_ID_MULT = 100_000_000

_factories: list | None = None


def shard_urls() -> list[str]:
    """All shard DATABASE_URLs in order; shard 0 is the primary."""
    from app.config import settings  # lazy: avoids import cycles

    raw = (getattr(settings, "DATABASE_URLS", "") or "").strip()
    if raw:
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if urls:
            primary = settings.DATABASE_URL.strip().rstrip("/")
            if urls[0].rstrip("/") != primary:
                # Small tables live on DATABASE_URL while files shard 0
                # would be urls[0] — a split brain. Say so loudly.
                log.error(
                    "sharding misconfigured: DATABASE_URLS[0] != "
                    "DATABASE_URL. Small tables and shard 0 will diverge!"
                )
            return urls
    return [settings.DATABASE_URL]


def get_shard_factories() -> list:
    """One async session factory per shard (cached)."""
    global _factories
    if _factories is None:
        _factories = []
        for url in shard_urls():
            clean, connect_args = _fix_sslmode(url)
            # Small pools: Supabase free allows ~60 direct connections;
            # 4 shards × 6 = 24 leaves room for the legacy shard-0 pool.
            engine = create_async_engine(
                clean,
                connect_args=connect_args,
                pool_size=4,
                max_overflow=2,
                pool_pre_ping=True,
                pool_recycle=300,
            )
            _factories.append(
                async_sessionmaker(
                    engine, class_=AsyncSession, expire_on_commit=False
                )
            )
        log.info("sharding: %d shard(s) configured", len(_factories))
    return _factories


def num_shards() -> int:
    """Number of configured shards (no engines built)."""
    return len(shard_urls())


def encode_gid(shard: int, local_id: int) -> int:
    """Pack (shard, local files.id) into one global id for callbacks."""
    return shard * SHARD_ID_MULT + int(local_id)


def decode_gid(gid: int) -> tuple[int, int]:
    """Unpack a global id → (shard, local files.id).

    Plain old single-DB ids (< SHARD_ID_MULT) decode to shard 0,
    so pre-sharding callbacks/tokens keep working untouched.
    """
    gid = int(gid)
    shard, local_id = divmod(gid, SHARD_ID_MULT)
    if shard >= num_shards():
        # Defensive: id from a shard that no longer exists (config
        # shrank) — treat as a legacy plain id on shard 0.
        log.warning("decode_gid: shard %d out of range for id %d", shard, gid)
        return 0, gid
    return shard, local_id


async def shard_size_mb(factory) -> float:
    """Current size of one shard's database in MB."""
    try:
        async with factory() as session:
            val = (
                await session.execute(
                    text("SELECT pg_database_size(current_database())")
                )
            ).scalar()
            return float(val or 0) / (1024 * 1024)
    except Exception as exc:  # noqa: BLE001 - size check must not break writes
        log.warning("shard size check failed: %s", exc)
        return 0.0


async def all_shard_sizes_mb() -> list[float]:
    """Sizes of every shard in MB, in shard order (parallel)."""
    factories = get_shard_factories()
    return list(await asyncio.gather(*(shard_size_mb(f) for f in factories)))


async def write_shard_index() -> int:
    """Index of the shard new ``files`` rows go to.

    First shard whose size is under ``SHARD_SIZE_MB``. Raises
    RuntimeError when every shard is full — the operator then appends
    a fresh DATABASE_URL to ``DATABASE_URLS`` and restarts.
    """
    from app.config import settings  # lazy: avoids import cycles

    limit = float(getattr(settings, "SHARD_SIZE_MB", 400) or 400)
    sizes = await all_shard_sizes_mb()
    for idx, size in enumerate(sizes):
        if size < limit:
            if idx > 0:
                log.info(
                    "sharding: write shard rotated to #%d (%.0f MB)",
                    idx, size,
                )
            return idx
    raise RuntimeError(
        f"all {len(sizes)} shards are over {limit:.0f} MB — "
        "append a new database URL to DATABASE_URLS"
    )


async def fanout(fn):
    """Run ``await fn(shard_idx, session)`` on every shard in parallel.

    Returns per-shard results in shard order. A failing shard yields
    ``None`` instead of failing the whole fan-out (degraded, not dead).
    """
    factories = get_shard_factories()

    async def _one(idx: int):
        try:
            async with factories[idx]() as session:
                return await fn(idx, session)
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash
            log.warning("shard #%d query failed: %s", idx, exc)
            return None

    return list(await asyncio.gather(*(_one(i) for i in range(len(factories)))))


async def total_files() -> int:
    """Total ``files`` rows across all shards (for admin stats)."""
    from sqlalchemy import func, select

    from app.models import File

    async def _count(idx: int, session) -> int:
        return (
            await session.execute(select(func.count(File.id)))
        ).scalar() or 0

    counts = await fanout(_count)
    return sum(c or 0 for c in counts)


async def total_file_bytes() -> int:
    """Total ``files.file_size`` across all shards (for admin stats)."""
    from sqlalchemy import func, select

    from app.models import File

    async def _sum(idx: int, session) -> int:
        return (
            await session.execute(
                select(func.coalesce(func.sum(File.file_size), 0)))
        ).scalar() or 0

    sums = await fanout(_sum)
    return sum(s or 0 for s in sums)
