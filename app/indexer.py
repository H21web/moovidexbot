"""Historical /index engine — pure MTProto, single bot client.

The BOT client fetches channel messages by ID batches via
``channels.GetMessages`` (bot-allowed) — NOT ``messages.getHistory``
(which Telegram blocks for bots). The end of history is bootstrapped
from a message the admin forwards (or a post link), the same trick
DreamX-family bots use. No user session needed.

Speed: ~100+ files/sec (metadata only — nothing is downloaded).

Features: live progress UI, skip=/from=/to=/limit=, /index cancel +
inline stop button, PostgreSQL checkpoints (resume after restart),
duplicate-safe (``ON CONFLICT DO NOTHING`` on file_id and on
(file_name, file_size) — reposts get fresh file_ids, so name+size is the
real duplicate key; duplicates count into "skipped").
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from pyrogram import Client, raw, utils
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
    """Insert rows, ignoring duplicates (file_id OR name+size).

    Returns inserted count — the rest were duplicates.
    """
    if not rows:
        return 0
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        # no index_elements: ANY unique violation (file_id or the
        # (file_name, file_size) constraint) skips the row
        stmt = pg_insert(File).values(rows).on_conflict_do_nothing()
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
                    elapsed: float, channel: str, done: bool = False,
                    pos: int = 0, total: int = 0) -> str:
    rate = scanned / elapsed if elapsed > 0 else 0.0
    mins, secs = divmod(int(elapsed), 60)
    pct_bar = "▰" * 10 if done else "▰" * min(10, int(rate / 10))
    bar = (pct_bar + "▱" * (10 - len(pct_bar))) if not done else "▰" * 10
    head = "✅ Indexing complete" if done else "📥 Indexing"
    where = (f"📍 Message <b>{pos:,}</b> / {total:,}\n"
             if total and not done else "")
    return (
        f"{head} {channel}\n"
        f"{bar}\n\n"
        f"{where}"
        f"📦 Indexed: <b>{indexed:,}</b>\n"
        f"🔍 Scanned: <b>{scanned:,}</b>\n"
        f"⏭️ Skipped: <b>{skipped:,}</b>\n"
        f"⚠️ Errors: <b>{errors:,}</b>\n\n"
        f"⚡ {rate:.0f} files/sec · ⏱ {mins:02d}:{secs:02d}"
    )


def normalize_channel_ref(text: str) -> str | int:
    """Turn user input into something get_chat/resolve understands.

    Accepts: t.me/c/12345/67 links, t.me/+invite / t.me/joinchat links,
    t.me/username links, @username, -10012345 ids, 10012345 or 12345 ids.
    Returns an int id when the input is numeric, else the original string.
    """
    text = (text or "").strip()
    m = re.search(r"t\.me/c/(\d+)", text)
    if m:
        return int("-100" + m.group(1))
    m = re.search(r"t\.me/(?:\+|joinchat/)([\w-]+)", text)
    if m:
        return text  # invite link — get_chat handles via CheckChatInvite
    m = re.search(r"t\.me/([A-Za-z][\w]{3,})", text)
    if m:
        return "@" + m.group(1)
    s = text.strip()
    if re.fullmatch(r"-?\d{5,}", s):
        num = int(s)
        if num > 0:
            digits = str(num)
            if digits.startswith("100"):
                digits = digits[3:]
            num = -int("100" + digits)
        return num
    return text


async def resolve_channel(client: Client, ref: str | int):
    """Resolve @username / invite link / id — including private channels.

    Works on the bot client — the bot must be admin/member of the
    channel for private-channel resolution to succeed.

    Strategy (in order):
    1. ``get_chat`` with a numeric id as int — Pyrogram then resolves via
       ``channels.GetChannels`` with access_hash=0, which Telegram honors
       for channels the bot can access. (Passing the id as a *string*
       misfires: resolve_peer treats digit-strings as phone numbers.)
    2. Dialog scan (title / username / id match).
    3. Raw ``channels.GetChannels`` with access_hash=0 + fetch_peers.
    """
    if isinstance(ref, str):
        ref = normalize_channel_ref(ref)
    last_exc: Exception | None = None

    # 1) direct get_chat — int ids take the GetChannels path
    try:
        return await client.get_chat(ref)
    except Exception as exc:
        last_exc = exc
        log.warning("get_chat(%r) failed: %r", ref, exc)

    # 2) dialog scan for private channels
    try:
        target_id = ref if isinstance(ref, int) else None
        needle = "" if isinstance(ref, int) else str(ref).lstrip("@").lower()
        async for dialog in client.get_dialogs():
            chat = dialog.chat
            if chat is None:
                continue
            if target_id is not None and chat.id == target_id:
                return chat
            if needle:
                title = (getattr(chat, "title", "") or "").lower()
                uname = (getattr(chat, "username", "") or "").lower()
                if needle in (uname, title):
                    return chat
    except Exception as exc:
        last_exc = exc
        log.warning("dialog scan failed for %r: %r", ref, exc)

    # 3) raw GetChannels with access_hash=0 (bot is admin/member)
    if isinstance(ref, int):
        try:
            raw_id = utils.get_channel_id(ref)
            r = await client.invoke(
                raw.functions.channels.GetChannels(
                    id=[raw.types.InputChannel(
                        channel_id=raw_id, access_hash=0)]
                )
            )
            await client.fetch_peers(r.chats)
            return await client.get_chat(ref)
        except Exception as exc:
            last_exc = exc
            log.warning("raw GetChannels failed for %r: %r", ref, exc)

    raise ValueError(
        f"channel not found / bot is not admin: {ref} ({last_exc})")


# IDs fetched per channels.GetMessages call (DreamX-family bots use 200).
FETCH_BATCH = 200


async def run_index_job(job: state.IndexJob, client: Client,
                       channel_ref: str, skip: int = 0,
                       from_id: int = 0, to_id: int = 0,
                       limit: int = 0, last_msg_id: int = 0,
                       on_progress=None) -> None:
    """Walk channel messages by ID batches and index files.

    Runs as an asyncio task. Uses channels.GetMessages in ID batches
    (bot-allowed) instead of messages.GetHistory (blocked for bots).
    ``last_msg_id`` bootstraps the end of history — taken from a message
    the admin forwarded (or a post link), the same trick DreamX-family
    bots use. Resume continues from the stored ``offset_id``.
    """
    job_id = job.job_id
    t0 = time.time()
    scanned = indexed = skipped = errors = 0
    offset_id = 0
    last_edit = 0.0
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
                    now - t0, channel_label,
                    pos=offset_id, total=end_id))
            except Exception as exc:
                log.debug("progress edit failed: %s", exc)

    async def flush():
        nonlocal indexed, skipped, batch
        if batch:
            try:
                inserted = await _bulk_insert(batch)
                indexed += inserted
                skipped += len(batch) - inserted  # duplicates
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

        # End bound: bootstrapped from the forwarded message / post link
        # (bots can't list history, so the admin supplies the latest id).
        end_id = to_id or last_msg_id
        if to_id and last_msg_id:
            end_id = min(to_id, last_msg_id)
        if not end_id:
            raise RuntimeError(
                "cannot determine the latest message id — forward a message "
                "from the channel or send a post link")
        start_id = max(from_id or 1, offset_id + 1)
        if start_id > end_id:
            await _checkpoint(job_id, scanned, indexed, skipped, errors,
                              offset_id, status="done")
            if on_progress:
                try:
                    await on_progress(
                        "✅ Nothing new to index — already reached message "
                        f"<b>{end_id:,}</b>.")
                except Exception:
                    pass
            log.info("index job %d: nothing to do (start %d > end %d)",
                     job_id, start_id, end_id)
            return

        await _checkpoint(job_id, scanned, indexed, skipped, errors,
                          offset_id, status="running")
        await edit_progress(force=True)

        n = 0
        cur = start_id
        while cur <= end_id:
            if job.cancel_event.is_set():
                break
            if limit and scanned >= limit:
                break
            ids = list(range(cur, min(cur + FETCH_BATCH, end_id + 1)))
            try:
                msgs = await client.get_messages(channel_id, ids)
            except FloodWait as exc:
                log.info("index job %d: floodwait %ss on get_messages",
                         job_id, exc.value)
                await asyncio.sleep(exc.value + 1)
                continue
            except Exception as exc:
                log.warning("get_messages failed for %d ids: %s",
                            len(ids), exc)
                errors += len(ids)
                scanned += len(ids)
                cur = ids[-1] + 1
                offset_id = ids[-1]
                await _checkpoint(job_id, scanned, indexed, skipped,
                                  errors, offset_id)
                continue
            if not isinstance(msgs, list):
                msgs = [msgs]
            for msg in msgs:
                if job.cancel_event.is_set():
                    break
                n += 1
                if n <= skip:
                    continue
                if limit and scanned >= limit:
                    break
                scanned += 1
                try:
                    rec = extract_record(msg, channel_id)
                    if rec is None:
                        skipped += 1  # deleted, non-media, or duplicate
                    else:
                        batch.append(rec)
                except Exception as exc:
                    log.debug("extract failed: %s", exc)
                    errors += 1

            await flush()
            cur = ids[-1] + 1
            offset_id = ids[-1]
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
