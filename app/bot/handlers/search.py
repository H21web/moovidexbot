"""Text search (private + groups) — v10.6: smart search (DB -> Search API -> Grok fallback) + did-you-mean confirm + Request Movie."""
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

from app import personalize, state
from app import intent as intent_mod, search_v9
from app import autodelete
from app.analytics import log_event
from app.bot import forcesub, ui, v8_ui
from app.bot.handlers.common import track_user
from app.bot.handlers.groups import effective_autodelete
from app.config import settings
from app.db import get_session_factory
from app.spell import suggest
from app.tmdb import resolve_title

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


def _verdict_line(best: dict, title: str) -> str:
    """One-line best-pick note. Local only — no AI, no quota.

    (moved from app.ai_assist, which is deleted.) Always returns a
    non-empty line, so the verdict renders on every result.
    """
    dl = (best or {}).get("downloads") or 0
    if dl:
        return f"Most downloaded pick — {dl} downloads"
    bits = [x for x in ((best or {}).get("quality"),
                        (best or {}).get("language")) if x]
    if bits:
        return f"Best {' '.join(bits)} match for \u201c{title}\u201d"
    return f"Top match for \u201c{title}\u201d"


def _no_results_kb(base: InlineKeyboardMarkup | None = None,
                   uid: int | None = None,
                   query: str | None = None
                   ) -> InlineKeyboardMarkup:
    """No-results keyboard: keep ``base`` rows, add Request Movie.

    v10.6 (flow diagram): the button carries a one-time token, so
    tapping it saves the original search as a movie request and the
    user gets "Request submitted" — no extra typing.
    """
    rows = [list(r) for r in (base.inline_keyboard if base else [])]
    if settings.REQUEST_CHANNEL and uid is not None and query:
        token = secrets.token_hex(8)
        state.req_tokens[token] = {"uid": uid, "q": query,
                                   "chat_id": None}
        rows.append([InlineKeyboardButton("🎞 Request Movie",
                                          callback_data=f"req:{token}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def _no_results_pm(client: Client, message: Message, q: str,
                        uid: int | None = None,
                        suggestions: list[str] | None = None):
    """No-results flow for PM: TMDB correction -> spell suggestions.

    v9: the AI recovery chain (title correction -> retry) already ran
    inside _v9_search_flow. ``suggestions`` lets the caller pass
    pre-computed spell suggestions so they aren't looked up twice.
    """
    text = (f"📭 <b>No results found</b>\n\n"
            f"I looked everywhere for \"<b>{ui.esc(q[:80])}</b>\".")
    if suggestions is None:
        # Never send raw user text to TMDB: resolve a clean title first
        # (local extraction -> TMDB -> web-search fallback).
        tm = await resolve_title(q)
        if tm and tm.get("title"):
            year = f" ({tm['year']})" if tm.get("year") else ""
            text = (
                f"🤔 <b>Did you mean {ui.esc(tm['title'])}</b>{year}?\n\n"
                "📭 It's not in the database yet."
            )
            await message.reply_text(
                text, reply_markup=_no_results_kb(uid=uid, query=q),
                parse_mode=ParseMode.HTML)
            return
        try:
            factory = get_session_factory(settings.DATABASE_URL)
            async with factory() as s:
                suggestions = await suggest(s, q)
        except Exception:  # noqa: BLE001
            suggestions = []
    if suggestions:
        text += "\n\n<b>Did you mean:</b>"
        kb = _no_results_kb(ui.spell_kb(suggestions), uid=uid, query=q)
    else:
        text += ("\n\nTap 🎞 <b>Request Movie</b> — we'll try to add it."
                 if settings.REQUEST_CHANNEL
                 else "\n\nTry a different spelling.")
        kb = _no_results_kb(uid=uid, query=q)
    await message.reply_text(text, reply_markup=kb,
                             parse_mode=ParseMode.HTML)


async def _did_you_mean(client: Client, wait: Message, uid: int,
                       original_q: str, corrected_title: str) -> bool:
    """Flow diagram: a Search-API / Grok corrected title found files —
    confirm with the user before showing results.

    ✅ Yes -> normal AutoFilter search with the corrected title.
    🎞 Request movie -> save the ORIGINAL search as a movie request.
    """
    token = secrets.token_hex(8)
    state.dym_tokens[token] = {"uid": uid, "original": original_q,
                               "corrected": corrected_title}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, show files",
                              callback_data=f"dym:{token}:yes")],
        [InlineKeyboardButton("🎞 Request movie",
                              callback_data=f"dym:{token}:no")],
    ])
    try:
        await wait.delete()
    except Exception:
        pass
    await wait.reply_text(
        f"🔍 <b>Did you mean</b>\n"
        f"🎬 <b>{ui.esc(corrected_title)}</b>\n\n"
        f"<i>Nothing found for \"{ui.esc(original_q[:60])}\".</i>",
        reply_markup=kb, parse_mode=ParseMode.HTML)
    return True


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
    kb = v8_ui.v8_results_kb(token, uid, page, pages,
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
                         uid: int, q: str, _confirmed: bool = False) -> bool:
    """v10.2 PM search: fast hot path, smart recovery, instant render.

    Always handles the message (True) except on unexpected failure.
    Flow (diagram): normalize -> PostgreSQL/AutoFilter -> Search API ->
    Grok AI -> did-you-mean confirm -> results, else Request Movie.
    ``_confirmed`` skips the did-you-mean prompt after the user tapped
    ✅ Yes on it.
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
    # web title -> Grok AI). Nothing found -> Request Movie card.
    suggestions: list[str] | None = None
    if res.get("status") not in ("ok", "uncertain") or not res.get("best"):
        try:
            await wait.delete()
        except Exception:
            pass
        await _no_results_pm(client, message, q, uid,
                             suggestions=suggestions)
        return True

    # v10.6 (flow diagram): a Search-API / Grok corrected title found
    # files — confirm before showing results.
    if not _confirmed and res.get("corrected_via") in ("web", "ai"):
        return await _did_you_mean(client, wait, uid, q,
                                   res["title"] or q)

    ai_note = _verdict_line(res["best"], res["title"] or q)
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
    ai_note = _verdict_line(res["best"], res["title"] or q)
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
    ai_note = _verdict_line(res["best"], res["title"] or q)
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
            await message.reply_text(
                "🔍 <i>I only do movie/series search.</i>\n"
                "Send me a title to find its files \U0001F50D",
                parse_mode=ParseMode.HTML)
            return
        elif intent == "greeting":
            await message.reply_text(
                "\U0001F44B Hey! Send me a movie or series name \U0001F50D")
            return
        elif intent == "request":
            await message.reply_text(
                "\U0001F39E To request a movie, use /request &lt;movie name&gt;")
            return
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
