"""Analytics helpers: log events, build daily series for the dashboard."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import settings
from app.db import get_session_factory
from app.models import ActivityLog, EventLog, File, MovieRequest, SearchLog, User

log = logging.getLogger(__name__)

# v10.8.10: activity-log retention — dashboard DB rows older than this
# are pruned (the log channel keeps the permanent copy).
ACTIVITY_RETENTION_DAYS = 30

# search -> pm/group split for the activity log
def _activity_kind(kind: str, chat_id: int | None) -> str:
    if kind == "search":
        return "search_group" if chat_id and chat_id < 0 else "search_pm"
    return kind


def _get_bot():
    """Lazy bot client (avoids a circular import at module load)."""
    try:
        from app.bot import app as bot_app
        return bot_app.bot
    except Exception:
        return None


async def _send_to_log_channel(text: str) -> None:
    chan = (settings.LOG_CHANNEL or "").strip()
    if not chan:
        return
    client = _get_bot()
    if not client:
        return
    try:
        from pyrogram.enums import ParseMode

        ref = int(chan) if chan.lstrip("-").isdigit() else chan
        await client.send_message(ref, text, parse_mode=ParseMode.HTML,
                                  disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("activity log-channel send failed: %s", exc)


async def log_event(kind: str, user_id: int | None = None,
                    chat_id: int | None = None,
                    detail: str | None = None) -> None:
    """Fire-and-forget analytics event (never breaks the caller).

    v10.8.10: also writes the human-readable ActivityLog row (AI usage,
    pm/group searches, requests, downloads, starts) and mirrors it to
    the log channel.
    """
    a_kind = _activity_kind(kind, chat_id)
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            s.add(EventLog(kind=kind, user_id=user_id, chat_id=chat_id))
            s.add(ActivityLog(kind=a_kind, user_id=user_id, chat_id=chat_id,
                              detail=(detail or "")[:500] or None))
            await s.commit()
    except Exception as exc:
        log.debug("log_event %s failed: %s", kind, exc)
        return
    # Mirror to the log channel (fire-and-forget inside fire-and-forget).
    labels = {"search_pm": "Search", "search_group": "Group search",
              "ai": "AI search", "request": "Request",
              "download": "Download", "start": "Start"}
    icon = {"search_pm": "🔍", "search_group": "👪", "ai": "🤖",
            "request": "🎞", "download": "📥", "start": "▶️"}.get(
                a_kind, "📝")
    line = f"{icon} <b>{labels.get(a_kind, a_kind)}</b>"
    if user_id:
        line += f"\n👤 <code>{user_id}</code>"
        if a_kind == "search_group" and chat_id:
            line += f" · 👪 <code>{chat_id}</code>"
    if detail:
        line += f"\n💬 {html.escape((detail or '')[:200])}"
    asyncio.create_task(_send_to_log_channel(line))


async def prune_activity_logs(days: int = ACTIVITY_RETENTION_DAYS) -> int:
    """Delete activity-log rows older than ``days``. Returns rows deleted."""
    from sqlalchemy import delete as sa_delete

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            res = await s.execute(
                sa_delete(ActivityLog).where(ActivityLog.created_at < cutoff))
            await s.commit()
            n = res.rowcount or 0
            if n:
                log.info("pruned %d activity-log rows older than %dd",
                         n, days)
            return n
    except Exception as exc:  # noqa: BLE001
        log.debug("prune_activity_logs failed: %s", exc)
        return 0


_prune_task: asyncio.Task | None = None


async def _prune_worker() -> None:
    while True:
        await asyncio.sleep(24 * 3600)
        await prune_activity_logs()


def start_prune_task() -> asyncio.Task:
    """Spawn the daily activity-log pruner (idempotent)."""
    global _prune_task
    if _prune_task is None or _prune_task.done():
        _prune_task = asyncio.create_task(_prune_worker())
    return _prune_task


async def _daily_on(session, table, date_col, days: int,
                  kind: str | None, since, out: dict) -> None:
    """Fill ``out`` daily counts using one session."""
    q = (select(func.date_trunc("day", date_col).label("d"),
                func.count().label("c"))
         .where(date_col >= since)
         .group_by("d"))
    if kind is not None:
        q = q.where(table.kind == kind)
    for day, count in (await session.execute(q)).all():
        key = day.strftime("%Y-%m-%d")
        if key in out:
            out[key] = out.get(key, 0) + int(count)


async def _daily(table, date_col, days: int = 30,
                 kind: str | None = None) -> dict[str, int]:
    """{YYYY-MM-DD: count} for the last `days` days (UTC)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    factory = get_session_factory(settings.DATABASE_URL)
    out = {(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d"): 0
           for i in range(days)}
    try:
        async with factory() as s:
            await _daily_on(s, table, date_col, days, kind, since, out)
    except Exception as exc:
        log.debug("analytics _daily failed: %s", exc)
    return out


async def _daily_sharded(table, date_col, days: int = 30,
                         kind: str | None = None) -> dict[str, int]:
    """``_daily`` summed across every shard (for the sharded tables)."""
    from app.db_shard import fanout

    since = datetime.now(timezone.utc) - timedelta(days=days)
    out = {(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d"): 0
           for i in range(days)}

    async def _one(idx: int, session):
        shard_out: dict[str, int] = {}
        try:
            await _daily_on(session, table, date_col, days, kind, since,
                            shard_out)
        except Exception as exc:
            log.debug("analytics shard #%d _daily failed: %s", idx, exc)
        return shard_out

    for part in await fanout(_one):
        for k, v in (part or {}).items():
            if k in out:
                out[k] += v
    return out


async def overview(days: int = 30) -> dict:
    """Everything the dashboard homepage needs."""
    # 6 independent daily series -> run concurrently, not sequentially.
    # v10.13 sharding: new_files spans all shards; the rest are shard 0.
    searches, downloads, starts, new_users, new_files, new_requests = \
        await asyncio.gather(
            _daily(SearchLog, SearchLog.created_at, days),
            _daily(EventLog, EventLog.created_at, days, kind="download"),
            _daily(EventLog, EventLog.created_at, days, kind="start"),
            _daily(User, User.joined_at, days),
            _daily_sharded(File, File.created_at, days),
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
