"""Analytics helpers: log events, build daily series for the dashboard."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import settings
from app.db import get_session_factory
from app.models import EventLog, File, MovieRequest, SearchLog, User

log = logging.getLogger(__name__)


async def log_event(kind: str, user_id: int | None = None,
                    chat_id: int | None = None) -> None:
    """Fire-and-forget analytics event (never breaks the caller)."""
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            s.add(EventLog(kind=kind, user_id=user_id, chat_id=chat_id))
            await s.commit()
    except Exception as exc:
        log.debug("log_event %s failed: %s", kind, exc)


async def _daily(table, date_col, days: int = 30,
                 kind: str | None = None) -> dict[str, int]:
    """{YYYY-MM-DD: count} for the last `days` days (UTC)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    factory = get_session_factory(settings.DATABASE_URL)
    out = {(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d"): 0
           for i in range(days)}
    try:
        async with factory() as s:
            q = (select(func.date_trunc("day", date_col).label("d"),
                        func.count().label("c"))
                 .where(date_col >= since)
                 .group_by("d"))
            if kind is not None:
                q = q.where(table.kind == kind)
            for day, count in (await s.execute(q)).all():
                key = day.strftime("%Y-%m-%d")
                if key in out:
                    out[key] = int(count)
    except Exception as exc:
        log.debug("analytics _daily failed: %s", exc)
    return out


async def overview(days: int = 30) -> dict:
    """Everything the dashboard homepage needs."""
    # 6 independent daily series -> run concurrently, not sequentially.
    searches, downloads, starts, new_users, new_files, new_requests = \
        await asyncio.gather(
            _daily(SearchLog, SearchLog.created_at, days),
            _daily(EventLog, EventLog.created_at, days, kind="download"),
            _daily(EventLog, EventLog.created_at, days, kind="start"),
            _daily(User, User.joined_at, days),
            _daily(File, File.created_at, days),
            _daily(MovieRequest, MovieRequest.created_at, days),
        )

    def total(d: dict[str, int], n: int) -> int:
        keys = sorted(d)[-n:]
        return sum(d[k] for k in keys)

    return {
        "days": days,
        "daily": {
            "searches": searches, "downloads": downloads, "starts": starts,
            "new_users": new_users, "new_files": new_files,
            "new_requests": new_requests,
        },
        "totals": {
            "today": {k: total(v, 1) for k, v in
                      (("searches", searches), ("downloads", downloads),
                       ("starts", starts), ("new_users", new_users),
                       ("new_files", new_files), ("new_requests", new_requests))},
            "week": {k: total(v, 7) for k, v in
                     (("searches", searches), ("downloads", downloads),
                      ("starts", starts), ("new_users", new_users),
                      ("new_files", new_files), ("new_requests", new_requests))},
            "month": {k: total(v, 30) for k, v in
                      (("searches", searches), ("downloads", downloads),
                       ("starts", starts), ("new_users", new_users),
                       ("new_files", new_files), ("new_requests", new_requests))},
        },
    }
