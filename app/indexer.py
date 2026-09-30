"""Historical /index engine — pure MTProto, single bot client.

The BOT client reads channel history directly via MTProto
(``messages.getHistory``) — this works when the bot is **admin** of the
channel, exactly like Tech VJ-style bots. No user session needed.

Speed: ~100+ files/sec (metadata only — nothing is downloaded).

Features: live progress UI, skip=/from=/to=/limit=, /index cancel +
inline stop button, PostgreSQL checkpoints (resume after restart),
duplicate-safe (``ON CONFLICT DO NOTHING`` on file_id).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.errors import FloodWait
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import state
from app.config import settings
from app.db import get_session_factory
from app.models import BackfillJob, File
from app.textutil import (
    clean_title,
    detect_quality_language,
    extract_year,
    title_key,
)

log = logging.getLogger(__name__)

EDIT_EVERY_SEC = 3.0
CHECKPOINT_EVERY = 2000


def _media_of(msg):
    """Return (media_obj, kind) for the first media on a message."""
    for kind in ("document", "video", "audio", "animation", "voice", "video_note"):
        media = getattr(msg, kind, None)
        if media is not None:
            return media, kind
    return None, None


def extract_record(msg, channel_id: int) -> dict | None:
    """Build a File row dict from a pyrogram Message. None = not indexable."""
    media, _kind = _media_of(msg)
    if media is None:
        return None
    file_id = getattr(media, "file_id", None)
    if not file_id:
        return None

    file_name = getattr(media, "file_name", None) or ""
    caption = (msg.caption or "").strip() or None
    quality, language = detect_quality_language(f"{file_name} {caption or ''}")

    date = msg.date
    if date is not None and date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)

    return {
        "file_id": file_id,
        "file_name": file_name or None,
        "file_size": getattr(media, "file_size", None),
        "mime_type": getattr(media, "mime_type", None),
        "caption": caption,
        "channel_id": channel_id,
        "message_id": msg.id,
        "quality": quality,
        "language": language,
        "title_key": title_key(file_name) or None,
        "width": getattr(media, "width", None),
        "height": getattr(media, "height", None),
        "duration": getattr(media, "duration", None),
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "posted_at": date,
    }


async def _bulk_insert(rows: list[dict]) -> int:
    """Insert rows, ignoring duplicates. Returns inserted count."""
    if not rows:
        return 0
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        stmt = pg_insert(File).values(rows).on_conflict_do_nothing(
            index_elements=["file_id"]
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount or 0


async def _checkpoint(job_id: int, scanned: int, indexed: int,
                     skipped: int, errors: int, offset_id: int,
                     status: str | None = None) -> None:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        values: dict = {
            "total_scanned": scanned,
            "total_indexed": indexed,
            "total_skipped": skipped,
            "total_errors": errors,
            "offset_id": offset_id,
            "stats": {
                "scanned": scanned, "indexed": indexed,
                "skipped": skipped, "errors": errors,
            },
        }
        if status:
            values["status"] = status
        await session.execute(
            update(BackfillJob).where(BackfillJob.id == job_id).values(**values)
        )
        await session.commit()


def format_progress(scanned: int, indexed: int, skipped: int, errors: int,
                    elapsed: float, channel: str, done: bool = False) -> str:
    rate = scanned / elapsed if elapsed > 0 else 0.0
    mins, secs = divmod(int(elapsed), 60)
    pct_bar = "▰" * 10 if done else "▰" * min(10, int(rate / 10))
    bar = (pct_bar + "▱" * (10 - len(pct_bar))) if not done else "▰" * 10
    head = "✅ Indexing complete" if done else "📥 Indexing"
    return (
        f"{head} {channel}\n"
        f"{bar}\n\n"
        f"📦 Indexed: <b>{indexed:,}</b>\n"
        f"🔍 Scanned: <b>{scanned:,}</b>\n"
        f"⏭️ Skipped: <b>{skipped:,}</b>\n"
        f"⚠️ Errors: <b>{errors:,}</b>\n\n"
        f"⚡ {rate:.0f} files/sec · ⏱ {mins:02d}:{secs:02d}"
    )


async def resolve_channel(client: Client, ref: str):
    """Resolve @username / invite link / id, with dialog scan for privates.

    Works on the bot client — the bot must be admin/member of the
    channel for private-channel resolution to succeed.
    """
    ref = ref.strip()
    try:
        return await client.get_chat(ref)
    except Exception:
        pass
    # Private channel: scan dialogs for a title/id match.
    needle = ref.lstrip("@").lower()
    try:
        async for dialog in client.get_dialogs():
            chat = dialog.chat
            if chat is None:
                continue
            title = (getattr(chat, "title", "") or "").lower()
            uname = (getattr(chat, "username", "") or "").lower()
            if needle in (uname, title) or ref == str(chat.id):
                return chat
    except Exception as exc:
        log.debug("dialog scan failed: %s", exc)
    raise ValueError(f"channel not found / bot is not admin: {ref}")


async def run_index_job(job: state.IndexJob, client: Client,
                       channel_ref: str, skip: int = 0,
                       from_id: int = 0, to_id: int = 0,
                       limit: int = 0,
                       on_progress=None) -> None:
    """Walk channel history and index files. Runs as an asyncio task."""
    job_id = job.job_id
    t0 = time.time()
    scanned = indexed = skipped = errors = 0
    offset_id = 0
    last_edit = 0.0
    seen_in_run: set[int] = set()
    batch: list[dict] = []
    channel_label = channel_ref

    async def edit_progress(force: bool = False):
        nonlocal last_edit
        now = time.time()
        if on_progress and (force or now - last_edit >= EDIT_EVERY_SEC):
            last_edit = now
            try:
                await on_progress(format_progress(
                    scanned, indexed, skipped, errors,
                    now - t0, channel_label))
            except Exception as exc:
                log.debug("progress edit failed: %s", exc)

    async def flush():
        nonlocal indexed, batch
        if batch:
            try:
                indexed += await _bulk_insert(batch)
            except Exception as exc:
                log.warning("bulk insert failed (%d rows): %s", len(batch), exc)
                errors += len(batch)
            batch = []

    try:
        chat = await resolve_channel(client, channel_ref)
        channel_id = chat.id
        job.channel_id = channel_id
        channel_label = getattr(chat, "title", None) or channel_ref
        # Resume: continue from the stored offset if this channel ran before.
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            row = (await session.execute(
                select(BackfillJob).where(BackfillJob.id == job_id))).scalar_one()
            if row.offset_id and not from_id:
                offset_id = row.offset_id
            scanned, indexed = row.total_scanned, row.total_indexed
            skipped, errors = row.total_skipped, row.total_errors

        await _checkpoint(job_id, scanned, indexed, skipped, errors,
                          offset_id, status="running")
        await edit_progress(force=True)

        n = 0
        async for msg in client.get_chat_history(chat.id, offset_id=offset_id or None):
            if job.cancel_event.is_set():
                break
            if not msg or msg.id in seen_in_run:
                continue
            seen_in_run.add(msg.id)
            n += 1
            if n <= skip:
                continue
            if from_id and msg.id < from_id:
                continue
            if to_id and msg.id > to_id:
                break
            if limit and scanned >= limit:
                break

            offset_id = msg.id
            scanned += 1
            try:
                rec = extract_record(msg, channel_id)
                if rec is None:
                    skipped += 1
                else:
                    batch.append(rec)
                    if len(batch) >= settings.INDEX_BATCH_SIZE:
                        await flush()
            except Exception as exc:
                log.debug("extract failed for msg %d: %s", msg.id, exc)
                errors += 1

            if scanned % CHECKPOINT_EVERY == 0:
                await flush()
                await _checkpoint(job_id, scanned, indexed, skipped,
                                  errors, offset_id)
            await edit_progress()

        await flush()
        cancelled = job.cancel_event.is_set()
        final = "cancelled" if cancelled else "done"
        await _checkpoint(job_id, scanned, indexed, skipped, errors,
                          offset_id, status=final)
        if on_progress:
            try:
                await on_progress(format_progress(
                    scanned, indexed, skipped, errors,
                    time.time() - t0, channel_label, done=True)
                    + ("\n\n🛑 Cancelled by admin." if cancelled else
                       "\n\n🎉 All files indexed."))
            except Exception:
                pass
        log.info("index job %d %s: %d indexed / %d scanned",
                 job_id, final, indexed, scanned)
    except FloodWait as exc:
        log.warning("index job %d floodwait %ds", job_id, exc.value)
        await _checkpoint(job_id, scanned, indexed, skipped, errors,
                          offset_id, status="error",
                          )
        raise
    except asyncio.CancelledError:
        await flush()
        await _checkpoint(job_id, scanned, indexed, skipped, errors,
                          offset_id, status="cancelled")
        raise
    except Exception as exc:
        log.exception("index job %d failed", job_id)
        await flush()
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            await session.execute(
                update(BackfillJob).where(BackfillJob.id == job_id).values(
                    status="error", error=str(exc)[:500],
                    total_scanned=scanned, total_indexed=indexed,
                    total_skipped=skipped, total_errors=errors,
                    offset_id=offset_id))
            await session.commit()
        if on_progress:
            try:
                await on_progress(f"❌ Indexing failed: {str(exc)[:200]}")
            except Exception:
                pass
    finally:
        state.job_remove(job_id)


def parse_index_args(text: str) -> dict:
    """Parse '/index @chan skip=100 from=1 to=500 limit=1000'."""
    args = {"channel": None, "skip": 0, "from_id": 0, "to_id": 0, "limit": 0}
    parts = (text or "").split()
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            k = k.strip().lower().lstrip("-")
            try:
                num = int(re.sub(r"[^\d]", "", v) or 0)
            except ValueError:
                num = 0
            if k == "skip":
                args["skip"] = num
            elif k in ("from", "from_id"):
                args["from_id"] = num
            elif k == "to":
                args["to_id"] = num
            elif k == "limit":
                args["limit"] = num
        elif args["channel"] is None and not p.startswith("/"):
            if p.lower() == "cancel":
                continue  # handled by the /index cancel branch in the handler
            args["channel"] = p
    return args
