"""New-release announcements (v10.15.9).

When a NEWLY RELEASED movie/series file is indexed live, post a simple
card to the update channel: landscape backdrop + key details (no blank
fields), plus a Search button (movie name + year).

Rules:
- Only recent releases (this year or last year) are announced — old
  movies indexed now never spam the channel.
- Details via ``app.enrich.enrich_title`` (JustWatch primary, TMDB
  fallback). When no details exist, a simple title/year line is sent.
- Dedup is Nova's ``notified_media`` pattern: one atomic
  ``INSERT ... ON CONFLICT DO NOTHING`` into ``notified_titles``.

Needs ``UPDATE_CHANNEL_ID`` on the env (numeric channel id; the bot
must be admin there to post).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import runtime as rt
from app.bot.ui import esc
from app.config import settings
from app.db import get_session_factory
from app.models import NotifiedTitle
from app.textutil import title_key

log = logging.getLogger(__name__)

_ANNOUNCE_KEY = "announced_releases"  # legacy v10.15.x setting (imported once)

# Only this year / last year get announced.
_RECENT_YEARS = 1

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

def search_deep_link(bot_username: str, query: str) -> str:
    """t.me link that opens the bot and runs a search for the query."""
    tok = base64.urlsafe_b64encode(query.encode()).decode().rstrip("=")
    return f"https://t.me/{bot_username}?start=srch_{tok}"


# ---------- card ----------

def _build_caption(title: str, year: int | None,
                   language: str | None, meta: dict | None) -> str:
    """Simple card, no blank fields — a line is added only when the
    data exists."""
    lines = ["✅ <b>New Added</b>", ""]
    head = f"🎬 <b>{esc(title)}</b>"
    if year:
        head += f" ({year})"
    lines.append(head)

    bits: list[str] = []
    if meta and meta.get("rating"):
        bits.append(f"⭐ <b>{meta['rating']}</b>/10")
    genres = (meta.get("genres") if meta else None) or []
    if genres:
        bits.append(f"🎭 {esc(', '.join(genres))}")
    if language:
        bits.append(f"☀️ {esc(language.title())}")
    if bits:
        lines.append(" · ".join(bits))

    plot = ((meta.get("plot") if meta else None) or "").strip()
    if plot:
        lines.append(f"<i>{esc(plot[:200])}</i>")
    return "\n".join(lines)


async def send_update(client, title: str, year: int | None = None,
                      language: str | None = None,
                      meta: dict | None = None) -> bool:
    """Post one new-release card. Landscape backdrop preferred.
    Returns True on success."""
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
        query = f"{title} {year}".strip() if year else title
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 Search",
                                 url=search_deep_link(username, query))]])

    caption = _build_caption(title, year, language, meta)
    # Landscape first (backdrop), portrait poster as fallback.
    image = (meta.get("backdrop_url") if meta else None) or \
            (meta.get("poster_url") if meta else None)
    try:
        if image:
            await client.send_photo(channel, image, caption=caption,
                                    reply_markup=kb)
        else:
            await client.send_message(channel, caption, reply_markup=kb)
        log.info("releases: announced %r", title)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("releases: send_update failed for %r: %s", title, exc)
        return False


# ---------- trigger ----------

async def trigger_update_if_new(client, title: str,
                                year: int | None = None,
                                language: str | None = None) -> None:
    """Announce a newly indexed file — but only if it's a RECENT
    release (this year / last year). Fire-and-forget, never raises."""
    try:
        if not (settings.UPDATE_CHANNEL_ID or 0):
            return
        # v10.15.10: admin on/off toggle (default ON).
        try:
            if not await rt.aget_setting("NEW_RELEASE_UPDATES"):
                return
        except Exception:  # noqa: BLE001
            pass
        title = (title or "").strip()
        if not title:
            return
        await _import_legacy_once()

        # 1. canonical details (JustWatch primary, TMDB fallback)
        from app import enrich as enrich_mod
        try:
            meta = await enrich_mod.enrich_title(title, year)
        except Exception as exc:  # noqa: BLE001
            log.debug("releases: enrich failed for %r: %s", title, exc)
            meta = None

        # 2. recent-release gate — old movies indexed now stay silent
        cur = datetime.now().year
        eff_year = (meta.get("year") if meta else None) or year
        if not eff_year or eff_year < cur - _RECENT_YEARS:
            return

        if meta and (meta.get("title") or "").strip():
            ctitle = meta["title"].strip()
            provider_id = meta.get("imdb_id") or None
        else:
            ctitle, provider_id = title, None
        tkey = title_key(ctitle)
        if not tkey:
            return

        # 3. atomic "is it new?" (Nova's notified_media insert)
        if not await _atomic_insert(tkey, int(eff_year), provider_id,
                                    ctitle):
            return  # duplicate — already announced

        # 4. send the card in the background
        asyncio.create_task(
            send_update(client, ctitle, int(eff_year), language, meta),
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
