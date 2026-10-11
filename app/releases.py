"""New-title announcements (v10.15.8).

Nova-Filter-Bot style ``send_update``: whenever a channel post with a
never-before-seen title is indexed live, post a "New Content Added"
card to the update channel, with a "Search in Bot" deep-link button.

Robustness (v10.15.8):
- Details come from ``app.enrich.enrich_title`` — JustWatch first,
  TMDB only as fallback/emergency (per the bot's established pipeline).
- Dedup is Nova's ``notified_media`` pattern: one atomic
  ``INSERT ... ON CONFLICT DO NOTHING`` into ``notified_titles``
  (UNIQUE on imdb_id, and on title_key+year). No read-modify-write
  race, no duplicate posts for the same movie.
- The year is resolved via enrichment *before* the dedup key is built,
  so "Karma" (no year in filename) and "Karma 2024" map to one entry.

Event-driven from the live indexer — no sweep, no JustWatch polling.
Needs ``UPDATE_CHANNEL_ID`` on the env (numeric channel id; the bot
must be admin there to post).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import runtime as rt
from app.bot.ui import esc
from app.config import settings
from app.db import get_session_factory
from app.models import NotifiedTitle
from app.textutil import title_key

log = logging.getLogger(__name__)

_ANNOUNCE_KEY = "announced_releases"  # legacy v10.15.x setting (imported once)

# Nova's NEW_ADDED_TEMPLATE, fields from app.enrich.enrich_title().
_NEW_ADDED_TEMPLATE = """✅ <b>New Content Added</b>

<blockquote>🏷️ <b>Title</b>: {title}
🎭 <b>Genres</b>: {genres}
📆 <b>Year</b>: {year}
🌟 <b>Rating</b>: <code>{rating} / 10</code>
☀️ <b>Language</b>: {language}</blockquote>"""

_imported_legacy = False


# ---------- dedup (Nova's notified_media, atomic) ----------

async def _atomic_insert(tkey: str, year: int,
                         provider_id: str | None, title: str) -> bool:
    """Insert a notified title. True = it was new (announce it)."""
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as s:
            stmt = (pg_insert(NotifiedTitle)
                    .values(title_key=tkey, year=year,
                            provider_id=provider_id,
                            title=title[:200])
                    .on_conflict_do_nothing())
            res = await s.execute(stmt)
            await s.commit()
            return bool(res.rowcount)
    except Exception as exc:  # noqa: BLE001
        log.debug("releases: notified insert failed: %s", exc)
        return False


async def _import_legacy_once() -> None:
    """One-time: carry the v10.15.x announced_releases setting over so
    titles announced by the old code don't re-announce. Never raises."""
    global _imported_legacy
    if _imported_legacy:
        return
    _imported_legacy = True
    try:
        raw = rt.get_setting(_ANNOUNCE_KEY) or ""
        keys = json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001
        return
    if not isinstance(keys, list) or not keys:
        return
    factory = get_session_factory(settings.DATABASE_URL)
    n = 0
    try:
        async with factory() as s:
            for k in keys:
                if not isinstance(k, str) or not k.strip(":"):
                    continue
                tk, _, yr = k.rpartition(":")
                try:
                    y = int(yr) if yr else 0
                except ValueError:
                    y = 0
                stmt = (pg_insert(NotifiedTitle)
                        .values(title_key=(tk or k).strip() or k,
                                year=y, provider_id=None,
                                title=(tk or k).strip() or k)
                        .on_conflict_do_nothing())
                res = await s.execute(stmt)
                n += res.rowcount or 0
            await s.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("releases: legacy import failed: %s", exc)
        return
    if n:
        log.info("releases: imported %d legacy announced titles", n)


# ---------- deep link ----------

def search_deep_link(bot_username: str, title: str) -> str:
    """t.me link that opens the bot and runs a search for the title."""
    tok = base64.urlsafe_b64encode(title.encode()).decode().rstrip("=")
    return f"https://t.me/{bot_username}?start=srch_{tok}"


# ---------- send_update (Nova logic, JW primary / TMDB fallback) ----------

async def send_update(client, title: str, year: int | None = None,
                      language: str | None = None,
                      meta: dict | None = None) -> bool:
    """Post one "new content added" card to the update channel.

    ``meta`` is an ``app.enrich.enrich_title`` dict (JustWatch primary,
    TMDB fallback). Without it, sends Nova's plain-text fallback.
    Returns True on success.
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
    ott = meta.get("ott") or []
    if ott:
        ott_url = (meta.get("ott_url") or "").strip()
        if ott_url:
            links = ", ".join(
                f'<a href="{esc(ott_url)}">{esc(p)}</a>' for p in ott)
        else:
            links = esc(", ".join(ott))
        caption += f"\n\n📺 <i>Available on: {links}</i>"
    caption += "\n\n✅ <b>Now available in the bot!</b>"

    try:
        poster = meta.get("poster_url") or meta.get("backdrop_url")
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


# ---------- trigger (Nova's trigger_update_if_new, hardened) ----------

async def trigger_update_if_new(client, title: str,
                                year: int | None = None,
                                language: str | None = None) -> None:
    """Announce once per movie. Fire-and-forget, never raises.

    Enrichment (JustWatch → TMDB fallback) runs *before* the dedup so
    the key is canonical: filename variants and year-less names resolve
    to one entry, and the atomic insert kills races.
    """
    try:
        if not (settings.UPDATE_CHANNEL_ID or 0):
            return
        title = (title or "").strip()
        if not title:
            return
        await _import_legacy_once()

        # 1. canonical identity via enrichment (JW primary, TMDB fallback)
        from app import enrich as enrich_mod
        try:
            meta = await enrich_mod.enrich_title(title, year)
        except Exception as exc:  # noqa: BLE001
            log.debug("releases: enrich failed for %r: %s", title, exc)
            meta = None

        if meta and (meta.get("title") or "").strip():
            ctitle = meta["title"].strip()
            cyear = int(meta.get("year") or year or 0)
            provider_id = meta.get("imdb_id") or None
        else:
            ctitle, cyear, provider_id = title, int(year or 0), None
        tkey = title_key(ctitle)
        if not tkey:
            return

        # 2. atomic "is it new?" (Nova's notified_media insert)
        if not await _atomic_insert(tkey, cyear, provider_id, ctitle):
            return  # duplicate — already announced

        # 3. send the card in the background
        asyncio.create_task(
            send_update(client, ctitle, cyear or None, language, meta),
            name=f"send-update-{tkey[:32]}")
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
