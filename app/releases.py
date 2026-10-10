"""New-release announcements (v10.15).

Simple and efficient: every ``SWEEP_HOURS``, pull trending /
now-playing / on-the-air titles from TMDB (three cheap REST calls),
check which ones the bot already has indexed (one indexed
``title_key`` lookup per shard, stops at first hit), and announce the
unannounced ones on the update channel with poster + details + a
search button.

Why TMDB and not JustWatch for the "what's new" list: JustWatch only
exposes new releases through complex GraphQL browse queries, while
TMDB gives trending/now-playing in one REST call each. The "📺
Available on" OTT line in the announcement still comes from
JustWatch (via ``enrich_title``).

Dedup lives in the ``announced_releases`` bot setting (JSON list of
title_keys, capped at 1000). No per-upload hook — the sweep covers
live posts, batch indexing and backfills uniformly.

Needs ``UPDATE_CHANNEL_ID`` on the env (numeric channel id; the bot
must be admin there to post).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from urllib.parse import quote_plus

from sqlalchemy import select

from app import runtime as rt
from app.config import settings
from app.models import File
from app.textutil import esc, title_key

log = logging.getLogger(__name__)

SWEEP_HOURS = 6
_MAX_RELEASES = 60
_ANNOUNCE_KEY = "announced_releases"
_ANNOUNCE_CAP = 1000
_POSTER_BASE = "https://image.tmdb.org/t/p/w500"


# ---------- TMDB "what's new" ----------

async def fetch_new_releases() -> list[dict]:
    """Trending + now playing + on the air from TMDB.

    Returns [{title, year, kind, poster, overview, rating}].
    Empty list when TMDB is unconfigured or unreachable. Never raises.
    """
    if not settings.TMDB_API_KEY:
        return []
    from app.tmdb import _get_client

    out: list[dict] = []
    seen: set[str] = set()
    client = _get_client()

    async def _get(path: str, **params) -> list[dict]:
        try:
            p = {"api_key": settings.TMDB_API_KEY, "language": "en-US",
                 "page": 1, **params}
            r = await client.get(path, params=p)
            r.raise_for_status()
            return r.json().get("results") or []
        except Exception as exc:  # noqa: BLE001
            log.debug("releases TMDB %s failed: %s", path, exc)
            return []

    # Trending across movies + TV this week.
    for m in await _get("/trending/all/week"):
        _add(out, seen, m, m.get("media_type"))
    # In cinemas now (India).
    for m in await _get("/movie/now_playing", region="IN"):
        _add(out, seen, m, "movie")
    # Series currently airing.
    for m in await _get("/tv/on_the_air"):
        _add(out, seen, m, "tv")
    return out[:_MAX_RELEASES]


def _add(out: list[dict], seen: set[str], m: dict, media_type: str | None):
    title = (m.get("title") or m.get("name") or "").strip()
    if not title:
        return
    key = title_key(title)
    if not key or key in seen:
        return
    seen.add(key)
    date = m.get("release_date") or m.get("first_air_date") or ""
    try:
        year = int(str(date)[:4]) if str(date)[:4].isdigit() else None
    except (TypeError, ValueError):
        year = None
    poster = m.get("poster_path") or ""
    out.append({
        "key": key,
        "title": title,
        "year": year,
        "kind": "series" if media_type == "tv" else "movie",
        "poster": f"{_POSTER_BASE}{poster}" if poster else "",
        "overview": (m.get("overview") or "").strip(),
        "rating": m.get("vote_average") or 0,
    })


# ---------- DB: do we have it indexed? ----------

async def has_files(key: str) -> bool:
    """True if any shard has a file with this title_key. Never raises."""
    try:
        from app.db_shard import get_shard_factories
        for factory in get_shard_factories():
            async with factory() as s:
                row = (await s.execute(
                    select(File.id).where(File.title_key == key).limit(1)
                )).first()
                if row:
                    return True
    except Exception as exc:  # noqa: BLE001
        log.debug("releases has_files failed: %s", exc)
    return False


# ---------- dedup ----------

def _announced() -> list[str]:
    try:
        raw = rt.get_setting(_ANNOUNCE_KEY) or ""
        data = json.loads(raw) if raw else []
        return list(data) if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


async def _mark_announced(key: str) -> None:
    keys = _announced()
    if key not in keys:
        keys.append(key)
    for stale in keys[:-_ANNOUNCE_CAP]:
        keys.remove(stale)
    try:
        await rt.set_setting(_ANNOUNCE_KEY, json.dumps(keys))
    except Exception as exc:  # noqa: BLE001
        log.warning("releases: could not persist announced list: %s", exc)


# ---------- announcement post ----------

def search_deep_link(bot_username: str, title: str) -> str:
    """t.me link that opens the bot and runs a search for the title."""
    tok = base64.urlsafe_b64encode(title.encode()).decode().rstrip("=")
    return f"https://t.me/{bot_username}?start=srch_{tok}"


async def announce(client, rel: dict) -> bool:
    """Post one release to the update channel. Returns True on success."""
    channel = (settings.UPDATE_CHANNEL_ID or 0)
    if not channel:
        log.warning("releases: UPDATE_CHANNEL_ID not set — skipping announce")
        return False
    try:
        me = await client.get_me()
        username = me.username or ""
    except Exception:  # noqa: BLE001
        username = ""
    if not username:
        log.warning("releases: could not resolve bot username")
        return False

    # OTT "available on" via the JustWatch-backed enrich (cached).
    ott_line = ""
    try:
        from app import enrich as enrich_mod
        meta = await enrich_mod.enrich_title(rel["title"], rel.get("year"))
        ott = (meta or {}).get("ott") or []
        if ott:
            ott_line = f"\n📺 <i>Available on: {esc(', '.join(ott[:2]))}</i>"
    except Exception as exc:  # noqa: BLE001
        log.debug("releases enrich failed: %s", exc)

    kind_icon = "📺" if rel["kind"] == "series" else "🎬"
    head = f"{kind_icon} <b>{esc(rel['title'])}</b>"
    if rel.get("year"):
        head += f" ({rel['year']})"
    lines = [head]
    if rel.get("rating"):
        lines.append(f"⭐ <b>{rel['rating']:.1f}</b> / 10")
    if rel.get("overview"):
        ov = rel["overview"]
        lines.append(f"<i>{esc(ov[:220] + '…' if len(ov) > 220 else ov)}</i>")
    if ott_line:
        lines.append(ott_line)
    lines.append("\n✅ <b>Now available in the bot!</b>")
    caption = "\n".join(lines)

    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔍 Search in Bot",
                             url=search_deep_link(username, rel["title"]))]])
    try:
        if rel.get("poster"):
            await client.send_photo(channel, rel["poster"], caption=caption,
                                    reply_markup=kb)
        else:
            await client.send_message(channel, caption, reply_markup=kb)
        log.info("releases: announced %r", rel["title"])
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("releases: announce failed for %r: %s",
                    rel["title"], exc)
        return False


# ---------- sweep ----------

async def sweep(client) -> int:
    """One announce cycle. Returns the number of new announcements."""
    if not (settings.UPDATE_CHANNEL_ID or 0):
        log.debug("releases: UPDATE_CHANNEL_ID unset — sweep skipped")
        return 0
    releases = await fetch_new_releases()
    if not releases:
        return 0
    announced = set(_announced())
    n = 0
    for rel in releases:
        if rel["key"] in announced:
            continue
        if not await has_files(rel["key"]):
            continue
        if await announce(client, rel):
            await _mark_announced(rel["key"])
            announced.add(rel["key"])
            n += 1
    if n:
        log.info("releases: sweep announced %d new title(s)", n)
    return n


async def _loop(client) -> None:
    await asyncio.sleep(120)  # let startup settle
    while True:
        try:
            await sweep(client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("releases sweep failed: %s", exc)
        await asyncio.sleep(SWEEP_HOURS * 3600)


def start(client):
    """Start the background sweep task. Returns the task."""
    log.info("releases: starting sweep every %dh", SWEEP_HOURS)
    return asyncio.create_task(_loop(client), name="releases-sweep")
