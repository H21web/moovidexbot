"""Text search (private + groups) — v6: personalized + AI-assisted."""
from __future__ import annotations

import asyncio
import logging
import math

from pyrogram import Client, filters
from pyrogram.types import Message

from app import ai, ai_search, personalize, state
from app import autodelete
from app.analytics import log_event
from app.bot import forcesub, ui
from app.bot.handlers.common import track_user
from app.bot.handlers.groups import effective_autodelete
from app.config import settings
from app.db import get_session_factory
from app.search import group_by_title, search_files
from app.spell import suggest
from app.tmdb import resolve_title
from app.web.tokens import watch_url

log = logging.getLogger(__name__)


def _is_pm(chat_id: int) -> bool:
    return not str(chat_id).startswith("-")


async def _do_search(client: Client, query_text: str, user_id: int,
                     personal: bool = True
                     ) -> tuple[str, object] | tuple[None, None]:
    """Run search (+ personalization), return (token, groups) or (None, None)."""
    items, _parsed = await search_files(query_text, user_id=user_id)
    if personal:
        items = await personalize.rerank(items, user_id)
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
    sent = await client.send_message(
        chat_id, text, reply_markup=ui.results_kb(
            token, page, total_pages, chunk, page_start=page * per))
    # In groups, auto-delete the results message after the group's timer.
    if str(chat_id).startswith("-"):
        ad = await effective_autodelete(int(chat_id))
        if ad > 0:
            await autodelete.schedule(int(chat_id), sent.id, ad)


async def _no_results_pm(client: Client, message: Message, q: str):
    """No-results flow for PM: TMDB correction -> spell suggestions ->
    AI button. Everything in the single clean template."""
    kb = None
    text = "❌ <b>No results found.</b>"
    # Never send raw user text to TMDB: resolve a clean title first
    # (local extraction -> TMDB -> web-search fallback).
    tm = await resolve_title(q)
    if tm and tm.get("title"):
        year = f" ({tm['year']})" if tm.get("year") else ""
        text = (
            f"🤔 Did you mean <b>{ui.esc(tm['title'])}</b>{year}?\n"
            "📭 That file is not in the database."
            + ("\n🎞 Use /request to ask for it!"
               if settings.REQUEST_CHANNEL else "")
        )
    else:
        try:
            factory = get_session_factory(settings.DATABASE_URL)
            async with factory() as s:
                suggestions = await suggest(s, q)
        except Exception:  # noqa: BLE001
            suggestions = []
        if suggestions:
            text += "\nDid you mean:"
            kb = ui.spell_kb(suggestions)
    # On-demand AI search button (only when Groq is configured; the query
    # lives server-side because callback data is limited to 64 bytes).
    if ai.is_configured():
        ai_token = ai.store_query(q)
        ai_kb = ui.ai_search_kb(ai_token)
        if kb is not None:
            kb.inline_keyboard.extend(ai_kb.inline_keyboard)
        else:
            kb = ai_kb
    await message.reply_text(text, reply_markup=kb)


async def _ai_chat_reply(client: Client, message: Message, uid: int, q: str):
    """Route a question-like PM message to Groq: live web answer first,
    entertainment chat as fallback."""
    wait = await message.reply_text("🤖 <i>thinking…</i>")
    try:
        reply, status = await ai.ai_web_answer(uid, q)
        if status == "ok":
            await wait.edit_text(reply)
            return
        if status == "no_quota":
            await wait.edit_text(
                "🤖 Daily AI limit reached — try again tomorrow 🌙")
            return
        # no_results / failed / ai_off -> fall through to normal chat
        reply, status = await ai.ai_chat(uid, q)
        if status in ("ok", "cached"):
            await wait.edit_text(reply)
        elif status == "no_quota":
            await wait.edit_text(
                "🤖 Daily AI limit reached — try again tomorrow 🌙")
        else:
            await wait.edit_text(
                "🤖 AI is unavailable right now — try searching instead 🔍")
    except Exception:  # noqa: BLE001
        log.exception("ai chat failed")
        try:
            await wait.edit_text("🤖 Something went wrong — try again.")
        except Exception:
            pass


async def _ai_search_flow(client: Client, message: Message,
                        uid: int, q: str) -> bool:
    """v7 AI search: understand -> find -> recommend. True = handled.

    - play intent + best file  -> "Playing" card with Play/Download
    - search intent + files    -> AI best-pick card, then classic results
    - chat intent              -> AI chat
    - no results / failure     -> False (classic flow takes over)
    """
    wait = await message.reply_text("🤖 <i>understanding…</i>")
    try:
        res = await ai_search.ai_search(uid, q)
    except Exception:  # noqa: BLE001
        log.exception("ai search failed")
        try:
            await wait.delete()
        except Exception:
            pass
        return False

    status, intent = res["status"], res["intent"]
    if status == "no_quota":
        await wait.edit_text("🤖 Daily AI limit reached — try again tomorrow 🌙")
        return True
    if intent == "chat":
        await wait.delete()
        await _ai_chat_reply(client, message, uid, q)
        return True
    if status != "ok" or not res.get("best"):
        await wait.delete()
        return False  # classic flow: _no_results_pm

    best = res["best"]
    title = res["title"]
    asyncio.create_task(ai.remember(uid, "user", q))
    url = watch_url(best["id"], uid)
    kb = ui.play_kb(best["id"], url)
    meta = " · ".join(x for x in (
        best.get("quality"), best.get("language"),
        ai_search.fmt_size(best.get("file_size"))) if x)
    fname = ui.esc((best.get("file_name") or title)[:70])

    if intent == "play":
        text = f"▶ <b>Playing:</b> {fname}"
        if meta:
            text += f"\n<i>{ui.esc(meta)}</i>"
        await wait.edit_text(text, reply_markup=kb)
        return True

    # search intent: best-pick card, then the full classic results below.
    n = len(res["files"])
    text = (f"🤖 <b>AI found {n} result{'s' if n != 1 else ''} for</b> "
            f"{ui.esc(title[:60])}\n⭐ <b>Best pick:</b> {fname}")
    if meta:
        text += f"\n<i>{ui.esc(meta)}</i>"
    await wait.edit_text(text, reply_markup=kb)
    try:
        token, groups = await _do_search(client, q, uid, personal=True)
        if token:
            await _send_results(client, message.chat.id, token, q, 0)
    except Exception:  # noqa: BLE001
        log.exception("classic results after ai card failed")
    return True


async def _on_text(client: Client, message: Message):
    if not message.text or message.text.startswith("/"):
        return
    user = await track_user(message)
    if user and user.is_banned:
        return
    uid = message.from_user.id
    kb = await forcesub.ensure_joined(client, uid, chat_id=message.chat.id)
    if kb:
        await message.reply_text(
            "📢 <b>Join our channels to use the bot</b>",
            reply_markup=kb)
        return
    q = message.text.strip()
    if len(q) < 2:
        return
    pm = _is_pm(message.chat.id)
    asyncio.create_task(log_event("search", user_id=uid,
                                  chat_id=message.chat.id))
    # v7: AI search in PM — understand intent, find files, recommend best.
    # (Chat-intent messages keep the classic _ai_chat_reply path.)
    if pm and ai.is_configured() and ai.detect_intent(q) != "chat":
        if await _ai_search_flow(client, message, uid, q):
            return
    # v6: question-like messages in PM go to the AI chat (ai_chat keeps
    # the memory itself — no separate remember here).
    if pm and ai.is_configured() and ai.detect_intent(q) == "chat":
        await _ai_chat_reply(client, message, uid, q)
        return
    if pm:
        asyncio.create_task(ai.remember(uid, "user", q))
    wait = await message.reply_text("🔍 <i>Searching…</i>")
    try:
        token, groups = await _do_search(client, q, uid, personal=pm)
        await wait.delete()
        if not token:
            if pm:
                await _no_results_pm(client, message, q)
            else:
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


def register(bot: Client) -> None:
    # groups + private, but not channels and not commands
    bot.on_message(
        filters.text & ~filters.command(["start", "help", "trending",
                                         "request", "index", "stats",
                                         "broadcast", "ban", "unban", "warn",
                                         "users", "settings", "requests",
                                         "connect", "groups", "cancel"])
        & ~filters.channel
    )(_on_text)
