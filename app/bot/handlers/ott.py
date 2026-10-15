"""v11.3 OTT / Latest / Today / Upcoming commands — TMDB backend.

The binged.com and ottplay APIs were unusable (binged returns HTTP 403
to datacenter IPs including Render; ottplay returned empty result sets),
so this module is rebuilt on the TMDB API the bot already uses
(TMDB_API_KEY). Same UX as v11.1/11.2: lists edit in place, tapping a
title opens the download card (web download + Play + Telegram).

v11.1 lesson kept: handlers register on the bot instance inside
register(bot) — never class-level @Client.on_* decorators.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta

import httpx
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

from app import state, tmdb as tmdb_mod
from app.bot import forcesub
from app.bot.handlers.common import is_banned, track_user
from app.bot.ui import esc
from app.config import settings
from app.db import get_session_factory
from app.models import MovieAlert
from app.textutil import title_key

log = logging.getLogger("ott")

TMDB_IMG = "https://image.tmdb.org/t/p"
_SESSION_TTL = 900  # 15 min
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"}

# message_id -> session dict (per list type)
_OTT: dict[int, dict] = {}   # platform picker: {providers, uid, ts}
_OTT_T: dict[int, dict] = {}  # platform titles: {items, pname, pid, page, uid, ts}
_LT: dict[int, dict] = {}
_TD: dict[int, dict] = {}
_UP: dict[int, dict] = {}
_OTT_DL: dict[int, dict] = {}  # download card: {title, uid, ts}


# ---------------------------------------------------------------- data layer

async def _tmdb(path: str, params: dict | None = None) -> dict | None:
    """GET TMDB. Returns parsed JSON or None on any failure."""
    if not settings.TMDB_API_KEY:
        return None
    p = {"api_key": settings.TMDB_API_KEY, "language": "en-US"}
    if params:
        p.update(params)
    try:
        async with httpx.AsyncClient(timeout=20, headers=_UA) as c:
            r = await c.get(f"https://api.themoviedb.org/3{path}", params=p)
            if r.status_code == 200:
                return r.json()
            log.warning("tmdb %s -> %s", path, r.status_code)
    except Exception as exc:  # noqa: BLE001
        log.warning("tmdb %s failed: %s", path, exc)
    return None


def _item(m: dict) -> dict:
    title = m.get("title") or m.get("name") or "?"
    dstr = m.get("release_date") or m.get("first_air_date") or ""
    media = m.get("media_type")
    if media not in ("movie", "tv"):
        media = "tv" if m.get("first_air_date") and not m.get("title") else "movie"
    pp = m.get("poster_path")
    return {
        "id": m.get("id"),
        "title": title,
        "year": dstr[:4],
        "date": dstr,
        "overview": (m.get("overview") or "").strip(),
        "rating": m.get("vote_average"),
        "media": media,
        "pop": m.get("popularity") or 0,
        "poster": f"{TMDB_IMG}/w500{pp}" if pp else None,
    }


def _sweep(d: dict) -> None:
    now = time.time()
    for k in [k for k, v in d.items()
              if now - v.get("ts", 0) > _SESSION_TTL]:
        d.pop(k, None)


# ---------------------------------------------------------------- list UI

def _list_text(heading: str, items: list[dict], page: int,
               per: int = 10) -> tuple[str, list[dict]]:
    start = page * per
    chunk = items[start:start + per]
    lines = [f"<b>{esc(heading)}</b>\n"]
    for i, it in enumerate(chunk, start + 1):
        yr = f" ({it['year']})" if it.get("year") else ""
        tag = "📺" if it["media"] == "tv" else "🎬"
        lines.append(f"{i}. {tag} <b>{esc(it['title'])}</b>{yr}")
    return "\n".join(lines), chunk


def _list_kb(chunk: list[dict], start: int, prefix: str, page: int,
             total_pages: int, back_cb: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    for i, it in enumerate(chunk, start + 1):
        yr = f" ({it['year']})" if it.get("year") else ""
        label = f"{i}. {it['title']}{yr}"[:60]
        rows.append([InlineKeyboardButton(label,
                                          callback_data=f"{prefix}:s:{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}:pg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}:pg:{page + 1}"))
    if nav:
        rows.append(nav)
    if back_cb:
        rows.append([InlineKeyboardButton("🔙 Back", callback_data=back_cb)])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="ot:x")])
    return InlineKeyboardMarkup(rows)


async def _send_list(client: Client, chat_id: int, heading: str, items: list[dict],
                     store: dict, prefix: str, session: dict,
                     back_cb: str | None = None) -> Message | None:
    if not items:
        await client.send_message(chat_id,
                                  f"⚠️ No titles found for <b>{esc(heading)}</b>.",
                                  parse_mode=ParseMode.HTML)
        return None
    text, chunk = _list_text(heading, items, 0)
    total_pages = max(1, -(-len(items) // 10))
    msg = await client.send_message(
        chat_id, text,
        reply_markup=_list_kb(chunk, 0, prefix, 0, total_pages, back_cb),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    session.update({"items": items, "heading": heading, "ts": time.time()})
    store[msg.id] = session
    return msg


async def _edit_list(client: Client, query: CallbackQuery, store: dict,
                     prefix: str, page: int,
                     back_cb: str | None = None) -> None:
    s = store.get(query.message.id)
    if not s:
        await query.answer("⌛ Expired — run the command again.",
                           show_alert=True)
        return
    items, heading = s["items"], s["heading"]
    total_pages = max(1, -(-len(items) // 10))
    page = max(0, min(page, total_pages - 1))
    text, chunk = _list_text(heading, items, page)
    try:
        await query.message.edit_text(
            text, reply_markup=_list_kb(chunk, page * 10, prefix, page,
                                        total_pages, back_cb),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        pass
    await query.answer()


# ---------------------------------------------------------------- /ott

async def ott_cmd(client: Client, message: Message):
    """/ott — pick an OTT platform (India), then browse its titles."""
    uid = message.from_user.id
    if await is_banned(uid):
        return
    await track_user(message)
    _sweep(_OTT)
    data = await _tmdb("/watch/providers/movie", {"watch_region": "IN"})
    provs = (data or {}).get("results") or []
    if not provs:
        await message.reply_text("⚠️ Couldn't load OTT platforms right now.")
        return
    provs = sorted(provs, key=lambda p: p.get("display_priority", 99))[:12]
    rows = []
    for i in range(0, len(provs), 2):
        row = []
        for p in provs[i:i + 2]:
            row.append(InlineKeyboardButton(
                p.get("provider_name", "?")[:28],
                callback_data=f"ot:p:{p.get('provider_id')}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Close", callback_data="ot:x")])
    msg = await message.reply_text(
        "<b>📺 Choose an OTT platform</b>\n<i>India catalogue</i>",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    _OTT[msg.id] = {"providers": {str(p.get("provider_id")): p.get("provider_name")
                                  for p in provs},
                    "uid": uid, "ts": time.time()}


async def _ott_platform_titles(client: Client, query: CallbackQuery,
                               pid: str, pname: str, page: int = 0):
    """Discover movies + series on one platform, merged by popularity."""
    await query.answer("⏳ Loading titles…")
    params = {"with_watch_providers": pid, "watch_region": "IN",
              "sort_by": "popularity.desc", "page": 1}
    mv = await _tmdb("/discover/movie", params) or {}
    tv = await _tmdb("/discover/tv", params) or {}
    items = sorted((_item(m) for m in (mv.get("results") or []) +
                    (tv.get("results") or [])),
                   key=lambda x: x["pop"], reverse=True)[:40]
    _sweep(_OTT_T)
    msg = await _send_list(client, query.message.chat.id, pname, items,
                           _OTT_T, "otp",
                           {"uid": query.from_user.id, "pid": pid,
                            "pname": pname})
    if msg:
        await query.answer()
    # keep the platform picker message tidy
    try:
        await query.message.edit_reply_markup(None)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- /latest

async def latest_cmd(client: Client, message: Message):
    """/latest — this week's trending movies & shows (TMDB)."""
    uid = message.from_user.id
    if await is_banned(uid):
        return
    await track_user(message)
    _sweep(_LT)
    data = await _tmdb("/trending/all/week") or {}
    items = [_item(m) for m in (data.get("results") or [])
             if (m.get("title") or m.get("name"))][:40]
    await _send_list(client, message.chat.id, "🔥 Trending this week", items,
                     _LT, "lt", {"uid": uid})


# ---------------------------------------------------------------- /today

async def today_cmd(client: Client, message: Message):
    """/today — movies & shows released today (India)."""
    uid = message.from_user.id
    if await is_banned(uid):
        return
    await track_user(message)
    _sweep(_TD)
    today = date.today().isoformat()
    mv = await _tmdb("/discover/movie",
                     {"primary_release_date.gte": today,
                      "primary_release_date.lte": today,
                      "region": "IN", "sort_by": "popularity.desc"}) or {}
    tv = await _tmdb("/discover/tv",
                     {"first_air_date.gte": today, "first_air_date.lte": today,
                      "sort_by": "popularity.desc"}) or {}
    items = sorted((_item(m) for m in (mv.get("results") or []) +
                    (tv.get("results") or [])),
                   key=lambda x: x["pop"], reverse=True)[:40]
    await _send_list(client, message.chat.id, "📅 Released today", items,
                     _TD, "td", {"uid": uid})


# ---------------------------------------------------------------- /upcoming

async def upcoming_cmd(client: Client, message: Message):
    """/upcoming — upcoming movies (India), with details + notify."""
    uid = message.from_user.id
    if await is_banned(uid):
        return
    await track_user(message)
    _sweep(_UP)
    data = await _tmdb("/movie/upcoming", {"region": "IN", "page": 1}) or {}
    items = [_item(m) for m in (data.get("results") or [])][:40]
    await _send_list(client, message.chat.id, "🔜 Coming soon", items,
                     _UP, "up", {"uid": uid})


async def _upcoming_detail(client: Client, query: CallbackQuery, idx: int):
    """Poster + overview + release date, with Notify + Download."""
    s = _UP.get(query.message.id)
    if not s or s["uid"] != query.from_user.id:
        await query.answer("⌛ Expired — run /upcoming again.",
                           show_alert=True)
        return
    items = s["items"]
    if not 1 <= idx <= len(items):
        return
    it = items[idx - 1]
    key = title_key(f"{it['title']} {it['year']}".strip())
    # already notified?
    notified = False
    try:
        sf = get_session_factory(0)
        async with sf() as db:
            from sqlalchemy import select as _sel
            r = await db.execute(
                _sel(MovieAlert).where(MovieAlert.user_id == query.from_user.id,
                                       MovieAlert.title_key == key))
            notified = r.scalar_one_or_none() is not None
    except Exception:  # noqa: BLE001
        pass
    yr = f" ({it['year']})" if it.get("year") else ""
    rating = f"⭐ {it['rating']:.1f}/10" if it.get("rating") else ""
    rel = it.get("date") or "TBA"
    ov = (it["overview"][:350] + "…") if len(it["overview"]) > 350 else it["overview"]
    text = (f"🎬 <b>{esc(it['title'])}</b>{yr}\n"
            f"{rating}   📅 <b>{esc(rel)}</b>\n\n"
            f"<i>{esc(ov)}</i>" if ov else
            f"🎬 <b>{esc(it['title'])}</b>{yr}\n{rating}   📅 <b>{esc(rel)}</b>")
    rows = [[InlineKeyboardButton("🌐 Download", callback_data=f"up:dl:{idx}")]]
    if notified:
        rows.append([InlineKeyboardButton("🔕 Stop alerts",
                                          callback_data=f"up:un:{idx}")])
    else:
        rows.append([InlineKeyboardButton("🔔 Notify me on release",
                                          callback_data=f"up:n:{idx}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="up:back")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="ot:x")])
    kb = InlineKeyboardMarkup(rows)
    try:
        if it.get("poster"):
            await client.send_photo(query.message.chat.id, it["poster"],
                                    caption=text, reply_markup=kb,
                                    parse_mode=ParseMode.HTML)
        else:
            await client.send_message(query.message.chat.id, text,
                                      reply_markup=kb,
                                      parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        pass
    await query.answer()


async def _upcoming_notify(client: Client, query: CallbackQuery, idx: int,
                           subscribe: bool):
    s = _UP.get(query.message.id)
    if not s or s["uid"] != query.from_user.id:
        await query.answer("⌛ Expired.", show_alert=True)
        return
    items = s["items"]
    if not 1 <= idx <= len(items):
        return
    it = items[idx - 1]
    key = title_key(f"{it['title']} {it['year']}".strip())
    uid = query.from_user.id
    try:
        sf = get_session_factory(0)
        async with sf() as db:
            from sqlalchemy import select as _sel, delete as _del
            if subscribe:
                r = await db.execute(
                    _sel(MovieAlert).where(MovieAlert.user_id == uid,
                                           MovieAlert.title_key == key))
                if r.scalar_one_or_none() is None:
                    db.add(MovieAlert(user_id=uid, title_key=key,
                                      title=it["title"][:200],
                                      year=int(it["year"]) if it.get("year") and
                                      it["year"].isdigit() else None))
                    await db.commit()
                await query.answer("🔔 I'll ping you when it's out!",
                                   show_alert=True)
            else:
                await db.execute(_del(MovieAlert).where(
                    MovieAlert.user_id == uid, MovieAlert.title_key == key))
                await db.commit()
                await query.answer("🔕 Alerts off.", show_alert=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("notify toggle failed: %s", exc)
        await query.answer("⚠️ Try again later.", show_alert=True)


# ---------------------------------------------------------------- download card

async def _ott_download_card(client: Client, query, title: str):
    """v11.2: download card — best file with a WEB download option.

    "Download option" = file download through the web (/dl link),
    plus Play (web player) and in-Telegram delivery choices.
    """
    from app import search_v9
    from app.web.tokens import dl_url, watch_url
    from app.bot.ui import fmt_size
    from app.textutil import clean_display_name
    uid = query.from_user.id
    chat_id = query.message.chat.id
    title = (title or "").strip()
    if not title:
        await query.answer("⚠️ No title.", show_alert=True)
        return
    kb = await forcesub.ensure_joined(client, uid, chat_id=chat_id)
    if kb:
        state.pending_search[uid] = title
        from app.bot.handlers.callbacks import send_join_prompt
        await send_join_prompt(client, query.message, uid, kb,
                               chat_id=chat_id)
        await query.answer()
        return
    await query.answer("🔍 Finding file…")
    try:
        res = await search_v9.smart_search(uid, title)
    except Exception as exc:  # noqa: BLE001
        log.warning("ott dl-card search failed for %r: %s", title, exc)
        res = {}
    if res.get("status") not in ("ok", "uncertain") or not res.get("best"):
        await query.answer("❌ No files found for this title yet.",
                           show_alert=True)
        return
    best = res["best"]
    gid = best["id"]
    name = clean_display_name(best.get("file_name") or title)
    meta = " · ".join(x for x in (
        best.get("quality"), best.get("language"),
        fmt_size(best.get("file_size"))) if x)
    text = (f"🎬 <b>{esc(name)}</b>\n"
            + (f"<i>{esc(meta)}</i>\n" if meta else "")
            + "\n<i>Choose how to get it:</i>")
    rows = []
    w = watch_url(gid, uid)
    d = dl_url(gid, uid)
    web_row = []
    if d:
        web_row.append(InlineKeyboardButton("🌐 Download", url=d))
    if w:
        from pyrogram.types import WebAppInfo
        web_row.append(InlineKeyboardButton("▶ Play",
                                            web_app=WebAppInfo(url=w)))
    if web_row:
        rows.append(web_row)
    rows.append([InlineKeyboardButton("📲 Get in Telegram",
                                      callback_data=f"dl:{gid}")])
    _OTT_DL[query.message.id] = {"title": title, "uid": uid,
                                 "ts": time.time()}
    rows.append([InlineKeyboardButton("🔍 More files",
                                      callback_data="ot:more")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="ot:x")])
    await client.send_message(chat_id, text,
                              reply_markup=InlineKeyboardMarkup(rows),
                              parse_mode=ParseMode.HTML,
                              disable_web_page_preview=True)


async def _ott_more_files(client: Client, query):
    """'🔍 More files' — full v8 results card for the stored title."""
    from app.bot.handlers.search import _search_and_send
    uid = query.from_user.id
    s = _OTT_DL.get(query.message.id)
    if not s or s["uid"] != uid:
        await query.answer("⚠️ Session expired.", show_alert=True)
        return
    chat_id = query.message.chat.id
    title = (s["title"] or "").strip()
    kb = await forcesub.ensure_joined(client, uid, chat_id=chat_id)
    if kb:
        state.pending_search[uid] = title
        from app.bot.handlers.callbacks import send_join_prompt
        await send_join_prompt(client, query.message, uid, kb,
                               chat_id=chat_id)
        await query.answer()
        return
    await query.answer("🔍 Searching…")
    try:
        sent = await _search_and_send(client, chat_id, uid, title)
    except Exception as exc:  # noqa: BLE001
        log.warning("ott more-files search failed for %r: %s", title, exc)
        sent = None
    if not sent:
        await query.answer("❌ No files found for this title yet.",
                           show_alert=True)


# ---------------------------------------------------------------- callbacks

async def ott_cb(client: Client, query: CallbackQuery):
    """/ott + /latest + /today + /upcoming callback router."""
    data = query.data or ""
    uid = query.from_user.id

    if data == "ot:x":
        for d in (_OTT, _OTT_T, _LT, _TD, _UP, _OTT_DL):
            d.pop(query.message.id, None)
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass
        await query.answer()
        return
    if data == "ot:more":
        await _ott_more_files(client, query)
        return

    # /ott platform pick
    if data.startswith("ot:p:"):
        s = _OTT.get(query.message.id)
        if not s or s["uid"] != uid:
            await query.answer("⌛ Expired — run /ott again.",
                               show_alert=True)
            return
        pid = data.split(":", 2)[2]
        pname = s["providers"].get(pid, "OTT")
        await _ott_platform_titles(client, query, pid, pname)
        return

    # generic title-tap: <prefix>:s:<n>  (otp/lt/td)
    m = re.match(r"^(otp|lt|td):s:(\d+)$", data)
    if m:
        prefix, idx = m.group(1), int(m.group(2))
        store = {"otp": _OTT_T, "lt": _LT, "td": _TD}[prefix]
        s = store.get(query.message.id)
        if not s or s["uid"] != uid:
            await query.answer("⌛ Expired — run the command again.",
                               show_alert=True)
            return
        items = s["items"]
        if not 1 <= idx <= len(items):
            return
        it = items[idx - 1]
        q = it["title"] + (f" {it['year']}" if it.get("year") else "")
        await _ott_download_card(client, query, q)
        return

    # generic pagination: <prefix>:pg:<page>
    m = re.match(r"^(otp|lt|td):pg:(\d+)$", data)
    if m:
        prefix, page = m.group(1), int(m.group(2))
        store = {"otp": _OTT_T, "lt": _LT, "td": _TD}[prefix]
        s = store.get(query.message.id)
        if not s or s["uid"] != uid:
            await query.answer("⌛ Expired.", show_alert=True)
            return
        back = "ot:back" if prefix == "otp" else None
        await _edit_list(client, query, store, prefix, page, back)
        return

    # /upcoming title tap -> detail card
    m = re.match(r"^up:s:(\d+)$", data)
    if m:
        await _upcoming_detail(client, query, int(m.group(1)))
        return
    m = re.match(r"^up:pg:(\d+)$", data)
    if m:
        await _edit_list(client, query, _UP, "up", int(m.group(1)))
        return
    m = re.match(r"^up:dl:(\d+)$", data)
    if m:
        s = _UP.get(query.message.id)
        if not s or s["uid"] != uid:
            await query.answer("⌛ Expired.", show_alert=True)
            return
        it = s["items"][int(m.group(1)) - 1]
        q = it["title"] + (f" {it['year']}" if it.get("year") else "")
        await _ott_download_card(client, query, q)
        return
    m = re.match(r"^up:n:(\d+)$", data)
    if m:
        await _upcoming_notify(client, query, int(m.group(1)), True)
        return
    m = re.match(r"^up:un:(\d+)$", data)
    if m:
        await _upcoming_notify(client, query, int(m.group(1)), False)
        return


# ---------------------------------------------------------------- register

def register(bot: Client) -> None:
    bot.on_message(filters.command("ott"))(ott_cmd)
    bot.on_message(filters.command("latest"))(latest_cmd)
    bot.on_message(filters.command("today"))(today_cmd)
    bot.on_message(filters.command("upcoming"))(upcoming_cmd)
    bot.on_callback_query(filters.regex(r"^(ot:|otp:|lt:|td:|up:)"))(ott_cb)
    # legacy v11.0 prefixes (kept so old buttons don't die silently)
    bot.on_callback_query(filters.regex(r"^ott_"))(ott_cb)
