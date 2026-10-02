"""Auto-delete scheduler — the "background goroutine".

Bot-sent result/file messages are scheduled here instead of a bare
``asyncio.create_task(asyncio.sleep(...))`` per message: one worker task
owns a priority queue of (delete_at, chat_id, message_id), sleeps until
the next item is due, and deletes it. Survives nothing (in-memory), but
handles restarts gracefully — a missed delete is just a leftover message.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import time

log = logging.getLogger(__name__)

_queue: list[tuple[float, int, int, int]] = []  # (delete_at, seq, chat_id, msg_id)
_waker: asyncio.Event | None = None
_task: asyncio.Task | None = None
_seq = 0


async def schedule(chat_id: int, message_id: int, delay_seconds: int) -> None:
    """Schedule a message for deletion after `delay_seconds`."""
    global _seq
    if delay_seconds <= 0 or not chat_id or not message_id:
        return
    _seq += 1
    heapq.heappush(_queue, (time.monotonic() + delay_seconds,
                            _seq, int(chat_id), int(message_id)))
    if _waker is not None:
        _waker.set()


async def _delete(chat_id: int, message_id: int) -> None:
    from app.bot import app as bot_app  # late import: bot starts after web
    client = bot_app.bot
    if not client:
        return
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception as exc:
        log.debug("autodelete %s/%s failed: %s", chat_id, message_id, exc)


async def worker() -> None:
    """Background task — start once from main.py."""
    global _waker
    _waker = asyncio.Event()
    log.info("autodelete worker started")
    while True:
        if not _queue:
            _waker.clear()
            await _waker.wait()
            continue
        delete_at, _seq_no, chat_id, message_id = _queue[0]
        now = time.monotonic()
        if delete_at > now:
            _waker.clear()
            try:
                await asyncio.wait_for(_waker.wait(), timeout=delete_at - now)
            except asyncio.TimeoutError:
                pass
            continue
        heapq.heappop(_queue)
        await _delete(chat_id, message_id)


def start() -> asyncio.Task:
    """Spawn the worker (idempotent)."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(worker())
    return _task
