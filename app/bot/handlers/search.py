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
                   query: str | None = None,
                   sid: str | None = None
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
                                   "chat_id": None, "sid": sid}
        rows.append([InlineKeyboardButton("🎞 Request Movie",
                                          callback_data=f"req:{token}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def _no_results_pm(client: Client, message: Message, q: str,
                        uid: int | None = None,
                        sid: str | None = None):
    """No-results flow for PM (v10.8): the AI recovery already ran and
    found nothing — show "no result found" + a Request Movie button
    carrying the ORIGINAL user message.
    """
    log.info("[s:%s] flow: no-results card for %r", sid, q[:60])
    text = (f"📭 <b>No results found</b>\n\n"
            f"I looked everywhere for \"<b>{ui.esc(q[:80])}</b>\"."
            f"\n\nTap 🎞 <b>Request Movie</b> and we'll try to add it."
            if settings.REQUEST_CHANNEL
            else f"\n\nTry a different spelling.")
    await message.reply_text(
        text, reply_markup=_no_results_kb(uid=uid, query=q, sid=sid),
        parse_mode=ParseMode.HTML)


async def _ai_choose(client: Client, wait: Message, uid: int,
                   res: dict, group: bool = False) -> bool:
    """v10.8: Grok returned several verified titles — ask the user which
    one they want. Each button runs a normal search with that title;
    "request instead" saves the ORIGINAL message as a movie request.
    """
    choices = res.get("choices") or []
    original_q = res.get("raw") or ""
    sid = res.get("sid")
    token = secrets.token_hex(8)
    state.ait_tokens[token] = {"uid": uid, "original": original_q,
                               "titles": [c["title"] for c in choices],
                               "sid": sid, "group": group}
    rows = []
    for i, c in enumerate(choices[:5]):
        label = c["title"][:40]
        if c.get("year"):
            label += f" ({c['year']})"
        rows.append([InlineKeyboardButton(f"🎬 {label}",
                                          callback_data=f"ait:{token}:{i}")])
    rows.append([InlineKeyboardButton("🎞 None — request instead",
                                      callback_data=f"ait:{token}:req")])
    kb = InlineKeyboardMarkup(rows)
    try:
        await wait.delete()
    except Exception:
        pass
    log.info("[s:%s] flow: choose among %d titles (original %r)", sid,
             len(choices), original_q[:60])
    await wait.reply_text(
        "🎬 <b>Which one did you mean?</b>\n\n"
        "<i>Tap the movie/series you want:</i>",
        reply_markup=kb, parse_mode=ParseMode.HTML)
    return True


async def _ai_suggest(client: Client, wait: Message, uid: int,
                      res: dict, group: bool = False) -> bool:
    """v10.8.8: JustWatch/AI returned titles but none have files.

    Show them as tappable buttons — tapping a title requests THAT
    title; "request instead" requests the original message text.
    """
    suggestions = res.get("suggestions") or []
    original_q = res.get("raw") or ""
    sid = res.get("sid")
    token = secrets.token_hex(8)
    state.ait_tokens[token] = {"uid": uid, "original": original_q,
                               "titles": [s["title"] for s in suggestions],
                               "sid": sid, "group": group}
    rows = []
    for i, s in enumerate(suggestions[:5]):
        label = s["title"][:40]
        if s.get("year"):
            label += f" ({s['year']})"
        rows.append([InlineKeyboardButton(
            f"🎬 {label}", callback_data=f"ais:{token}:{i}")])
    rows.append([InlineKeyboardButton("📝 None — request my text instead",
                                      callback_data=f"ais:{token}:req")])
    kb = InlineKeyboardMarkup(rows)
    try:
        await wait.delete()
    except Exception:
        pass
    log.info("[s:%s] flow: suggest %d titles (original %r)", sid,
             len(suggestions), original_q[:60])
    await wait.reply_text(
        "🤔 <b>No files found — did you mean one of these?</b>\n\n"
        f"<i>Nothing in the library for {ui.esc(original_q[:60])}. "
        "Tap a title to request it:</i>",
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

    # v10: smart_search already ran the recovery chain (instant ->
    # fuzzy -> Grok AI). Nothing found -> Request Movie card.
    sid = res.get("sid")
    if res.get("status") == "choose" and not _confirmed:
        return await _ai_choose(client, wait, uid, res)
    if res.get("status") == "suggest" and not _confirmed:
        return await _ai_suggest(client, wait, uid, res)
    if res.get("status") not in ("ok", "uncertain") or not res.get("best"):
        log.info("[s:%s] flow: no results -> request card", sid)
        try:
            await wait.delete()
        except Exception:
            pass
        await _no_results_pm(client, message, q, uid, sid=sid)
        return True

    log.info("[s:%s] flow: rendering %d files", sid, len(res["files"]))

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
    if res.get("status") == "choose":
        return await _ai_choose(client, wait, uid, res, group=True)
    if res.get("status") == "suggest":
        return await _ai_suggest(client, wait, uid, res, group=True)
    if res.get("status") not in ("ok", "uncertain") or not res.get("best"):
        try:
            await wait.delete()
        except Exception:
            pass
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
