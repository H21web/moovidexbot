"""OTT discovery (v11): /ott /latest /today /upcoming.

Adapted from standalone plugins into Moovidex's architecture:
- no ``utils.temp`` / ``info.py`` / ``plugins.Extra`` — sessions live in
  module dicts with TTL, admin/channel come from ``app.config``.
- inline buttons everywhere (no reply keyboards).
- deep links reuse the built-in ``srch_`` search links.
- /today was an empty file — implemented via the ottplay new-release
  API filtered to today.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta

import httpx
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app import tmdb as tmdb_mod
from app.bot.handlers.common import is_banned, track_user
from app.config import settings
from app.db import get_session_factory
from app.models import MovieAlert
from app.ott_cards import generate_status_image
from app.releases import search_deep_link
from app.textutil import title_key

log = logging.getLogger(__name__)

BINGED_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.binged.com/",
    "Origin": "https://www.binged.com",
}
OTTPLAY_UA = {
    "User-Agent": BINGED_UA["User-Agent"],
    "Accept": "application/json",
    "Referer": "https://www.ottplay.com/",
    "Origin": "https://www.ottplay.com",
}
WHATS_STREAMING = "https://www.binged.com/wp-json/binged-api/v1/whats-streaming"
BINGED_MOVIES = "https://www.binged.com/wp-json/binged-api/v1/movies"
OTTPLAY_NEW = "https://api2.ottplay.com/api/v4.7/web/new-release"

PLATFORM_IMAGES = {
    "amazon": "https://envs.sh/FI0.jpg",
    "netflix": "https://envs.sh/FIS.jpg",
    "zee5": "https://envs.sh/FIW.jpg",
    "aha video": "https://envs.sh/FIB.jpg",
    "hoichoi": "https://envs.sh/FII.jpg",
    "jio cinema": "https://envs.sh/FIn.jpg",
    "sun nxt": "https://envs.sh/FIT.jpg",
}

_SESS_TTL = 600  # 10 min per interactive session


def clean_text(text):
    if not text:
        return text
    text = html.unescape(text)
    for k, v in {"\u2019": "'", "\u2018": "'", "\u201c": '"',
                 "\u201d": '"', "\u2013": "-", "\u2014": "-",
                 "\u2026": "..."}.items():
        text = text.replace(k, v)
    return text.strip()


_bot_username: str | None = None


async def _username(client: Client) -> str | None:
    global _bot_username
    if _bot_username:
        return _bot_username
    try:
        me = await client.get_me()
        _bot_username = me.username or None
    except Exception:  # noqa: BLE001
        pass
    return _bot_username


def _prune(store: dict):
    now = time.time()
    for k in [k for k, v in store.items()
              if now - v.get("ts", 0) > _SESS_TTL]:
        store.pop(k, None)


async def _fetch_json(url, headers, params=None, timeout=12):
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers,
                                     trust_env=False) as s:
            r = await s.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            log.debug("ott fetch %d: %s", r.status_code, url)
    except Exception as exc:  # noqa: BLE001
        log.debug("ott fetch failed: %s", exc)
    return None


def _close_kb(cb: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Close", callback_data=cb)]])


# ---------------------------------------------------------------- /ott ----

_OTT: dict[int, dict] = {}  # message_id -> session


async def _ott_platforms():
    data = await _fetch_json(WHATS_STREAMING, BINGED_UA)
    if not isinstance(data, dict):
        return None
    return list(data.values())


@Client.on_message(filters.command("ott"))
async def ott_cmd(client: Client, message):
    await track_user(message)
    if message.from_user and await is_banned(message.from_user.id):
        return
    wait = await message.reply_text("📺 <i>Loading platforms…</i>",
                                    parse_mode=ParseMode.HTML)
    platforms = await _ott_platforms()
    if not platforms:
        await wait.edit_text("⚠️ Couldn't load OTT platforms. Try again later.")
        return
    _prune(_OTT)
    rows = [[InlineKeyboardButton(clean_text(p.get("title", "?"))[:40],
                                  callback_data=f"ot:p:{i}")]
            for i, p in enumerate(platforms)]
    rows.append([InlineKeyboardButton("❌ Close", callback_data="ot:x")])
    await wait.edit_text("📺 <b>Select a platform:</b>",
                         reply_markup=InlineKeyboardMarkup(rows),
                         parse_mode=ParseMode.HTML)
    _OTT[wait.id] = {"uid": message.from_user.id, "platforms": platforms,
                     "ts": time.time()}


async def _ott_show_platform(client, query, sess, page=0):
    msg = query.message
    plat = sess["platform"]
    movies = plat.get("movies", [])
    title = plat.get("title", "Platform")
    per, pages = 8, max(1, -(-len(movies) // 8))
    page = max(0, min(page, pages - 1))
    chunk = movies[page * per:(page + 1) * per]
    uname = await _username(client)
    rows = []
    for m in chunk:
        mt = clean_text(m.get("title", "Untitled"))[:40]
        if uname:
            rows.append([InlineKeyboardButton(
                f"🎬 {mt}", url=search_deep_link(uname, mt))])
        else:
            rows.append([InlineKeyboardButton(f"🎬 {mt}",
                                              callback_data="ot:noop")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev",
                                        callback_data=f"ot:pg:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️",
                                        callback_data=f"ot:pg:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Platforms",
                                      callback_data="ot:back")])
    kb = InlineKeyboardMarkup(rows)
    caption = (f"🎬 <b>{html.escape(title)}</b>\n"
               f"<i>Page {page + 1}/{pages}</i>")
    logo = PLATFORM_IMAGES.get(title.lower().strip(), "")
    # Always delete + resend: works for both text and photo messages
    # (edit_text on a photo message fails — the original bug).
    try:
        await msg.delete()
    except Exception:  # noqa: BLE001
        pass
    if logo:
        try:
            sent = await client.send_photo(msg.chat.id, photo=logo,
                                           caption=caption,
                                           reply_markup=kb,
                                           parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001
            sent = await client.send_message(msg.chat.id, caption,
                                             reply_markup=kb,
                                             parse_mode=ParseMode.HTML)
    else:
        sent = await client.send_message(msg.chat.id, caption,
                                         reply_markup=kb,
                                         parse_mode=ParseMode.HTML)
    _OTT.pop(msg.id, None)
    sess["page"] = page
    sess["ts"] = time.time()
    _OTT[sent.id] = sess


@Client.on_callback_query(filters.regex(r"^ot:"))
async def ott_cb(client: Client, query):
    data = query.data or ""
    _prune(_OTT)
    if data == "ot:noop":
        await query.answer()
        return
    if data == "ot:x":
        _OTT.pop(query.message.id, None)
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass
        await query.answer()
        return
    sess = _OTT.get(query.message.id)
    if not sess or sess["uid"] != query.from_user.id:
        await query.answer("⚠️ Session expired — send /ott again.",
                           show_alert=True)
        return
    if data.startswith("ot:p:"):
        try:
            idx = int(data.split(":")[2])
            sess["platform"] = sess["platforms"][idx]
        except (ValueError, IndexError):
            await query.answer("⚠️ Invalid selection.", show_alert=True)
            return
        await query.answer()
        await _ott_show_platform(client, query, sess, page=0)
    elif data.startswith("ot:pg:"):
        try:
            await _ott_show_platform(client, query, sess,
                                     page=int(data.split(":")[2]))
        except ValueError:
            pass
        await query.answer()
    elif data == "ot:back":
        rows = [[InlineKeyboardButton(
            clean_text(p.get("title", "?"))[:40],
            callback_data=f"ot:p:{i}")]
            for i, p in enumerate(sess["platforms"])]
        rows.append([InlineKeyboardButton("❌ Close",
                                          callback_data="ot:x")])
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass
        sent = await client.send_message(
            query.message.chat.id, "📺 <b>Select a platform:</b>",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode=ParseMode.HTML)
        _OTT.pop(query.message.id, None)
        sess["ts"] = time.time()
        _OTT[sent.id] = sess
        await query.answer()


# -------------------------------------------------------------- /latest ----

_LATEST: dict[int, dict] = {}  # user_id -> {page, total, movies, ts}


async def _latest_page(page=1):
    data = await _fetch_json(f"{BINGED_MOVIES}?page={page}", BINGED_UA)
    if not data:
        return [], 1
    return data.get("data", []), data.get("pagination", {}).get(
        "total_pages", 1)


async def _latest_render(client, chat_id, uid, edit_id=None):
    _prune(_LATEST)
    s = _LATEST.get(uid)
    if not s:
        return None
    uname = await _username(client)
    rows = []
    for m in s["movies"][:12]:
        t = clean_text(m.get("title", "Untitled"))[:40]
        if uname:
            rows.append([InlineKeyboardButton(
                f"🎬 {t}", url=search_deep_link(uname, t))])
    nav = []
    if s["page"] > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev",
                                        callback_data="lt:pg:prev"))
    if s["page"] < s["total"]:
        nav.append(InlineKeyboardButton("Next ➡️",
                                        callback_data="lt:pg:next"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Close", callback_data="lt:x")])
    text = (f"🎬 <b>Latest streaming movies</b>\n"
            f"<i>Page {s['page']}/{s['total']} — tap a title to search it:</i>")
    kb = InlineKeyboardMarkup(rows)
    if edit_id:
        try:
            await client.edit_message_text(chat_id, edit_id, text,
                                           reply_markup=kb,
                                           parse_mode=ParseMode.HTML,
                                           disable_web_page_preview=True)
            return edit_id
        except Exception:  # noqa: BLE001
            pass
    sent = await client.send_message(chat_id, text, reply_markup=kb,
                                     parse_mode=ParseMode.HTML,
                                     disable_web_page_preview=True)
    return sent.id


@Client.on_message(filters.command("latest"))
async def latest_cmd(client: Client, message):
    await track_user(message)
    if message.from_user and await is_banned(message.from_user.id):
        return
    wait = await message.reply_text("🎬 <i>Loading latest movies…</i>",
                                    parse_mode=ParseMode.HTML)
    movies, total = await _latest_page(1)
    if not movies:
        await wait.edit_text("⚠️ Couldn't load movies. Try again later.")
        return
    _prune(_LATEST)
    _LATEST[message.from_user.id] = {"page": 1, "total": total,
                                     "movies": movies, "ts": time.time()}
    try:
        await wait.delete()
    except Exception:  # noqa: BLE001
        pass
    await _latest_render(client, message.chat.id, message.from_user.id)


@Client.on_callback_query(filters.regex(r"^lt:"))
async def latest_cb(client: Client, query):
    data = query.data or ""
    uid = query.from_user.id
    _prune(_LATEST)
    s = _LATEST.get(uid)
    if data == "lt:x":
        _LATEST.pop(uid, None)
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass
        await query.answer()
        return
    if not s:
        await query.answer("⚠️ Session expired — send /latest again.",
                           show_alert=True)
        return
    if data == "lt:pg:next" and s["page"] < s["total"]:
        movies, _ = await _latest_page(s["page"] + 1)
        if movies:
            s["page"] += 1
            s["movies"] = movies
    elif data == "lt:pg:prev" and s["page"] > 1:
        movies, _ = await _latest_page(s["page"] - 1)
        if movies:
            s["page"] -= 1
            s["movies"] = movies
    s["ts"] = time.time()
    await _latest_render(client, query.message.chat.id, uid,
                         edit_id=query.message.id)
    await query.answer()


# ------------------------------------------------------- /today /upcoming ----

_UPCOMING: dict[str, tuple[float, list]] = {}  # key -> (ts, movies)
_UP_DETAIL: dict[str, dict] = {}  # f"{uid}:{mid}" -> processed


def _upcoming_cached(key="upcoming"):
    hit = _UPCOMING.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    return None


async def _fetch_upcoming(from_date, to_date, limit=50, cache_key="upcoming"):
    cached = _upcoming_cached(cache_key)
    if cached is not None:
        return cached
    params = {"limit": limit, "from_date": from_date, "to_date": to_date,
              "content_type": "all", "language": "", "provider": ""}
    data = await _fetch_json(OTTPLAY_NEW, OTTPLAY_UA, params=params,
                             timeout=15)
    movies = []
    if data:
        movies = data.get("result") or data.get("data") or []
    _UPCOMING[cache_key] = (time.time(), movies)
    return movies


def _movie_date(m):
    d = m.get("release_date")
    if not d and m.get("where_to_watch"):
        d = m.get("where_to_watch")[0].get("available_from")
    return d


def _fmt_date(d):
    if not d:
        return "N/A"
    try:
        return datetime.fromisoformat(
            d.replace("Z", "+00:00")).strftime("%d %b %Y")
    except Exception:  # noqa: BLE001
        return d[:10]


async def _process_movie(m):
    title = clean_text(m.get("name", "Untitled"))
    year = m.get("release_year")
    ct = m.get("content_type", "movie")
    type_str = "Series" if ct != "movie" else "Movie"
    lang = (m.get("primary_language") or {}).get("logo_text", "")
    provs = []
    logos = []
    for p in m.get("where_to_watch", [])[:4]:
        prov = p.get("provider", {})
        pn = prov.get("name")
        lg = prov.get("logo_url") or prov.get("icon_url")
        if lg:
            logos.append(lg)
        link = p.get("movie_url") or p.get("show_url")
        if not link and pn and p.get("partner_title_id"):
            ptid = p.get("partner_title_id")
            pl = pn.lower()
            if "netflix" in pl:
                link = f"https://www.netflix.com/title/{ptid}"
            elif "zee5" in pl:
                link = f"https://www.zee5.com/global/content/{ptid}"
        if link and not link.startswith("http"):
            link = None
        if pn:
            provs.append(f'<a href="{html.escape(link)}">{html.escape(pn)}</a>'
                         if link else html.escape(pn))
    tm = await tmdb_mod.get_movie(title, year)
    rating = (tm.get("rating") if tm else None) or "N/A"
    plot = (tm.get("plot") if tm else None) or "No description available."
    posters = m.get("posters", [])
    return {
        "title": title, "year": year, "type_str": type_str,
        "lang": lang, "providers": provs, "provider_logos": logos,
        "rating": rating, "plot": plot,
        "r_date": _fmt_date(_movie_date(m)),
        "poster": posters[0] if posters else (tm.get("poster_url")
                                              if tm else None),
        "backdrop": (tm.get("poster_url") if tm else None),
        "mid": str(m.get("_id", "")),
    }


async def _detail_card(client, chat_id, uid, d, edit_id=None,
                       notify=True):
    uname = await _username(client)
    msg = (f"🔜 <b>{html.escape(d['title'])}</b> · {d['year'] or ''} · "
           f"<i>{d['type_str']}</i>\n\n")
    if d["lang"]:
        msg += f"🉑 #{d['lang'].replace(' ', '')}\n"
    if d["providers"]:
        msg += f"📺 {', '.join(d['providers'][:3])}\n"
    msg += f"📅 {html.escape(d['r_date'])}\n⭐ {html.escape(str(d['rating']))}\n\n"
    msg += f"<i>{html.escape(d['plot'][:220])}</i>"

    kb_rows = []
    if uname:
        kb_rows.append([InlineKeyboardButton(
            "🔍 Search this title",
            url=search_deep_link(uname, d["title"]))])
    if notify:
        kb_rows.append([InlineKeyboardButton(
            "🔔 Notify me", callback_data=f"up:n:{d['mid']}")])
    kb_rows.append([InlineKeyboardButton("❌ Close",
                                         callback_data="up:x")])
    kb = InlineKeyboardMarkup(kb_rows)

    card = None
    if d["poster"]:
        try:
            card = await generate_status_image(
                d["backdrop"], d["poster"], d["provider_logos"],
                d["title"], d["year"], d["rating"], "",
                d["plot"], is_upcoming=True, release_date=d["r_date"])
        except Exception as exc:  # noqa: BLE001
            log.debug("card gen failed: %s", exc)
    try:
        if edit_id:
            await client.delete_messages(chat_id, edit_id)
    except Exception:  # noqa: BLE001
        pass
    if card:
        return await client.send_photo(chat_id, photo=card, caption=msg,
                                       reply_markup=kb,
                                       parse_mode=ParseMode.HTML)
    if d["poster"]:
        return await client.send_photo(chat_id, photo=d["poster"],
                                       caption=msg, reply_markup=kb,
                                       parse_mode=ParseMode.HTML)
    return await client.send_message(chat_id, msg, reply_markup=kb,
                                     parse_mode=ParseMode.HTML,
                                     disable_web_page_preview=True)


async def _list_releases(client, message, movies, heading, prefix):
    _prune(_UP_DETAIL)
    rows = []
    for m in movies[:25]:
        t = clean_text(m.get("name", "Untitled"))[:34]
        ds = _fmt_date(_movie_date(m))
        mid = str(m.get("_id", ""))
        rows.append([InlineKeyboardButton(
            f"{t} ({ds})"[:60], callback_data=f"{prefix}:d:{mid}")])
    rows.append([InlineKeyboardButton("❌ Close",
                                      callback_data=f"{prefix}:x")])
    await message.edit_text(
        f"{heading}\n<i>Tap a title for details:</i>",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML)


@Client.on_message(filters.command("upcoming"))
async def upcoming_cmd(client: Client, message):
    await track_user(message)
    if message.from_user and await is_banned(message.from_user.id):
        return
    wait = await message.reply_text("🗓 <i>Fetching upcoming releases…</i>",
                                    parse_mode=ParseMode.HTML)
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    future = (datetime.now() + timedelta(days=41)).strftime("%Y-%m-%d")
    movies = await _fetch_upcoming(tomorrow, future)
    if not movies:
        await wait.edit_text("🚫 No upcoming releases found.")
        return
    movies.sort(key=lambda m: _movie_date(m) or "9999")
    await _list_releases(client, wait, movies,
                         "🗓 <b>Upcoming OTT releases</b>", "up")


@Client.on_message(filters.command("today"))
async def today_cmd(client: Client, message):
    await track_user(message)
    if message.from_user and await is_banned(message.from_user.id):
        return
    wait = await message.reply_text("📅 <i>Checking today's releases…</i>",
                                    parse_mode=ParseMode.HTML)
    today = datetime.now().strftime("%Y-%m-%d")
    movies = await _fetch_upcoming(today, today, cache_key="today")
    if not movies:
        await wait.edit_text(
            "📅 <b>No OTT releases today.</b>\n"
            "<i>Try /upcoming for what's coming next.</i>",
            parse_mode=ParseMode.HTML)
        return
    await _list_releases(client, wait, movies,
                         "📅 <b>Releasing today</b>", "td")


async def _release_detail(client, query, prefix):
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        return
    mid = parts[2]
    uid = query.from_user.id
    key = f"{uid}:{mid}"
    _prune(_UP_DETAIL)
    d = _UP_DETAIL.get(key)
    if not d:
        # re-fetch the list (cached 30 min) and find the movie
        movies = _upcoming_cached("upcoming" if prefix == "up" else "today") or []
        m = next((x for x in movies if str(x.get("_id")) == mid), None)
        if not m:
            # fetch fresh for the right window
            if prefix == "up":
                tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                future = (datetime.now() + timedelta(days=41)).strftime("%Y-%m-%d")
                movies = await _fetch_upcoming(tomorrow, future,
                                               cache_key="upcoming")
            else:
                today = datetime.now().strftime("%Y-%m-%d")
                movies = await _fetch_upcoming(today, today,
                                               cache_key="today")
            m = next((x for x in movies if str(x.get("_id")) == mid), None)
        if not m:
            await query.answer("⚠️ Details expired — run the command again.",
                               show_alert=True)
            return
        await query.answer("⏳ Loading details…")
        d = await _process_movie(m)
        d["ts"] = time.time()
        _UP_DETAIL[key] = d
    else:
        await query.answer()
    await _detail_card(client, query.message.chat.id, uid, d,
                       edit_id=query.message.id)


@Client.on_callback_query(filters.regex(r"^up:"))
async def upcoming_cb(client: Client, query):
    data = query.data or ""
    if data == "up:x":
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass
        await query.answer()
        return
    if data.startswith("up:d:"):
        await _release_detail(client, query, "up")
        return
    if data.startswith("up:n:"):
        mid = data.split(":")[2]
        uid = query.from_user.id
        key = f"{uid}:{mid}"
        d = _UP_DETAIL.get(key)
        if not d:
            await query.answer("⚠️ Session expired.", show_alert=True)
            return
        tk = title_key(d["title"])
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            from sqlalchemy import select as _select
            exists = (await s.execute(
                _select(MovieAlert).where(
                    MovieAlert.user_id == uid,
                    MovieAlert.title_key == tk))).scalar_one_or_none()
            if exists:
                await query.answer("🔔 Already subscribed!",
                                   show_alert=True)
                return
            s.add(MovieAlert(user_id=uid, title_key=tk,
                             title=d["title"][:256]))
            await s.commit()
        await query.answer(f"✅ You'll be notified when "
                           f"“{d['title'][:30]}” drops!",
                           show_alert=True)


@Client.on_callback_query(filters.regex(r"^td:"))
async def today_cb(client: Client, query):
    data = query.data or ""
    if data == "td:x":
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass
        await query.answer()
        return
    if data.startswith("td:d:"):
        # reuse the upcoming detail machinery (no notify button dup)
        query.data = "up:d:" + data.split(":")[2]
        await _release_detail(client, query, "td")


# ------------------------------------------------- notify-me fan-out ----

async def notify_release_drop(title: str):
    """DM users who tapped 🔔 Notify me when this title gets indexed.

    Called from the release announcer after a title is announced.
    Sends a search deep link (user picks their file) — best-effort,
    never breaks announcing.
    """
    try:
        from sqlalchemy import select as _select
        tk = title_key(title)
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            rows = (await s.execute(
                _select(MovieAlert).where(
                    MovieAlert.title_key == tk))).scalars().all()
            uids = [r.user_id for r in rows]
            for r in rows:
                await s.delete(r)
            await s.commit()
        if not uids:
            return
        from app.bot import app as bot_app
        from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        client = bot_app.bot
        if not client:
            return
        uname = await _username(client)
        for uid in uids[:200]:  # sanity cap
            try:
                kb = None
                if uname:
                    kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🔍 Search now",
                            url=search_deep_link(uname, title))]])
                await client.send_message(
                    uid,
                    f"🔔 <b>{html.escape(title)}</b> just dropped on "
                    f"MooviDex! 🎬",
                    reply_markup=kb, parse_mode=ParseMode.HTML)
                await asyncio.sleep(0.4)
            except Exception as exc:  # noqa: BLE001
                log.debug("notify drop failed for %d: %s", uid, exc)
    except Exception as exc:  # noqa: BLE001
        log.debug("notify_release_drop failed: %s", exc)


def register(bot: Client) -> None:
    bot.on_message(filters.command("ott"))(ott_cmd)
    bot.on_message(filters.command("latest"))(latest_cmd)
    bot.on_message(filters.command("today"))(today_cmd)
    bot.on_message(filters.command("upcoming"))(upcoming_cmd)
