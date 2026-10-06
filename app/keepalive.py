"""DB keepalive — defeats idle-sleep on free-tier Postgres.

Every ``INTERVAL`` seconds runs a cheap ``SELECT 1`` so the database never
sits idle long enough to suspend. If the DB *was* asleep, this one query
takes the cold-start hit and everything after it is warm.

No Cloudflare Worker / external cron needed — the bot is already always-on.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

INTERVAL = 240  # seconds (under the typical ~5 min idle-sleep threshold)

_task: asyncio.Task | None = None


async def _worker() -> None:
    from sqlalchemy import text

    from app.config import settings
    from app.db import get_session_factory

    while True:
        await asyncio.sleep(INTERVAL)
        try:
            factory = get_session_factory(settings.DATABASE_URL)
            async with factory() as session:
                await session.execute(text("SELECT 1"))
            log.debug("db keepalive ping ok")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("db keepalive ping failed", exc_info=True)


def start() -> asyncio.Task:
    """Spawn the keepalive loop (idempotent)."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_worker())
        log.info("db keepalive started (every %ds)", INTERVAL)
    return _task
