"""/index command + real-time auto-index of new channel posts.

Both historical backfill (``/index``) and live auto-index run on the
SINGLE bot client over MTProto:

* bots CAN read channel history via ``messages.getHistory`` — the bot
  must be **admin** in the channel (same as Tech VJ-style bots;
  no user session needed).
* new posts in channels where the bot is admin are auto-indexed live
  as they arrive.

Two ways to index history:

1. Interactive: send ``/index`` with no arguments and follow the
   buttons — forward a message from the channel, or send its link /
   @username / id, then tweak skip/limit/from/to and hit Start.
2. Direct: ``/index @channel skip=1000 limit=5000`` (one shot).
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone

from pyrogram import Client, StopPropagation, filters
from pyrogram.types import CallbackQuery, Message
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
    normalize_channel_ref,
    parse_index_args,
    resolve_channel,
    run_index_job,
)
from app.models import BackfillJob, File

log = logging.getLogger(__name__)

USAGE = (
    "📥 <b>/index usage</b>\n\n"
    "<code>/index</code> — interactive setup (buttons)\n"
    "<code>/index @channel</code> — index full history\n"
    "<code>/index @channel skip=1000</code>\n"
    "<code>/index @channel from=5000 to=90000</code>\n"
    "<code>/index @channel limit=5000</code>\n"
    "<code>/index cancel</code> — stop the running job\n\n"
    "⚠️ The bot must be <b>admin</b> in the channel.\n"
    "New posts in admin channels are auto-indexed live."
)

SETUP_PROMPT = (
    "📥 <b>New index</b>\n\n"
    "Send me the channel — any of these:\n"
    "• Forward a message <b>from the channel</b>\n"
    "• Channel link: <code>t.me/c/...</code> or <code>t.me/+...</code>\n"
    "• <code>@username</code> or <code>-100...</code> ID\n\n"
    "Direct form still works too:\n"
    "<code>/index @channel limit=5000</code>"
)

OPT_LABELS = {
    "skip": "Skip — messages to skip from the newest",
    "limit": "Limit — max files to index",
    "from_id": "From message ID",
    "to_id": "To message ID",
}


def extract_channel_ref(message: Message):
    """Channel ref from a forwarded message, else the message text/caption."""
    fwd = message.forward_from_chat
    if fwd is not None:
        return fwd.id
    text = (message.text or message.caption or "").strip()
    return text or None


def _setup_text(pending: dict) -> str:
    opts = pending["opts"]

    def fmt(v: int, off: str = "—") -> str:
        return f"{v:,}" if v else off

    return (
        f"📁 <b>{pending['title']}</b>\n"
        f"<code>{pending['chat_id']}</code>\n\n"
        "⚙️ <b>Options</b> — tap a button to change:\n"
        f"⏭ Skip: <b>{fmt(opts['skip'], '0')}</b> · "
        f"🔢 Limit: <b>{fmt(opts['limit'])}</b>\n"
        f"⬇️ From msg: <b>{fmt(opts['from_id'])}</b> · "
        f"⬆️ To msg: <b>{fmt(opts['to_id'])}</b>\n\n"
        "Tap <b>▶️ Start indexing</b> when ready.\n"
        "⚠️ The bot must be <b>admin</b> in the channel."
    )


async def _start_job(client: Client, progress_msg: Message,
                     channel_ref: str, skip: int = 0,
                     from_id: int = 0, to_id: int = 0,
                     limit: int = 0) -> None:
    """Create the DB job row and launch run_index_job as a task."""
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

    await progress_msg.edit_text(
        f"📥 <i>Starting index of {channel_ref}…</i>\n"
        f"<i>Bot must be admin in the channel.</i>",
        reply_markup=ui.index_stop_kb(job_id))

    async def on_progress(text: str):
        try:
            await progress_msg.edit_text(
                text, reply_markup=ui.index_stop_kb(job_id))
        except Exception:
            pass

    job = state.IndexJob(job_id=job_id, channel_ref=channel_ref,
                         progress_msg=progress_msg)
    state.job_register(job)
    # NOTE: `client` here IS the bot client — it reads channel history
    # itself via MTProto (works when the bot is admin of the channel).
    job.task = asyncio.create_task(run_index_job(
        job, client, channel_ref,
        skip=skip, from_id=from_id, to_id=to_id, limit=limit,
        on_progress=on_progress))
    log.info("started index job %d for %s", job_id, channel_ref)


def _already_indexing(channel_ref: str) -> bool:
    norm = str(normalize_channel_ref(channel_ref))
    for job in state._index_jobs.values():
        if not job.task or job.task.done():
            continue
        if (job.channel_ref == channel_ref
                or str(normalize_channel_ref(job.channel_ref)) == norm):
            return True
    return False


@admin_only
async def _index(client: Client, message: Message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    # --- /index cancel ---
    if text.lower() in ("/index cancel", "/cancel"):
        state.pending_clear(uid)
        stopped = 0
        for job_id, job in list(state._index_jobs.items()):
            if job.task and not job.task.done():
                job.cancel_event.set()
                stopped += 1
        await message.reply_text(
            f"🛑 Stopped {stopped} job(s)." if stopped else "No running jobs.")
        return

    args = parse_index_args(text)

    # --- interactive setup when no channel given ---
    if not args["channel"]:
        state.pending_set(uid, {"step": "channel"})
        await message.reply_text(SETUP_PROMPT,
                                 reply_markup=ui.ix_setup_cancel_kb())
        return

    # --- direct one-shot form: /index @channel skip=.. ... ---
    channel_ref = args["channel"]
    if _already_indexing(channel_ref):
        await message.reply_text("⚠️ Already indexing this channel.")
        return

    prog = await message.reply_text("📥 <i>Starting…</i>")
    await _start_job(client, prog, channel_ref,
                     skip=args["skip"], from_id=args["from_id"],
                     to_id=args["to_id"], limit=args["limit"])


async def _index_interactive(client: Client, message: Message):
    """Catch follow-up messages while an admin is in /index setup.

    Runs in group -1 so it sees the message before the search handler;
    raises StopPropagation only when it actually consumes the message.
    """
    uid = message.from_user.id if message.from_user else None
    if not uid or not settings.is_admin(uid):
        return
    pending = state.pending_get(uid)
    if not pending:
        return
    text = (message.text or message.caption or "").strip()
    if text.startswith("/"):
        return  # let command handlers process it

    step = pending.get("step")

    # --- step 1: waiting for the channel ---
    if step == "channel":
        ref = extract_channel_ref(message)
        if ref is None:
            await message.reply_text(
                "⚠️ I couldn't read a channel from that.\n"
                "Forward a message <b>from the channel</b>, or send its "
                "link / @username / ID.",
                reply_markup=ui.ix_setup_cancel_kb())
            raise StopPropagation
        resolving = await message.reply_text("🔍 Resolving channel…")
        try:
            chat = await resolve_channel(client, ref)
        except ValueError as exc:
            await resolving.edit_text(
                f"❌ {exc}\n\n"
                "Make sure the bot is <b>admin</b> in the channel, "
                "then send the channel again.",
                reply_markup=ui.ix_setup_cancel_kb())
            raise StopPropagation
        try:
            await resolving.delete()
        except Exception:
            pass
        pending.update({
            "step": "options",
            "chat_id": chat.id,
            "title": getattr(chat, "title", None) or str(chat.id),
            "opts": {"skip": 0, "from_id": 0, "to_id": 0, "limit": 0},
        })
        panel = await message.reply_text(
            _setup_text(pending), reply_markup=ui.ix_setup_kb(pending))
        pending["panel_msg_id"] = panel.id
        raise StopPropagation

    # --- step 2: waiting for a number for one option ---
    if step and step.startswith("opt:"):
        key = step[4:]
        digits = re.sub(r"[^\d]", "", text)
        if not digits and text != "0":
            await message.reply_text(
                "⚠️ Send a plain number (0 = off).",
                reply_markup=ui.ix_setup_cancel_kb())
            raise StopPropagation
        pending["opts"][key] = int(digits or 0)
        pending["step"] = "options"
        try:
            await client.edit_message_text(
                message.chat.id, pending["panel_msg_id"],
                _setup_text(pending),
                reply_markup=ui.ix_setup_kb(pending))
        except Exception:
            pass
        await message.reply_text(
            f"✅ {OPT_LABELS.get(key, key)} = "
            f"<b>{pending['opts'][key]:,}</b>")
        raise StopPropagation


async def _ixs(client: Client, query: CallbackQuery):
    """Setup-panel buttons: ixs:cancel | ixs:start | ixs:opt:<key>."""
    uid = query.from_user.id
    if not settings.is_admin(uid):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    data = query.data or ""
    pending = state.pending_get(uid)

    if data == "ixs:cancel":
        state.pending_clear(uid)
        await query.message.edit_text("❌ Index setup cancelled.")
        await query.answer()
        return

    if pending is None:
        await query.answer("Session expired — send /index again.",
                           show_alert=True)
        return

    if data == "ixs:start":
        opts = pending["opts"]
        chat_id = pending["chat_id"]
        if _already_indexing(str(chat_id)):
            await query.answer("⚠️ Already indexing this channel.",
                               show_alert=True)
            return
        state.pending_clear(uid)
        await query.answer("Starting…")
        try:
            await query.message.edit_reply_markup(None)
        except Exception:
            pass
        prog = await query.message.reply_text("📥 <i>Starting…</i>")
        await _start_job(client, prog, str(chat_id),
                         skip=opts["skip"], from_id=opts["from_id"],
                         to_id=opts["to_id"], limit=opts["limit"])
        return

    if data.startswith("ixs:opt:"):
        key = data.split(":")[2]
        if key not in OPT_LABELS:
            await query.answer()
            return
        pending["step"] = f"opt:{key}"
        await query.message.reply_text(
            f"✏️ Send a number for <b>{OPT_LABELS[key]}</b>\n"
            "(0 = off):")
        await query.answer()
        return

    await query.answer()


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
    # interactive setup interceptor runs first; consumes only setup msgs
    bot.on_message(filters.private, group=-1)(_index_interactive)
    bot.on_message(filters.private & filters.command("index"))(_index)
    bot.on_callback_query(filters.regex(r"^ixs:"))(_ixs)
    bot.on_message(filters.channel)(_auto_index)
