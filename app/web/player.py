"""Web player: /watch page + /dl ranged streaming via MTProto.

Unlike Bot API downloads (20 MB cap), MTProto ``upload.GetFile`` streams
files of ANY size with precise byte offsets, so seeking works and 4 GB
movies play fine.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pyrogram.errors import FileReferenceExpired
from sqlalchemy import select, update

from app import streamer
from app import tmdb
from app.bot import app as bot_app
from app.config import settings
from app.db import get_session_factory
from app.indexer import _media_of
from app.models import File
from app.web.tokens import parse_watch_token

log = logging.getLogger(__name__)
router = APIRouter()

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_stream_sem = asyncio.Semaphore(20)

_WATCH_HTML = os.path.join(os.path.dirname(__file__), "watch.html")
_watch_template: str | None = None


def _template() -> str:
    global _watch_template
    if _watch_template is None:
        with open(_WATCH_HTML, encoding="utf-8") as fh:
            _watch_template = fh.read()
    return _watch_template


async def _get_file(token: str) -> File:
    data = parse_watch_token(token)
    if not data:
        raise HTTPException(403, "invalid or expired link")
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        f = (await session.execute(
            select(File).where(File.id == data["f"]))).scalar_one_or_none()
    if not f:
        raise HTTPException(404, "file not found")
    return f


def _content_type(f: File) -> str:
    if f.mime_type:
        return f.mime_type
    guess, _ = mimetypes.guess_type(f.file_name or "")
    return guess or "application/octet-stream"


def _parse_range(range_header: str | None, size: int) -> tuple[int, int | None]:
    """Return (offset, length). length None = to end."""
    if not range_header:
        return 0, None
    m = _RANGE_RE.match(range_header.strip())
    if not m:
        return 0, None
    start_s, end_s = m.groups()
    if start_s == "" and end_s:
        # suffix range: last N bytes
        n = int(end_s)
        return max(0, size - n), n
    start = int(start_s or 0)
    if end_s:
        end = min(int(end_s), size - 1)
        if start > end:
            raise HTTPException(416, "range not satisfiable")
        return start, end - start + 1
    return start, None


async def _refresh_file_id(f: File) -> str:
    """Re-fetch the source message to repair an expired file_reference."""
    client = bot_app.bot
    if not client or not f.channel_id or not f.message_id:
        raise FileReferenceExpired("no source to refresh from")
    msg = await client.get_messages(f.channel_id, f.message_id)
    media, _ = _media_of(msg)
    new_id = getattr(media, "file_id", None) if media else None
    if not new_id:
        raise FileReferenceExpired("source message has no media")
    if new_id != f.file_id:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            await session.execute(
                update(File).where(File.id == f.id).values(file_id=new_id))
            await session.commit()
        log.info("refreshed expired file_reference for file %d", f.id)
    return new_id


@router.get("/watch/{token}", response_class=HTMLResponse)
async def watch(token: str, request: Request):
    f = await _get_file(token)
    base = str(request.base_url).rstrip("/")
    dl_url = f"{base}/dl/{token}"
    name = (f.file_name or "Video").rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = _content_type(f)
    playable = mime.startswith(("video/", "audio/")) or \
        name.lower().endswith((".mp4", ".webm", ".mov", ".m4v",
                               ".mp3", ".ogg", ".wav", ".m4a"))
    # TMDB enrichment for poster/plot (best-effort, cached 30 days).
    meta: dict | None = None
    try:
        guess = _title_guess(name)
        if guess:
            meta = await tmdb.get_movie(guess)
    except Exception:  # noqa: BLE001
        meta = None
    poster = (meta or {}).get("poster_url") or ""
    plot = (meta or {}).get("plot") or ""
    rating = (meta or {}).get("rating") or 0

    if poster:
        poster_html = (f'<img class="poster" src="{escape(poster)}" '
                       f'alt="poster" loading="lazy">')
        backdrop = f"url('{escape(poster)}') center/cover no-repeat"
    else:
        poster_html = '<div class="poster poster-fallback">🎬</div>'
        backdrop = "linear-gradient(135deg,#1a2233,#0b0e14)"

    chips = []
    if f.quality:
        chips.append(f'<span class="chip hot">{escape(f.quality)}</span>')
    if f.language:
        chips.append(f'<span class="chip">{escape(f.language)}</span>')
    if rating:
        chips.append(f'<span class="chip ok">⭐ {rating}</span>')
    if ext:
        chips.append(f'<span class="chip">.{escape(ext)}</span>')
    if playable:
        chips.append('<span class="chip ok">▶ playable</span>')

    def row(k: str, v: str) -> str:
        return (f'<div class="drow"><span class="k">{k}</span>'
                f'<span class="v">{escape(v)}</span></div>')

    details = "".join([
        row("File name", name),
        row("Size", _fmt_size(f.file_size)),
        row("Format", ("." + ext) if ext else "—"),
        row("Type", mime),
        row("Quality", f.quality or "—"),
        row("Language", f.language or "—"),
    ])
    if meta and meta.get("title"):
        details += row("TMDB", f"{meta['title']}"
                             f" ({meta.get('year') or '—'})")

    html_page = _template().replace("__TITLE__", escape(name))
    # JS-string-safe filename (no double quotes / backslashes).
    js_name = name.replace("\\", "\\\\").replace('"', "")
    html_page = (html_page
                 .replace("__DL_URL__", dl_url)
                 .replace("__PLAYABLE__", "true" if playable else "false")
                 .replace("__SIZE__", str(f.file_size or 0))
                 .replace("__MIME__", escape(mime))
                 .replace("__EXT__", escape(ext))
                 .replace("__POSTER__", escape(poster))
                 .replace("__POSTER_HTML__", poster_html)
                 .replace("__CHIPS_HTML__", "".join(chips))
                 .replace("__PLOT__", escape(plot[:300]))
                 .replace("__BACKDROP_CSS__", backdrop)
                 .replace("__DETAILS_HTML__", details)
                 .replace("__FILENAME__", js_name))
    return HTMLResponse(html_page)


def _title_guess(name: str) -> str:
    """'KGF.Chapter.2.2022.1080p.mkv' -> 'KGF Chapter 2'."""
    base = name.rsplit(".", 1)[0] if "." in name else name
    base = re.sub(r"[\[\(].*?[\]\)]", " ", base)
    base = re.sub(r"\b(480p|720p|1080p|2160p|4k|hdrip|webrip|web-dl|bluray|"
                  r"hdtv|x264|x265|hevc|aac|ac3|dd5\.1|esub|subs)\b",
                  " ", base, flags=re.IGNORECASE)
    base = re.sub(r"[._]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip(" -")
    return base[:80]


def _fmt_size(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return ""


@router.api_route("/dl/{token}", methods=["GET", "HEAD"])
async def download(token: str, request: Request):
    f = await _get_file(token)
    client = bot_app.bot
    if not client:
        raise HTTPException(503, "bot not ready")
    size = f.file_size or 0
    if size <= 0:
        raise HTTPException(404, "unknown file size")

    try:
        offset, length = _parse_range(request.headers.get("range"), size)
    except HTTPException:
        return Response(status_code=416,
                        headers={"Content-Range": f"bytes */{size}"})
    if offset >= size:
        return Response(status_code=416,
                        headers={"Content-Range": f"bytes */{size}"})
    if length is None:
        length = size - offset
    length = min(length, size - offset)

    ctype = _content_type(f)
    filename = (f.file_name or "file").rsplit("/", 1)[-1]
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": ctype,
        "Content-Length": str(length),
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    partial = offset != 0 or length != size
    if partial:
        headers["Content-Range"] = f"bytes {offset}-{offset + length - 1}/{size}"

    if request.method == "HEAD":
        return Response(status_code=206 if partial else 200, headers=headers)

    file_id = f.file_id
    for attempt in (0, 1):
        try:
            async def gen():
                async with _stream_sem:
                    async for chunk in streamer.stream_file(
                            client, file_id, offset=offset, length=length):
                        yield chunk
            break
        except FileReferenceExpired:
            if attempt == 1:
                raise HTTPException(410, "file reference expired")
            log.info("file_reference expired for %d, refreshing", f.id)
            file_id = await _refresh_file_id(f)

    return StreamingResponse(gen(),
                             status_code=206 if partial else 200,
                             headers=headers)
