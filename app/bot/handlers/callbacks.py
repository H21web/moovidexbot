"""Callback queries: pagination, movie cards, delivery, misc."""
from __future__ import annotations

import asyncio
import logging
import math

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.errors import FloodWait, PeerIdInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app import state
from app import autodelete
from app.analytics import log_event
from app.bot import forcesub, ui
from app.bot.handlers.common import is_banned
from app.bot.handlers.groups import effective_autodelete
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
    # Go back by editing the same message (it may currently be a card).
    text = (f"🔍 <b>Results for</b> {ui.esc(data['query'])}\n"
            f"<i>{len(data['groups'])} found</i>")
    await query.message.edit_text(
        text, reply_markup=ui.results_kb(token, page, total_pages, chunk,
                                         page_start=start))


async def _send_file(client: Client, target_id: int, f, uid: int):
    """Send a File row to ``target_id`` via cached media.

    Shared by in-PM delivery, group→PM delivery, and the ``dl_``
    deep-link handler. Returns the sent message.
    """
    # send_cached_media (not send_document): send_document rejects
    # non-document file_ids ("Expected DOCUMENT, got VIDEO"), which
    # broke delivery for every video file.
    sent = await client.send_cached_media(
        target_id,
        file_id=f.file_id,
        caption=ui.file_caption({
            "file_name": f.file_name, "quality": f.quality,
            "language": f.language, "file_size": f.file_size}),
        parse_mode=ParseMode.HTML,
        reply_markup=ui.file_kb(
            f.id, watch_url(f.id, uid)),
        protect_content=settings.PROTECT_CONTENT,
    )
    asyncio.create_task(log_event("download", user_id=uid,
                                  chat_id=sent.chat.id))
    # PM deliveries fall back to the global auto-delete default
    # (no per-group row exists for a user id).
    ad = await effective_autodelete(target_id)
    if ad > 0:
        await autodelete.schedule(sent.chat.id, sent.id, ad)
    return sent


async def _get_file(file_db_id: int):
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        return (await session.execute(
            select(File).where(File.id == file_db_id))).scalar_one_or_none()


async def _deliver(client: Client, query):
    await query.answer("📤 Preparing your file…")
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    src = query.message.chat
    in_group = src.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    kb = await forcesub.ensure_joined(client, uid, chat_id=src.id)
    if kb:
        # Reuse the card message: swap its content for the join prompt.
        await query.message.edit_text(
            "📢 <b>Join our channels to download</b>", reply_markup=kb,
            parse_mode=ParseMode.HTML)
        return
    try:
        file_db_id = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        return
    f = await _get_file(file_db_id)
    if not f:
        await query.answer("❌ File not found (removed?).", show_alert=True)
        return

    # Group searches deliver to the user's PM only — never in the group.
    target = uid if in_group else src.id
    if not in_group:
        await query.message.edit_text("📤 <i>Uploading…</i>",
                                      parse_mode=ParseMode.HTML)
    try:
        await _send_file(client, target, f, uid)
    except PeerIdInvalid:
        # User never started the bot in PM — one-tap deep link that
        # delivers this exact file once they tap START.
        me = await client.get_me()
        deep = f"https://t.me/{me.username}?start=dl_{f.id}"
        await query.message.edit_text(
            "👋 <b>Almost there!</b>\n\n"
            "Tap below to open my private chat — "
            "your file will be sent there:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("▶️ Open bot & get file",
                                     url=deep)]]),
            parse_mode=ParseMode.HTML)
        return
    except FloodWait as exc:
        await query.message.edit_text(
            f"⏳ Flood control — retry in {exc.value}s.")
        return
    except Exception as exc:
        log.warning("deliver failed for file %d: %s", f.id, exc)
        await query.message.edit_text(
            "❌ Couldn't send the file. Try again later.")
        return
    if in_group:
        await query.answer("📥 File sent to your private chat ✅")
    else:
        try:
            await query.message.delete()
        except Exception:
            pass


async def _spell(client: Client, query):
    suggestion = query.data.split(":", 1)[1]
    await query.answer()
    # Re-run search with the suggestion, reusing the same message.
    from app.bot.handlers.search import _do_search
    uid = query.from_user.id
    token, groups = await _do_search(client, suggestion, uid)
    if not token:
        await query.message.edit_text("❌ Still nothing found.")
        return
    per = settings.RESULTS_PER_PAGE
    total_pages = max(1, math.ceil(len(groups) / per))
    chunk = groups[:per]
    await query.message.edit_text(
        f"🔍 <b>Results for</b> {ui.esc(suggestion)}\n"
        f"<i>{len(groups)} found</i>",
        reply_markup=ui.results_kb(token, 0, total_pages, chunk,
                                   page_start=0))


async def _fsub_retry(client: Client, query):
    uid = query.from_user.id
    kb = await forcesub.ensure_joined(client, uid,
                                      chat_id=query.message.chat.id)
    if kb:
        await query.answer("❌ You haven't joined all channels yet.",
                           show_alert=True)
    else:
        await query.answer("✅ All joined!", show_alert=True)
        try:
            await query.message.delete()
        except Exception:
            pass
        # Deep-link flow: user came from a group file button via
        # /start dl_<id> — deliver the waiting file now.
        dl_id = state.pending_dl.pop(uid, None)
        if dl_id:
            f = await _get_file(dl_id)
            if f:
                try:
                    await _send_file(client, uid, f, uid)
                except Exception as exc:
                    log.warning("retry deliver failed for file %d: %s",
                                dl_id, exc)
                    try:
                        await client.send_message(
                            uid, "❌ Couldn't send the file. "
                                 "Try again later.")
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
