"""Callback queries: pagination, movie cards, delivery, misc."""
from __future__ import annotations

import asyncio
import logging
import math

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from sqlalchemy import select

from app import state
from app.bot import forcesub, ui
from app.bot.handlers.common import is_banned
from app.config import settings
from app.db import get_session_factory
from app.models import File
from app.tmdb import get_movie
from app.web.tokens import watch_url

log = logging.getLogger(__name__)


def _page_data(token: str, page: int):
    data = state.results_get(token)
    if not data:
        return None, None, None
    groups = data["groups"]
    per = settings.RESULTS_PER_PAGE
    total_pages = max(1, math.ceil(len(groups) / per))
    page = max(0, min(page, total_pages - 1))
    return data, groups[page * per:(page + 1) * per], (page, total_pages,
                                                     page * per)


async def _pg(client: Client, query):
    token, page = query.data.split(":")[1:]
    page = int(page)
    data, chunk, meta = _page_data(token, page)
    if not data:
        await query.answer("⌛ Expired — search again.", show_alert=True)
        return
    page, total_pages, start = meta
    text = (f"🔍 <b>Results for</b> {ui.esc(data['query'])}\n"
            f"<i>{len(data['groups'])} found</i>")
    await query.message.edit_text(
        text, reply_markup=ui.results_kb(token, page, total_pages, chunk,
                                         page_start=start))
    await query.answer()


async def _movie(client: Client, query):
    _, token, gidx = query.data.split(":")
    gidx = int(gidx)
    data = state.results_get(token)
    if not data or gidx >= len(data["groups"]):
        await query.answer("⌛ Expired — search again.", show_alert=True)
        return
    group = data["groups"][gidx]
    per = settings.RESULTS_PER_PAGE
    page = gidx // per
    await query.answer()
    poster = None
    try:
        meta = await get_movie(group.get("display"), group.get("year"))
        poster = (meta or {}).get("poster_url")
    except Exception as exc:
        log.debug("tmdb failed: %s", exc)
    text = ui.movie_card(group)
    kb = ui.movie_kb(token, gidx, group, page)
    try:
        if poster:
            await query.message.edit_text("🎬 <i>Loading…</i>")
            await query.message.reply_photo(
                poster, caption=text, reply_markup=kb)
            await query.message.delete()
        else:
            await query.message.edit_text(text, reply_markup=kb)
    except Exception as exc:
        log.debug("movie card edit failed: %s", exc)


async def _back(client: Client, query):
    _, token, page = query.data.split(":")
    await query.answer()
    data, chunk, meta = _page_data(token, int(page))
    if not data:
        await query.message.edit_text("⌛ Expired — search again.")
        return
    page, total_pages, start = meta
    # If we replaced the list message with a photo, go back with a new msg.
    text = (f"🔍 <b>Results for</b> {ui.esc(data['query'])}\n"
            f"<i>{len(data['groups'])} found</i>")
    try:
        await query.message.delete()
    except Exception:
        pass
    await client.send_message(
        query.message.chat.id, text,
        reply_markup=ui.results_kb(token, page, total_pages, chunk,
                                   page_start=start))


async def _deliver(client: Client, query):
    await query.answer("📤 Preparing your file…")
    uid = query.from_user.id
    if await is_banned(uid):
        await query.message.reply_text("⛔ You are banned.")
        return
    kb = await forcesub.ensure_joined(client, uid)
    if kb:
        await query.message.reply_text(
            "📢 <b>Join our channels to download</b>", reply_markup=kb)
        return
    try:
        file_db_id = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        return
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        f = (await session.execute(
            select(File).where(File.id == file_db_id))).scalar_one_or_none()
    if not f:
        await query.message.reply_text("❌ File not found (removed?).")
        return

    status = await query.message.reply_text("📤 <i>Uploading…</i>")
    try:
        sent = await client.send_document(
            query.message.chat.id,
            document=f.file_id,
            caption=ui.file_caption({
                "file_name": f.file_name, "quality": f.quality,
                "language": f.language, "file_size": f.file_size,
                "duration": f.duration}),
            parse_mode=ParseMode.HTML,
            reply_markup=ui.file_kb(
                f.id, watch_url(f.id, uid)),
            protect_content=settings.PROTECT_CONTENT,
        )
    except FloodWait as exc:
        await status.edit_text(f"⏳ Flood control — retry in {exc.value}s.")
        return
    except Exception as exc:
        log.warning("deliver failed for file %d: %s", f.id, exc)
        await status.edit_text("❌ Couldn't send the file. Try again later.")
        return
    try:
        await status.delete()
    except Exception:
        pass
    if settings.AUTO_DELETE_SECONDS > 0:
        asyncio.create_task(_auto_delete(client, sent.chat.id, sent.id,
                                        settings.AUTO_DELETE_SECONDS))


async def _auto_delete(client: Client, chat_id: int, msg_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await client.delete_messages(chat_id, msg_id)
    except Exception:
        pass


async def _spell(client: Client, query):
    suggestion = query.data.split(":", 1)[1]
    await query.answer()
    # Re-run search with the suggestion as a fresh message flow.
    from app.bot.handlers.search import _do_search, _send_results
    uid = query.from_user.id
    token, groups = await _do_search(client, suggestion, uid)
    try:
        await query.message.delete()
    except Exception:
        pass
    if not token:
        await client.send_message(query.message.chat.id,
                                  "❌ Still nothing found.")
        return
    await _send_results(client, query.message.chat.id, token, suggestion)


async def _fsub_retry(client: Client, query):
    kb = await forcesub.ensure_joined(client, query.from_user.id)
    if kb:
        await query.answer("❌ You haven't joined all channels yet.",
                           show_alert=True)
    else:
        await query.answer("✅ All joined! Send your movie name.",
                           show_alert=True)
        try:
            await query.message.delete()
        except Exception:
            pass


async def _ixstop(client: Client, query):
    uid = query.from_user.id if query.from_user else None
    if not settings.is_admin(uid):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    try:
        job_id = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        return
    job = state.job_get(job_id)
    if not job:
        await query.answer("No active job.", show_alert=True)
        return
    job.cancel_event.set()
    await query.answer("🛑 Stopping…", show_alert=False)


def register(bot: Client) -> None:
    bot.on_callback_query(filters.regex(r"^pg:"))(_pg)
    bot.on_callback_query(filters.regex(r"^mv:"))(_movie)
    bot.on_callback_query(filters.regex(r"^bk:"))(_back)
    bot.on_callback_query(filters.regex(r"^dl:"))(_deliver)
    bot.on_callback_query(filters.regex(r"^sp:"))(_spell)
    bot.on_callback_query(filters.regex(r"^fsub_retry$"))(_fsub_retry)
    bot.on_callback_query(filters.regex(r"^ixstop:"))(_ixstop)
