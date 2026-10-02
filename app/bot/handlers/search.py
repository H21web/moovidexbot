"""Text search (private + groups) — v9: smart search + AI intent router + AI assist."""
from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app import ai, personalize, state
from app import ai_assist, intent as intent_mod, search_v9
from app import autodelete
from app.analytics import log_event
from app.bot import forcesub, ui, v8_ui
from app.bot.handlers.common import track_user
from app.bot.handlers.groups import effective_autodelete
from app.config import settings
from app.db import get_session_factory
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


def _stash_ai_query(q: str) -> str:
    """Store a query for the 🤖 AI Search button.

    Consumed by ``ai.take_query`` in the ``aiq:`` callback. The 64-byte
    callback-data limit is why the full text lives server-side keyed by
    token (ai.py keeps the store + TTL; this is its producer).
    """
    token = secrets.token_hex(8)
    ai._ai_queries[token] = (time.time(), q)
    return token


def _no_results_kb(ai_token: str,
                   base: InlineKeyboardMarkup | None = None
                   ) -> InlineKeyboardMarkup:
    """No-results keyboard: keep ``base`` rows, add AI Search (+ Request)."""
    rows = [list(r) for r in (base.inline_keyboard if base else [])]
    ai_row = [InlineKeyboardButton("🤖 AI Search",
                                   callback_data=f"aiq:{ai_token}")]
    if settings.REQUEST_CHANNEL:
        ai_row.append(InlineKeyboardButton("🎞 Request",
                                           callback_data="request"))
    rows.append(ai_row)
    return InlineKeyboardMarkup(rows)


async def _no_results_pm(client: Client, message: Message, q: str,
                        suggestions: list[str] | None = None):
    """No-results flow for PM: TMDB correction -> spell suggestions.

    v9: the AI recovery chain (title correction -> retry) already ran
    inside _v9_search_flow. ``suggestions`` lets the caller pass
    pre-computed spell suggestions so they aren't looked up twice.
    The 🤖 AI Search button (promised in /help) is always attached.
    """
    ai_token = _stash_ai_query(q)
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
            await message.reply_text(
                text, reply_markup=_no_results_kb(ai_token),
                parse_mode=ParseMode.HTML)
            return
        try:
            factory = get_session_factory(settings.DATABASE_URL)
            async with factory() as s:
                suggestions = await suggest(s, q)
        except Exception:  # noqa: BLE001
            suggestions = []
    if suggestions:
        text += "\nDid you mean:"
        kb = _no_results_kb(ai_token, ui.spell_kb(suggestions))
    else:
        text += ("\n🎞 Try /request to ask for it!"
                 if settings.REQUEST_CHANNEL
                 else "\nTry a different spelling.")
        kb = _no_results_kb(ai_token)
    await message.reply_text(text, reply_markup=kb,
                             parse_mode=ParseMode.HTML)


async def _ai_chat_reply(client: Client, message: Message, uid: int, q: str):
    """Route a question-like PM message to Groq: live web answer first,
    entertainment chat as fallback."""
    wait = await message.reply_text("🤖 <i>thinking…</i>",
                                    parse_mode=ParseMode.HTML)
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


async def _build_v8(client: Client, token: str,
                  uid: int, page: int = 0) -> tuple[str, object] | tuple[None, None]:
    """Build the (text, keyboard) for a v8 results session.

    Shared by the PM edit-render, the group send-render, and background
    enrich re-renders. Returns ``(None, None)`` when the session is gone.
    """
    data = state.v8_get(token)
    if not data:
        return None, None
    if not data.get("group") and data.get("user_id") != uid:
        return None, None
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
    if data.get("corrected"):
        text = (f"🔤 Showing results for "
                f"<b>{ui.esc(data['corrected'])}</b>\n\n" + text)
    kb = v8_ui.v8_results_kb(token, best["id"], uid, page, pages,
                             data.get("filters") or {})
    data["page"] = page
    return text, kb


async def render_v8_results(client: Client, message: Message,
                        token: str, uid: int, page: int = 0) -> None:
    """Render (or re-render) a v8 results message: best pick + list."""
    data = state.v8_get(token)
    if not data or (not data.get("group") and data.get("user_id") != uid):
        try:
            await message.edit_text("⌛ Results expired — search again.")
        except Exception:
            pass
        return
    built = await _build_v8(client, token, uid, page)
    if not built[0]:
        return
    text, kb = built
    try:
        await message.edit_text(text, reply_markup=kb,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True)
    except Exception:
        log.debug("v8 render edit failed", exc_info=True)


async def send_v8_results(client: Client, chat_id: int, token: str,
                         uid: int):
    """Send a v8 results card as a NEW message (groups, similar-search).

    Returns the sent message or None.
    """
    built = await _build_v8(client, token, uid, 0)
    if not built[0]:
        return None
    text, kb = built
    try:
        return await client.send_message(
            chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True)
    except Exception:
        log.debug("v8 send failed", exc_info=True)
        return None


async def _v9_search_flow(client: Client, message: Message,
                         uid: int, q: str) -> bool:
    """v10.2 PM search: fast hot path, smart recovery, instant render.

    Always handles the message (True) except on unexpected failure.
    Flow: keyword intent -> smart_search (hot sweeps -> spell fix ->
    web title -> AI title) -> results render IMMEDIATELY -> enrich
    (poster/info) fills in via a background edit.
    """
    wait = await message.reply_text("\U0001F50D <i>Searching…</i>",
                                    parse_mode=ParseMode.HTML)
    try:
        res = await search_v9.smart_search(uid, q)
    except Exception:  # noqa: BLE001
        log.exception("v10 search failed")
        try:
            await wait.delete()
        except Exception:
            pass
        return False

    # v10: smart_search already ran the full recovery chain (spell ->
    # web title -> AI title). The assist chain below is the last resort
    # for suggestions only.
    suggestions: list[str] | None = None
    if res.get("status") not in ("ok", "uncertain") or not res.get("best"):
        fix = await ai_assist.assist_no_results(uid, q)
        if fix.get("action") == "retry":
            try:
                res2 = await search_v9.smart_search(uid, fix["query"])
            except Exception:  # noqa: BLE001
                log.debug("v10 retry search failed", exc_info=True)
                res2 = {"status": "no_results"}
            if res2.get("best"):
                res, q = res2, fix["query"]
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
    ai_note = ai_assist.verdict_line(res["best"], res["title"] or q)
    best = res["best"]
    best["_pick_reasons"] = res.get("best_reasons") or []
    token = state.v8_put({
        "files": res["files"],
        "best": best,
        "meta": None,  # filled by _fill_meta below
        "query": q,
        "user_id": uid,
        "filters": {},
        "filter_opts": v8_ui.v8_filter_options(res["files"]),
        "ai_note": ai_note,
        "corrected": res.get("corrected"),
    })
    await render_v8_results(client, wait, token, uid, page=0)
    asyncio.create_task(_fill_meta(client, wait, token, uid,
                                   res["title"] or q,
                                   res["parsed"].get("year")))
    return True


async def _search_and_send(client: Client, chat_id: int, uid: int,
                           q: str) -> bool:
    """Run a full search and send the v8 card as a new message.

    Shared by the group search flow and the 🍿 similar-movies callback.
    Returns True when results were sent.
    """
    try:
        res = await search_v9.smart_search(uid, q)
    except Exception:  # noqa: BLE001
        log.exception("v10 search_and_send failed")
        return False
    if res.get("status") not in ("ok", "uncertain") or not res.get("best"):
        return False
    ai_note = ai_assist.verdict_line(res["best"], res["title"] or q)
    best = res["best"]
    best["_pick_reasons"] = res.get("best_reasons") or []
    token = state.v8_put({
        "files": res["files"],
        "best": best,
        "meta": None,
        "query": q,
        "user_id": uid,
        "filters": {},
        "filter_opts": v8_ui.v8_filter_options(res["files"]),
        "ai_note": ai_note,
        "corrected": res.get("corrected"),
        "group": str(chat_id).startswith("-"),
    })
    sent = await send_v8_results(client, chat_id, token, uid)
    if not sent:
        return False
    asyncio.create_task(_fill_meta(client, sent, token, uid,
                                   res["title"] or q,
                                   res["parsed"].get("year")))
    return True


async def _v9_search_flow_group(client: Client, message: Message,
                                uid: int, q: str) -> None:
    """v10.2 group search: the SAME v8 card model as PM (unified UI).

    Best pick + Play/Download/Save buttons + pagination + filters —
    no more old button model in groups. The results message follows the
    group's auto-delete timer.
    """
    wait = await message.reply_text("🔍 <i>Searching…</i>",
                                    parse_mode=ParseMode.HTML)
    try:
        res = await search_v9.smart_search(uid, q)
    except Exception:  # noqa: BLE001
        log.exception("v10 group search failed")
        try:
            await wait.edit_text("⚠️ Search failed, try again.")
        except Exception:
            pass
        return
    if res.get("status") not in ("ok", "uncertain") or not res.get("best"):
        try:
            await wait.delete()
        except Exception:
            pass
        factory = get_session_factory(settings.DATABASE_URL)
        try:
            async with factory() as s:
                suggestions = await suggest(s, q)
        except Exception:  # noqa: BLE001
            suggestions = []
        if suggestions:
            await message.reply_text(
                "❌ <b>No results found.</b>\nDid you mean:",
                reply_markup=ui.spell_kb(suggestions),
                parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(
                "❌ <b>No results found.</b>\n"
                + ("🎞 Try /request to ask for it!"
                   if settings.REQUEST_CHANNEL
                   else "Try a different spelling."),
                parse_mode=ParseMode.HTML)
        return
    ai_note = ai_assist.verdict_line(res["best"], res["title"] or q)
    best = res["best"]
    best["_pick_reasons"] = res.get("best_reasons") or []
    token = state.v8_put({
        "files": res["files"],
        "best": best,
        "meta": None,
        "query": q,
        "user_id": uid,
        "filters": {},
        "filter_opts": v8_ui.v8_filter_options(res["files"]),
        "ai_note": ai_note,
        "corrected": res.get("corrected"),
        "group": True,
    })
    sent = await send_v8_results(client, message.chat.id, token, uid)
    try:
        await wait.delete()
    except Exception:
        pass
    if sent:
        ad = await effective_autodelete(int(message.chat.id))
        if ad > 0:
            await autodelete.schedule(int(message.chat.id), sent.id, ad)
        asyncio.create_task(_fill_meta(client, sent, token, uid,
                                       res["title"] or q,
                                       res["parsed"].get("year")))


async def _fill_meta(client: Client, message: Message, token: str,
                     uid: int, title: str, year: int | None) -> None:
    """Background enrich: add poster/info to an already-rendered result.

    Never raises; silently skips when enrich finds nothing, the results
    expired, or the user moved on. Re-renders the page the user is
    currently on so pagination/filtering is never clobbered.
    """
    try:
        from app import enrich as enrich_mod
        meta = await enrich_mod.enrich_title(title, year, uid)
    except Exception:  # noqa: BLE001
        log.debug("v9 background enrich failed", exc_info=True)
        return
    if not meta:
        return
    data = state.v8_get(token)
    if not data or data.get("meta"):
        return
    if not data.get("group") and data.get("user_id") != uid:
        return
    data["meta"] = meta
    # Re-read the page immediately before the final edit: the user may
    # have paginated while enrich was in flight.
    page = data.get("page", 0)
    try:
        await render_v8_results(client, message, token, uid, page=page)
    except Exception:  # noqa: BLE001
        log.debug("v9 background enrich render failed", exc_info=True)


async def _on_text(client: Client, message: Message):
    if not message.text or message.text.startswith("/"):
        return
    user = await track_user(message)
    if user and user.is_banned:
        return
    uid = message.from_user.id
    kb = await forcesub.ensure_joined(client, uid, chat_id=message.chat.id)
    if kb:
        # v10.3: remember the query — "✅ I've joined" auto-runs it so
        # the user never has to retype their search.
        state.pending_search[uid] = message.text.strip()
        await message.reply_text(
            "📢 <b>Join our channels to use the bot</b>",
            reply_markup=kb)
        return
    q = message.text.strip()
    if len(q) < 2:
        return
    await _handle_text_query(client, message, uid, q)


async def _handle_text_query(client: Client, message: Message,
                             uid: int, q: str) -> None:
    """Everything _on_text does AFTER the force-sub gate.

    Shared by the live handler and fsub_retry (auto-continue after the
    user joins). ``message`` only needs ``reply_text`` + ``chat.id`` —
    the acting user is always ``uid``.
    """
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
    # v10.2: groups use the SAME v8 card model as PM (unified UI).
    await _v9_search_flow_group(client, message, uid, q)


def register(bot: Client) -> None:
    # groups + private, but not channels and not commands.
    # v10.2: every real command is excluded so _on_text never double-fires.
    bot.on_message(
        filters.text & ~filters.command(["start", "help", "trending",
                                         "request", "index", "stats",
                                         "broadcast", "ban", "unban", "warn",
                                         "users", "settings", "requests",
                                         "connect", "groups", "cancel",
                                         "saved", "mystats", "admin",
                                         "deltimer", "dbcheck"])
        & ~filters.channel
    )(_on_text)
