"""New-release announcements (v10.15).

Simple and efficient: every ``SWEEP_HOURS``, pull currently-popular
recent titles from JustWatch India, keep the ones actually streaming
on subscription OTT, check which ones the bot already has indexed
(one indexed ``title_key`` lookup per shard, stops at first hit), and
announce the unannounced ones on the update channel with poster +
details + a search button.

Why JustWatch (not TMDB) for the "what's new" list: TMDB trending is
popularity-based and its OTT data is patchy; JustWatch IS the OTT
authority — titles, streaming offers and title URLs all come from one
response.

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
from datetime import datetime

from sqlalchemy import select

from app import runtime as rt
from app.config import settings
from app.enrich import _extract_offers, _format_ott
from app.models import File
from app.textutil import esc, title_key

log = logging.getLogger(__name__)

SWEEP_HOURS = 6
_MAX_RELEASES = 60
_ANNOUNCE_KEY = "announced_releases"
_ANNOUNCE_CAP = 1000


# ---------- JustWatch: latest OTT releases ----------

def _jw_popular_sync(count: int, min_year: int) -> list:
    """Blocking JustWatch popular-titles fetch (runs in a thread)."""
    from simplejustwatchapi.justwatch import popular as jw_popular
    return jw_popular(country="IN", language="en", count=count,
                      best_only=False, min_release_year=min_year)


async def fetch_new_releases() -> list[dict]:
    """Latest OTT releases via JustWatch India.

    The library has no "new releases" sort, so we take
    currently-popular titles from the last two release years and keep
    only ones with a subscription (FLATRATE) streaming offer — that is
    exactly "latest released on OTT". Never raises.
    """
    min_year = datetime.now().year - 1
    try:
        entries = await asyncio.to_thread(_jw_popular_sync,
                                          _MAX_RELEASES, min_year)
    except Exception as exc:  # noqa: BLE001
        log.warning("releases: justwatch popular failed: %s", exc)
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for e in entries or []:
        title = (e.title or "").strip()
        if not title:
            continue
        key = title_key(title)
        if not key or key in seen:
            continue
        # OTT-only: must be streaming on subscription in India.
        offers = _extract_offers(e)
        if not any(o["type"] == "FLATRATE" for o in offers):
            continue
        seen.add(key)
        scoring = getattr(e, "scoring", None)
        rating = 0
        if scoring is not None:
            rating = (getattr(scoring, "imdb_score", None)
                      or getattr(scoring, "tmdb_score", None) or 0)
        jw_url = (getattr(e, "url", "") or "").strip()
        if not jw_url.startswith("http"):
            from urllib.parse import quote_plus
            jw_url = ("https://www.justwatch.com/in/search?q="
                      + quote_plus(title))
        backdrops = list(getattr(e, "backdrops", None) or [])
        out.append({
            "key": key,
            "title": title,
            "year": e.release_year,
            "kind": ("series"
                     if (e.object_type or "").upper() == "SHOW" else "movie"),
            "poster": e.poster or "",
            "backdrop": backdrops[-1] if backdrops else "",
            "overview": (getattr(e, "short_description", "") or "").strip(),
            "rating": rating,
            "ott": _format_ott(offers)[:2],
            "ott_url": jw_url,
        })
    return out


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
    ott = rel.get("ott") or []
    if ott:
        ott_url = rel.get("ott_url") or ""
        links = ", ".join(
            f'<a href="{esc(ott_url)}">{esc(p)}</a>' for p in ott)
        lines.append(f"\n📺 <i>Available on: {links}</i>")
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
