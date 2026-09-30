"""Text search (private + groups) and inline mode."""
from __future__ import annotations

import logging
import math

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)

from app import state
from app.bot import forcesub, ui
from app.bot.handlers.common import is_banned, track_user
from app.config import settings
from app.db import get_session_factory
from app.search import group_by_title, search_files
from app.spell import suggest
from app.tmdb import get_movie

log = logging.getLogger(__name__)


async def _do_search(client: Client, query_text: str,
                     user_id: int) -> tuple[str, object] | tuple[None, None]:
    """Run search, return (token, first_page_text/kb) or (None, None)."""
    items, _parsed = await search_files(query_text, user_id=user_id)
    groups = group_by_title(items)
    if not groups:
        return None, None
    token = state.results_put(groups, query_text, user_id)
    return token, groups


async def _send_results(client: Client, chat_id: int, token: str,
                        query_text: str, page: int = 0):
    data = state.results_get(token)
    if not data:
        await client.send_message(chat_id, "⌛ Results expired — search again.")
        return
    groups = data["groups"]
    per = settings.RESULTS_PER_PAGE
    total_pages = max(1, math.ceil(len(groups) / per))
    page = max(0, min(page, total_pages - 1))
    chunk = groups[page * per:(page + 1) * per]
    text = (f"🔍 <b>Results for</b> {ui.esc(query_text)}\n"
            f"<i>{len(groups)} found</i>")
    await client.send_message(
        chat_id, text, reply_markup=ui.results_kb(
            token, page, total_pages, chunk, page_start=page * per))


async def _on_text(client: Client, message: Message):
    if not message.text or message.text.startswith("/"):
        return
    user = await track_user(message)
    if user and user.is_banned:
        return
    uid = message.from_user.id
    kb = await forcesub.ensure_joined(client, uid)
    if kb:
        await message.reply_text(
            "📢 <b>Join our channels to use the bot</b>",
            reply_markup=kb)
        return
    q = message.text.strip()
    if len(q) < 2:
        return
    wait = await message.reply_text("🔍 <i>Searching…</i>")
    try:
        token, groups = await _do_search(client, q, uid)
        await wait.delete()
        if not token:
            factory = get_session_factory(settings.DATABASE_URL)
            async with factory() as s:
                suggestions = await suggest(s, q)
            if suggestions:
                await message.reply_text(
                    "❌ <b>No results found.</b>\nDid you mean:",
                    reply_markup=ui.spell_kb(suggestions))
            else:
                await message.reply_text(
                    "❌ <b>No results found.</b>\n"
                    + ("🎞 Try /request to ask for it!"
                       if settings.REQUEST_CHANNEL else
                       "Try a different spelling."))
            return
        await _send_results(client, message.chat.id, token, q)
    except Exception as exc:
        log.exception("search failed")
        try:
            await wait.edit_text("⚠️ Search failed, try again.")
        except Exception:
            pass


async def _on_inline(client: Client, inline_query):
    q = (inline_query.query or "").strip()
    if len(q) < 2:
        await inline_query.answer([], is_personal=True)
        return
    uid = inline_query.from_user.id if inline_query.from_user else None
    if uid and await is_banned(uid):
        await inline_query.answer([], is_personal=True)
        return
    items, _ = await search_files(q, user_id=uid, log_query=False)
    groups = group_by_title(items)[:20]
    results = []
    for g in groups:
        files = g.get("files", [])
        best = files[0] if files else {}
        title = g.get("display") or "Unknown"
        year = g.get("year")
        label = f"{title}" + (f" ({year})" if year else "")
        quals = ", ".join(sorted({f.get("quality") or "?" for f in files}))
        poster = None
        try:
            meta = await get_movie(title, year)
            poster = (meta or {}).get("poster_url")
        except Exception:
            pass
        text = ui.movie_card(g)
        kb_rows = []
        for f in files[:6]:
            kb_rows.append([InlineKeyboardButton(
                f"📥 {f.get('quality') or '?'} · {ui.fmt_size(f.get('file_size'))}",
                callback_data=f"dl:{f['id']}")])
        results.append(InlineQueryResultArticle(
            title=label[:60],
            description=f"{quals} · {len(files)} file(s)"[:80],
            input_message_content=InputTextMessageContent(text),
            thumb_url=poster,
            reply_markup=InlineKeyboardMarkup(kb_rows) if kb_rows else None,
        ))
    await inline_query.answer(results, is_personal=True, cache_time=60)


def register(bot: Client) -> None:
    # groups + private, but not channels and not commands
    bot.on_message(
        filters.text & ~filters.command(["start", "help", "trending",
                                         "request", "index", "stats",
                                         "broadcast", "ban", "unban",
                                         "users", "settings", "requests",
                                         "cancel"])
        & ~filters.channel
    )(_on_text)
    bot.on_inline_query()(_on_inline)
