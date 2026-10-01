"""Shared in-process state: hot caches, pagination store, job registry."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# --- tiny TTL cache (hot search results) ---
_hot: dict[str, tuple[float, object]] = {}
HOT_TTL = 300


def hot_get(key: str):
    item = _hot.get(key)
    if item and time.time() - item[0] < HOT_TTL:
        return item[1]
    _hot.pop(key, None)
    return None


def hot_set(key: str, value: object) -> None:
    if len(_hot) > 2000:
        _hot.clear()
    _hot[key] = (time.time(), value)


# --- pagination / callback store ---
# token -> {"groups": [...], "query": str, "user_id": int, "created": ts}
_results: dict[str, dict] = {}
RESULTS_TTL = 900


def results_put(groups: list[dict], query: str, user_id: int) -> str:
    token = uuid.uuid4().hex[:10]
    _results[token] = {
        "groups": groups, "query": query, "user_id": user_id,
        "created": time.time(),
    }
    if len(_results) > 500:
        oldest = sorted(_results, key=lambda k: _results[k]["created"])[:100]
        for k in oldest:
            _results.pop(k, None)
    return token


def results_get(token: str) -> dict | None:
    item = _results.get(token)
    if item and time.time() - item["created"] < RESULTS_TTL:
        return item
    _results.pop(token, None)
    return None


# --- interactive /index setup sessions: admin user id -> dict ---
# {"step": "channel" | "options" | "opt:<key>",
#  "chat_id": int, "title": str,
#  "opts": {"skip": int, "from_id": int, "to_id": int, "limit": int},
#  "panel_msg_id": int | None}
#
# The in-memory dict is only an L1 cache — the ``index_sessions`` table
# is the source of truth, so a setup survives bot restarts and works
# even if two instances are briefly alive at once. Sessions expire
# after 30 minutes of inactivity.
_index_pending: dict[int, dict] = {}

PENDING_TTL = 1800


async def _pending_db_get(user_id: int):
    from sqlalchemy import select

    from app.config import settings
    from app.db import get_session_factory
    from app.models import IndexSession

    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        return (await s.execute(
            select(IndexSession).where(IndexSession.user_id == user_id)
        )).scalar_one_or_none()


async def pending_get(user_id: int) -> dict | None:
    data = _index_pending.get(user_id)
    if data is not None:
        return data
    try:
        from datetime import datetime, timezone

        row = await _pending_db_get(user_id)
        if row is None:
            return None
        updated = row.updated_at
        if updated is not None:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated).total_seconds()
            if age > PENDING_TTL:
                await pending_clear(user_id)
                return None
        data = dict(row.data or {})
        _index_pending[user_id] = data
        return data
    except Exception as exc:
        log.debug("pending_get failed: %s", exc)
        return None


async def pending_set(user_id: int, data: dict) -> None:
    _index_pending[user_id] = data
    try:
        from app.config import settings
        from app.db import get_session_factory
        from app.models import IndexSession

        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            row = (await s.get(IndexSession, user_id))
            payload = dict(data)
            if row is None:
                s.add(IndexSession(user_id=user_id, data=payload))
            else:
                row.data = payload
            await s.commit()
    except Exception as exc:
        log.debug("pending_set failed: %s", exc)


async def pending_clear(user_id: int) -> None:
    _index_pending.pop(user_id, None)
    try:
        from app.config import settings
        from app.db import get_session_factory
        from app.models import IndexSession

        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            row = await s.get(IndexSession, user_id)
            if row is not None:
                await s.delete(row)
                await s.commit()
    except Exception as exc:
        log.debug("pending_clear failed: %s", exc)


# --- /index job registry (shared between engine and handlers) ---
@dataclass
class IndexJob:
    job_id: int
    channel_ref: str
    task: asyncio.Task | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    progress_msg: object = None  # pyrogram Message being edited
    started_at: float = field(default_factory=time.time)


_index_jobs: dict[int, IndexJob] = {}


def job_register(job: IndexJob) -> None:
    _index_jobs[job.job_id] = job


def job_get(job_id: int) -> IndexJob | None:
    return _index_jobs.get(job_id)


def job_remove(job_id: int) -> None:
    _index_jobs.pop(job_id, None)


def job_active_for_channel(channel_id: int) -> IndexJob | None:
    for job in _index_jobs.values():
        if getattr(job, "channel_id", None) == channel_id and not job.task.done():
            return job
    return None
