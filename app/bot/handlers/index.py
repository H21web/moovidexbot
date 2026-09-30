"""/index command + real-time auto-index of new channel posts.

Both historical backfill (``/index``) and live auto-index run on the
SINGLE bot client over MTProto:

* bots CAN read channel history via ``messages.getHistory`` — the bot
  must be **admin** in the channel (same as Tech VJ-style bots;
  no user session needed).
* new posts in channels where the bot is admin are auto-indexed live
  as they arrive.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import state
from app.bot import ui
from app.bot.handlers.common import admin_only
from app.config import settings
from app.db import get_session_factory
from app.indexer import (
    extract_record,
    format_progress,
    parse_index_args,
    run_index_job,
)
from app.models import BackfillJob, File

log = logging.getLogger(__name__)

USAGE = (
    "📥 <b>/index usage</b>\n\n"
    "<code>/index @channel</code> — index full history\n"
    "<code>/index @channel skip=1000</code>\n"
    "<code>/index @channel from=5000 to=90000</code>\n"
    "<code>/index @channel limit=5000</code>\n"
    "<code>/index cancel</code> — stop the running job\n\n"
    "⚠️ The bot must be <b>admin</b> in the channel.\n"
    "New posts in admin channels are auto-indexed live."
)


@admin_only
async def _index(client: Client, message: Message):
    args = parse_index_args(message.text or "")

    # --- /index cancel ---
    if (message.text or "").strip().lower() in ("/index cancel", "/cancel"):
        stopped = 0
        for job_id, job in list(state._index_jobs.items()):
            if job.task and not job.task.done():
                job.cancel_event.set()
                stopped += 1
        await message.reply_text(
            f"🛑 Stopped {stopped} job(s)." if stopped else "No running jobs.")
        return

    if not args["channel"]:
        # show active job or usage
        for job in state._index_jobs.values():
            if job.task and not job.task.done():
                await message.reply_text(
                    f"📥 Indexing <b>{job.channel_ref}</b>…\n"
                    f"Use <code>/index cancel</code> to stop.")
                return
        await message.reply_text(USAGE)
        return

    channel_ref = args["channel"]
    # one job per channel at a time
    for job in state._index_jobs.values():
        if (job.channel_ref == channel_ref and job.task
                and not job.task.done()):
            await message.reply_text("⚠️ Already indexing this channel.")
            return

    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        db_job = BackfillJob(
            run_token=uuid.uuid4().hex[:16],
            channel_ref=channel_ref,
            status="pending",
            started_at=datetime.now(timezone.utc),
        )
        session.add(db_job)
        await session.commit()
        job_id = db_job.id

    prog = await message.reply_text(
        f"📥 <i>Starting index of {channel_ref}…</i>\n"
        f"<i>Bot must be admin in the channel.</i>",
        reply_markup=ui.index_stop_kb(job_id))

    async def on_progress(text: str):
        try:
            await prog.edit_text(text, reply_markup=ui.index_stop_kb(job_id))
        except Exception:
            pass

    job = state.IndexJob(job_id=job_id, channel_ref=channel_ref,
                         progress_msg=prog)
    state.job_register(job)
    # NOTE: `client` here IS the bot client — it reads channel history
    # itself via MTProto (works when the bot is admin of the channel).
    job.task = asyncio.create_task(run_index_job(
        job, client, channel_ref,
        skip=args["skip"], from_id=args["from_id"],
        to_id=args["to_id"], limit=args["limit"],
        on_progress=on_progress))
    log.info("started index job %d for %s", job_id, channel_ref)


async def _auto_index(client: Client, message: Message):
    """Index new channel posts live (bot receives these as admin)."""
    try:
        rec = extract_record(message, message.chat.id)
        if not rec:
            return
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            stmt = pg_insert(File).values(rec).on_conflict_do_nothing(
                index_elements=["file_id"])
            await session.execute(stmt)
            await session.commit()
    except Exception as exc:
        log.debug("auto-index failed: %s", exc)


def register(bot: Client) -> None:
    bot.on_message(filters.private & filters.command("index"))(_index)
    bot.on_message(filters.channel)(_auto_index)
