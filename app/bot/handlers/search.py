"""Text search (private + groups) — v9: smart search + AI intent router + AI assist."""
from __future__ import annotations

import asyncio
import logging
import math

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from app import ai, personalize, state
from app import ai_assist, intent as intent_mod, search_v9
from app import autodelete
from app.analytics import log_event
from app.bot import forcesub, ui, v8_ui
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


_bot_username: str | None = None


async def _get_bot_username(client: Client) -> str | None:
    """Cache the bot's username for file deep links (tap name -> deliver)."""
    global _bot_username
    if _bot_username:
        return _bot_username
    try:
        me = await client.get_me()
        _bot_username = me.username or None
    except Exception:
        log.debug("get_me failed", exc_info=True)
    return _bot_username


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


async def _no_results_pm(client: Client, message: Message, q: str,
                        suggestions: list[str] | None = None):
    """No-results flow for PM: TMDB correction -> spell suggestions.

    v9: the AI recovery chain (title correction -> retry) already ran
    inside _v9_search_flow. ``suggestions`` lets the caller pass
    pre-computed spell suggestions so they aren't looked up twice.
    """
    kb = None
    text = "❌ <b>No results found.</b>"
    if suggestions is None:
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
            await message.reply_text(text, reply_markup=kb)
            return
        try:
            factory = get_session_factory(settings.DATABASE_URL)
            async with factory() as s:
                suggestions = await suggest(s, q)
        except Exception:  # noqa: BLE001
            suggestions = []
    if suggestions:
        text += "\nDid you mean:"
        kb = ui.spell_kb(suggestions)
    else:
        text += ("\n🎞 Try /request to ask for it!"
                 if settings.REQUEST_CHANNEL
                 else "\nTry a different spelling.")
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


async def render_v8_results(client: Client, message: Message,
                        token: str, uid: int, page: int = 0) -> None:
    """Render (or re-render) a v8 results message: best pick + list."""
    data = state.v8_get(token)
    if not data or data.get("user_id") != uid:
        try:
            await message.edit_text("⌛ Results expired — search again.")
        except Exception:
            pass
        return
    files = v8_ui.apply_v8_filters(data["files"], data.get("filters") or {})
    best = data["best"]
    rest = [f for f in files if f.get("id") != best.get("id")]
    pages = max(1, math.ceil(len(rest) / v8_ui.V8_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = rest[page * v8_ui.V8_PAGE_SIZE:(page + 1) * v8_ui.V8_PAGE_SIZE]
    username = await _get_bot_username(client)
    text = v8_ui.v8_results_text(
        data.get("meta"), best, chunk, page, pages, len(rest),
        data.get("filters") or {}, uid, data.get("ai_note"), username)
    kb = v8_ui.v8_results_kb(token, best["id"], uid, page, pages,
                             data.get("filters") or {})
    try:
        await message.edit_text(text, reply_markup=kb,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True)
    except Exception:
        log.debug("v8 render edit failed", exc_info=True)


async def _judge_uncertain(client: Client, message: Message, uid: int,
                          q: str, res: dict,
                          waiter) -> tuple[dict, bool]:
    """Run the AI judge on an 'uncertain' result set.

    Returns ``(res, replied)`` — ``replied`` is True when the bot already
    answered (nothing genuinely matched), so the caller stops.
    """
    if res.get("status") != "uncertain" or not res.get("files"):
        return res, False
    judged = await ai_assist.assist_uncertain(uid, q, res["files"])
    action = judged.get("action")
    if action == "filtered" and judged.get("files"):
        res = dict(res)
        res["files"] = judged["files"]
        res["best"] = judged["files"][0]
        res["status"] = "ok"
        return res, False
    if action == "none_match":
        try:
            await waiter.delete()
        except Exception:
            pass
        note = ui.esc(judged.get("note") or "")
        await waiter._client.send_message(
            waiter.chat.id,
            "\u274c <b>No results found.</b>\n"
            f"\U0001F916 <i>{note}</i>"
            + ("\n\U0001F39E Use /request to ask for it!"
               if settings.REQUEST_CHANNEL else ""),
        )
        return res, True
    return res, False  # as_is -> show the weak hits as-is


async def _v9_search_flow(client: Client, message: Message,
                         uid: int, q: str) -> bool:
    """v9 PM search: smart search + AI assist on uncertain/no results.

    Always handles the message (True) except on unexpected failure.
    AI assists exactly where search is unsure: judging weak candidate
    sets, and the no-results recovery chain (title correction -> retry
    -> spell suggestions). Works with AI off — the flow degrades to
    plain keyword search.
    """
    wait = await message.reply_text("\U0001F50D <i>Searching…</i>")
    try:
        res = await search_v9.smart_search(uid, q)
    except Exception:  # noqa: BLE001
        log.exception("v9 search failed")
        try:
            await wait.delete()
        except Exception:
            pass
        return False

    res, replied = await _judge_uncertain(client, message, uid, q,
                                          res, wait)
    if replied:
        return True

    suggestions: list[str] | None = None
    if res.get("status") != "ok" or not res.get("best"):
        # AI recovery chain: correct the title -> retry once.
        fix = await ai_assist.assist_no_results(uid, q)
        if fix.get("action") == "retry":
            try:
                res2 = await search_v9.smart_search(uid, fix["query"])
            except Exception:  # noqa: BLE001
                log.debug("v9 retry search failed", exc_info=True)
                res2 = {"status": "no_results"}
            if res2.get("best"):
                res, q = res2, fix["query"]
                res, replied = await _judge_uncertain(
                    client, message, uid, q, res, wait)
                if replied:
                    return True
        elif fix.get("action") == "suggest":
            suggestions = fix.get("suggestions")
        if res.get("status") not in ("ok", "uncertain") or not res.get("best"):
            try:
                await wait.delete()
            except Exception:
                pass
            await _no_results_pm(client, message, q,
                                 suggestions=suggestions)
            return True

    asyncio.create_task(ai.remember(uid, "user", q))
    from app import enrich as enrich_mod
    meta = await enrich_mod.enrich_title(res["title"] or q,
                                         res["parsed"].get("year"), uid)
    ai_note = await ai_assist.ai_verdict(uid, res["best"],
                                         res["title"] or q)
    token = state.v8_put({
        "files": res["files"],
        "best": res["best"],
        "meta": meta,
        "query": q,
        "user_id": uid,
        "filters": {},
        "filter_opts": v8_ui.v8_filter_options(res["files"]),
        "ai_note": ai_note,
    })
    await render_v8_results(client, wait, token, uid, page=0)
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
    # v9: AI intent router — no more what/when prefix matching. AI works
    # whether or not there are results; every message gets a real route.
    if pm:
        intent = await intent_mod.classify(uid, q)
        log.info("v9 intent %r -> %s", q[:60], intent)
        if intent in ("movie_search", "other"):
            if await _v9_search_flow(client, message, uid, q):
                return
        elif intent == "question":
            # ai_chat keeps the memory itself — no separate remember here.
            if ai.is_configured():
                await _ai_chat_reply(client, message, uid, q)
            else:
                await message.reply_text(
                    "\U0001F916 AI is off right now — "
                    "send me a movie name to search \U0001F50D")
            return
        elif intent == "greeting":
            await message.reply_text(
                "\U0001F44B Hey! Send me a movie or series name \U0001F50D")
            return
        elif intent == "request":
            await message.reply_text(
                "\U0001F39E To request a movie, use /request &lt;movie name&gt;")
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
