"""New-title announcements (v10.15.6).

Nova-Filter-Bot style ``send_update``: whenever a channel post with a
never-before-seen title is indexed live, post a "New Content Added"
card (TMDB poster + details) to the update channel, with a
"Search in Bot" deep-link button.

Event-driven from the live indexer — no sweep, no JustWatch. Dedup
lives in the ``announced_releases`` bot setting (``title_key:year``,
capped at 1000).

Needs ``UPDATE_CHANNEL_ID`` on the env (numeric channel id; the bot
must be admin there to post).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re

from app import runtime as rt
from app import tmdb as tmdb_mod
from app.bot.ui import esc
from app.config import settings
from app.textutil import title_key

log = logging.getLogger(__name__)

_ANNOUNCE_KEY = "announced_releases"
_ANNOUNCE_CAP = 1000

# Nova's NEW_ADDED_TEMPLATE, fields mapped to app.tmdb.get_movie().
_NEW_ADDED_TEMPLATE = """✅ <b>New Content Added</b>

<blockquote>🏷️ <b>Title</b>: {title}
🎭 <b>Genres</b>: {genres}
📆 <b>Year</b>: {year}
🌟 <b>Rating</b>: <code>{rating} / 10</code>
☀️ <b>Language</b>: {language}</blockquote>"""


# ---------- dedup (Nova's notified_media collection) ----------

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


def _dedup_key(title: str, year: int | None) -> str:
    return f"{title_key(title)}:{year or ''}"


# ---------- deep link ----------

def search_deep_link(bot_username: str, title: str) -> str:
    """t.me link that opens the bot and runs a search for the title."""
    tok = base64.urlsafe_b64encode(title.encode()).decode().rstrip("=")
    return f"https://t.me/{bot_username}?start=srch_{tok}"


# ---------- send_update (Nova logic, adapted) ----------

async def send_update(client, title: str, year: int | None = None,
                      language: str | None = None) -> bool:
    """Post one "new content added" card to the update channel.

    Mirrors Nova-Filter-Bot's send_update: TMDB poster + details card,
    plain-text fallback when TMDB has nothing. Returns True on success.
    """
    channel = (settings.UPDATE_CHANNEL_ID or 0)
    if not channel:
        return False
    try:
        me = await client.get_me()
        username = me.username or ""
    except Exception:  # noqa: BLE001
        username = ""

    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = None
    if username:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 Search in Bot",
                                 url=search_deep_link(username, title))]])

    meta: dict | None = None
    try:
        meta = await tmdb_mod.get_movie(title, year)
    except Exception as exc:  # noqa: BLE001
        log.debug("releases: tmdb lookup failed for %r: %s", title, exc)

    if not meta:
        # Nova fallback: plain "New Added" line, no details.
        text = f"✅ New Added ✅\n\n🏷️ Title: {esc(title.strip().title())}"
        if year:
            text += f" ({year})"
        try:
            await client.send_message(channel, text, reply_markup=kb)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("releases: send_update failed for %r: %s",
                        title, exc)
            return False

    disp_title = meta.get("title") or title
    disp_year = meta.get("year") or year or "N/A"
    genres = meta.get("genres") or []
    caption = _NEW_ADDED_TEMPLATE.format(
        title=esc(disp_title),
        genres=esc(", ".join(genres)) if genres else "N/A",
        year=esc(str(disp_year)),
        rating=meta.get("rating") or "N/A",
        language=esc((language or "N/A").title()),
    )
    plot = (meta.get("plot") or "").strip()
    if plot:
        caption += f"\n\n<i>{esc(plot[:300])}</i>"
    caption += "\n\n✅ <b>Now available in the bot!</b>"

    try:
        poster = meta.get("poster_url")
        if poster:
            await client.send_photo(channel, poster, caption=caption,
                                    reply_markup=kb)
        else:
            await client.send_message(channel, caption, reply_markup=kb)
        log.info("releases: announced %r", disp_title)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("releases: send_update failed for %r: %s", title, exc)
        return False


# ---------- trigger (Nova's trigger_update_if_new) ----------

async def trigger_update_if_new(client, title: str,
                                year: int | None = None,
                                language: str | None = None) -> None:
    """Announce once per new title+year. Fire-and-forget, never raises."""
    try:
        if not (settings.UPDATE_CHANNEL_ID or 0):
            return
        title = (title or "").strip()
        if not title:
            return
        key = _dedup_key(title, year)
        if not key.strip(":") or key in _announced():
            return
        # Mark before sending (Nova does the insert first too) so a
        # concurrent duplicate can't double-post.
        await _mark_announced(key)
        asyncio.create_task(send_update(client, title, year, language),
                            name=f"send-update-{key[:32]}")
    except Exception as exc:  # noqa: BLE001
        log.debug("releases: trigger failed for %r: %s", title, exc)


def announce_title(client, file_name: str, language: str | None = None) -> None:
    """Convenience: derive clean title/year from a filename and trigger.

    Series announce once per show (SxxExx stripped), not per episode.
    Schedules the work; returns immediately, never raises.
    """
    try:
        from app.textutil import clean_title, extract_year
        base = clean_title(file_name)
        base = re.sub(r"\bs\d{1,2}e\d{1,3}\b", " ", base, flags=re.I)
        base = re.sub(r"\s\d$", "", base)  # stray digit from "AAC2.0" etc.
        base = re.sub(r"\s+", " ", base).strip()
        if not base:
            return
        year = extract_year(file_name)
        asyncio.create_task(
            trigger_update_if_new(client, base, year, language),
            name="trigger-update")
    except Exception as exc:  # noqa: BLE001
        log.debug("releases: announce_title failed: %s", exc)
