"""/index command + real-time auto-index of new channel posts.

Both historical backfill (``/index``) and live auto-index run on the
SINGLE bot client over MTProto:

* bots can NOT use ``messages.getHistory`` — so history is walked with
  ``channels.GetMessages`` in ID batches (bot-allowed). The end of
  history is bootstrapped from a message the admin forwards (or a post
  link), the same trick DreamX-family bots use. The bot must be
  **admin** in the channel; no user session needed.
* new posts in channels where the bot is admin are auto-indexed live
  as they arrive.

Two ways to index history:

1. Interactive: send ``/index`` with no arguments and follow the
   buttons — forward a message from the channel, or send its link /
   @username / id, then tweak skip/limit/from/to and hit Start.
2. Direct: ``/index @channel`` — resolves the channel, then asks for
   one forwarded message / post link to bootstrap the latest message id.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone

from pyrogram import Client, StopPropagation, filters
from pyrogram.enums import ParseMode
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
    "<code>/index @channel</code> — resolve channel, then forward a\n"
    "message / post link to bootstrap the latest msg id\n"
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


# t.me/c/<cid>/<mid> and t.me/<username>/<mid> post links
POST_LINK_RE = re.compile(r"t\.me/(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)")


def extract_bootstrap(message: Message):
    """(channel ref, latest message id) from a forwarded msg or post link.

    The message id bootstraps the end of history (DreamX-style): bots
    can't list history, they can only fetch known IDs, so the admin
    supplies the newest one by forwarding any message / post link.
    Returns (ref, 0) when no message id could be read.
    """
    fwd = message.forward_from_chat
    if fwd is not None:
        return fwd.id, message.forward_from_message_id or 0
    text = (message.text or message.caption or "").strip()
    m = POST_LINK_RE.search(text)
    if m:
        mid = int(m.group(3))
        if m.group(1):  # t.me/c/<cid>/<mid>
            return int("-100" + m.group(1)), mid
        return "@" + m.group(2), mid
    return (text or None), 0


def _setup_text(pending: dict) -> str:
    opts = pending["opts"]

    def fmt(v: int, off: str = "—") -> str:
        return f"{v:,}" if v else off

    latest = pending.get("last_msg_id") or 0
    latest_line = (f"📍 Latest msg: <b>{latest:,}</b>\n\n"
                   if latest else "")
    return (
        f"📁 <b>{pending['title']}</b>\n"
        f"<code>{pending['chat_id']}</code>\n\n"
        f"{latest_line}"
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
                     limit: int = 0, last_msg_id: int = 0) -> None:
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
    # NOTE: `client` here IS the bot client — it fetches channel
    # messages by ID via channels.GetMessages (bot-allowed). The bot must
    # be admin in the channel. `last_msg_id` bootstraps the end of history.
    job.task = asyncio.create_task(run_index_job(
        job, client, channel_ref,
        skip=skip, from_id=from_id, to_id=to_id, limit=limit,
        last_msg_id=last_msg_id, on_progress=on_progress))
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
        await state.pending_clear(uid)
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
        await state.pending_set(uid, {"step": "channel"})
        await message.reply_text(SETUP_PROMPT,
                                 reply_markup=ui.ix_setup_cancel_kb())
        return

    # --- direct form: /index @channel ... -> resolve, then bootstrap ---
    channel_ref = args["channel"]
    if _already_indexing(channel_ref):
        await message.reply_text("⚠️ Already indexing this channel.")
        return
    resolving = await message.reply_text("🔍 Resolving channel…")
    try:
        chat = await resolve_channel(client, channel_ref)
    except ValueError as exc:
        await resolving.edit_text(f"❌ {exc}")
        return
    try:
        await resolving.delete()
    except Exception:
        pass
    title = getattr(chat, "title", None) or str(chat.id)
    await state.pending_set(uid, {
        "step": "bootstrap",
        "chat_id": chat.id,
        "title": title,
        "opts": {"skip": args["skip"], "from_id": args["from_id"],
                 "to_id": args["to_id"], "limit": args["limit"]},
    })
    await message.reply_text(
        f"📁 <b>{title}</b> resolved.\n\n"
        "📍 One more thing: forward <b>any one message</b> from that "
        "channel here (or send a post link like "
        "<code>t.me/c/123/456</code>) — I need the latest message id "
        "to know where history ends.",
        reply_markup=ui.ix_setup_cancel_kb())


async def _index_interactive(client: Client, message: Message):
    """Catch follow-up messages while an admin is in /index setup.

    Runs in group -1 so it sees the message before the search handler;
    raises StopPropagation only when it actually consumes the message.
    """
    uid = message.from_user.id if message.from_user else None
    if not uid or not settings.is_admin(uid):
        return
    pending = await state.pending_get(uid)
    if not pending:
        # Diagnostic: the classic symptom of a lost session is a number
        # falling through to search — log it loudly.
        if text and re.fullmatch(r"\d+", text):
            log.warning(
                "index: number %r from admin %s but NO pending session "
                "(is migration 0004 applied? is only ONE bot instance "
                "running?)", text, uid)
        return
    text = (message.text or message.caption or "").strip()
    if text.startswith("/"):
        return  # let command handlers process it

    step = pending.get("step")

    # --- step 1: waiting for the channel ---
    if step == "channel":
        ref, last_msg_id = extract_bootstrap(message)
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
        title = getattr(chat, "title", None) or str(chat.id)
        pending.update({
            "chat_id": chat.id,
            "title": title,
            "opts": {"skip": 0, "from_id": 0, "to_id": 0, "limit": 0},
        })
        if not last_msg_id:
            # Bare @username/ID: channel known, but we still need the
            # latest message id — ask for one forwarded message / post link.
            pending["step"] = "bootstrap"
            await message.reply_text(
                f"📁 <b>{title}</b> resolved.\n\n"
                "📍 Now forward <b>any one message</b> from that "
                "channel here (or send a post link like "
                "<code>t.me/c/123/456</code>) — I need the latest message "
                "id to know where history ends.",
                reply_markup=ui.ix_setup_cancel_kb())
            raise StopPropagation
        pending.update({"step": "options", "last_msg_id": last_msg_id})
        panel = await message.reply_text(
            _setup_text(pending), reply_markup=ui.ix_setup_kb(pending))
        pending["panel_msg_id"] = panel.id
        await state.pending_set(uid, pending)
        raise StopPropagation

    # --- step 1b: waiting for a forwarded message / post link ---
    if step == "bootstrap":
        _ref, last_msg_id = extract_bootstrap(message)
        if not last_msg_id:
            await message.reply_text(
                "⚠️ That had no message id. Forward <b>any one "
                "message</b> from the channel, or send a post link "
                "(<code>t.me/.../&lt;msg_id&gt;</code>).",
                reply_markup=ui.ix_setup_cancel_kb())
            raise StopPropagation
        pending["last_msg_id"] = last_msg_id
        pending["step"] = "options"
        panel = await message.reply_text(
            _setup_text(pending), reply_markup=ui.ix_setup_kb(pending))
        pending["panel_msg_id"] = panel.id
        await state.pending_set(uid, pending)
        raise StopPropagation


    # --- step 2: waiting for a number for one option ---
    if step and step.startswith("opt:"):
        key = step[4:]
        digits = re.sub(r"[^\d]", "", text)
        if not digits and text != "0":
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await client.edit_message_text(
                    message.chat.id, pending["panel_msg_id"],
                    f"⚠️ Send a <b>plain number</b> for "
                    f"<b>{OPT_LABELS.get(key, key)}</b> (0 = off).",
                    reply_markup=ui.ix_setup_cancel_kb(),
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass
            raise StopPropagation
        pending["opts"][key] = int(digits or 0)
        pending["step"] = "options"
        log.info("index setup: admin %s set %s=%s", uid, key,
                 pending["opts"][key])
        try:
            await message.delete()  # keep the chat clean: panel shows value
        except Exception:
            pass
        try:
            await client.edit_message_text(
                message.chat.id, pending["panel_msg_id"],
                _setup_text(pending),
                reply_markup=ui.ix_setup_kb(pending))
        except Exception:
            pass
        await state.pending_set(uid, pending)
        raise StopPropagation


async def _ixs(client: Client, query: CallbackQuery):
    """Setup-panel buttons: ixs:cancel | ixs:start | ixs:opt:<key>."""
    uid = query.from_user.id
    if not settings.is_admin(uid):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    data = query.data or ""
    pending = await state.pending_get(uid)

    if data == "ixs:cancel":
        await state.pending_clear(uid)
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
        last_msg_id = pending.get("last_msg_id", 0)
        if _already_indexing(str(chat_id)):
            await query.answer("⚠️ Already indexing this channel.",
                               show_alert=True)
            return
        await state.pending_clear(uid)
        await query.answer("Starting…")
        # Reuse the panel message for progress — one message, edited.
        prog = query.message
        try:
            await prog.edit_text("📥 <i>Starting…</i>")
        except Exception:
            prog = await query.message.reply_text("📥 <i>Starting…</i>")
        await _start_job(client, prog, str(chat_id),
                         skip=opts["skip"], from_id=opts["from_id"],
                         to_id=opts["to_id"], limit=opts["limit"],
                         last_msg_id=last_msg_id)
        return

    if data.startswith("ixs:opt:"):
        key = data.split(":")[2]
        if key not in OPT_LABELS:
            await query.answer()
            return
        pending["step"] = f"opt:{key}"
        await state.pending_set(uid, pending)
        log.info("index setup: admin %s chose option %s", uid, key)
        # Turn the panel itself into the prompt — no extra message.
        await query.message.edit_text(
            f"✏️ Send a number for <b>{OPT_LABELS[key]}</b>\n"
            "(0 = off):",
            reply_markup=ui.ix_setup_cancel_kb(),
            parse_mode=ParseMode.HTML)
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
            stmt = pg_insert(File).values(rec).on_conflict_do_nothing()
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
