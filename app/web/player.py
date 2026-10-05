"""Web player: /watch page + /dl ranged streaming via MTProto.

Unlike Bot API downloads (20 MB cap), MTProto ``upload.GetFile`` streams
files of ANY size with precise byte offsets, so seeking works and 4 GB
movies play fine.

Robustness notes (v10.11):
  * single ranges only (multipart -> 416), suffix ranges supported
  * ETag + If-None-Match (304) + If-Range (stale range -> full 200)
  * transient Telegram stalls resume mid-stream from the last byte sent
    (up to 3 resumes; FileReferenceExpired refreshes the file_id first)
  * FileReferenceExpired on the first chunk refreshes from the source
    message and retries once, then 410
  * /watch failures render a styled error page instead of a bare status
  * X-Content-Type-Options: nosniff on every response
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pyrogram.errors import FileReferenceExpired
from sqlalchemy import select, update

from app import streamer
from app import tmdb
from app.bot import app as bot_app
from app.config import settings
from app.db import bump_file_downloads, get_session_factory
from app.indexer import _media_of
from app.models import File
from app.web.tokens import parse_watch_token

log = logging.getLogger(__name__)
router = APIRouter()

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_stream_sem = asyncio.Semaphore(20)
# errors worth resuming mid-stream (transient network/Telegram stalls)
_RESUMEABLE = (streamer.StreamError, asyncio.TimeoutError,
               ConnectionError, OSError)
_MAX_RESUMES = 3
_PREFETCH_TIMEOUT = 45.0

_WATCH_HTML = os.path.join(os.path.dirname(__file__), "watch.html")
_watch_template: str | None = None

_LOGO_URL = "https://h21web.github.io/cdn/assets/moovidex-logo.png"

_bot_username: str | None = None
_bot_username_failed = False


def _template() -> str:
    global _watch_template
    if _watch_template is None:
        with open(_WATCH_HTML, encoding="utf-8") as fh:
            _watch_template = fh.read()
    return _watch_template


async def _bot_username() -> str | None:
    """Cached bot username for the 'Go To Bot' button (None = unknown)."""
    global _bot_username, _bot_username_failed
    if _bot_username or _bot_username_failed:
        return _bot_username
    client = bot_app.bot
    if not client:
        return None
    try:
        me = await client.get_me()
        _bot_username = (getattr(me, "username", "") or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        log.debug("get_me failed: %s", exc)
        _bot_username_failed = True
    return _bot_username


def _error_page(status: int, title: str, message: str) -> HTMLResponse:
    """Styled MooviDex error page (self-contained, no external assets)."""
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{status} — MooviDex</title>
<style>
body{{margin:0;background:#0d0716;color:#f4f1fa;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:20px}}
.card{{background:#1a1029;border:1px solid #2c1d47;border-radius:20px;padding:38px 30px;max-width:380px;box-shadow:0 14px 44px rgba(0,0,0,.45)}}
.code{{font-size:52px;font-weight:900;color:#8b2ff7;margin-bottom:6px}}
h1{{font-size:18px;margin:0 0 10px}}p{{font-size:13.5px;color:#a89cc4;line-height:1.6;margin:0}}
</style></head><body><div class="card"><div class="code">{status}</div>
<h1>{escape(title)}</h1><p>{escape(message)}</p></div></body></html>"""
    return HTMLResponse(html, status_code=status)


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


def _content_disposition(filename: str, attachment: bool = False) -> str:
    """RFC 6266/5987 Content-Disposition.

    Sanitised ASCII ``filename`` (quotes/CR/LF stripped — no header
    injection) plus the UTF-8 ``filename*`` for non-ASCII names.

    Explicit downloads (``?dl=1``) are served as ``attachment`` so the
    browser downloads the file instead of trying to play a multi-GB
    stream inline — that inline attempt is what made the ⬇ Download
    button look dead.
    """
    safe = (filename or "file").rsplit("/", 1)[-1]
    safe = safe.replace('"', "").replace("\r", "").replace("\n", "")
    ascii_name = safe.encode("ascii", "ignore").decode("ascii") or "file"
    disp = "attachment" if attachment else "inline"
    return (f'{disp}; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(safe)}")


def _etag(f: File, size: int) -> str:
    """Stable validator for a file (survives file_reference refreshes)."""
    return f'"{f.id}-{size}"'


def _parse_range(range_header: str | None, size: int) -> tuple[int, int | None]:
    """Return (offset, length). length None = to end."""
    if not range_header:
        return 0, None
    m = _RANGE_RE.match(range_header.strip())
    if not m:
        return 0, None
    start_s, end_s = m.groups()
    try:
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
    except (ValueError, OverflowError):
        # Absurdly long digit runs trip the int() digit limit (or other
        # garbage): ignore the Range header instead of 500ing.
        log.debug("ignoring malformed Range header %r", range_header[:60])
        return 0, None


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


async def _open_stream(client, file_id: str, offset: int, length: int):
    """Create a stream with the first chunk pre-fetched (inside try).

    The async generator body only runs once iterated, so pre-fetching
    here lets the caller catch FileReferenceExpired / stalls before
    the HTTP response starts.
    """
    stream = streamer.stream_file(client, file_id,
                                  offset=offset, length=length)
    try:
        first = await asyncio.wait_for(stream.__anext__(),
                                       timeout=_PREFETCH_TIMEOUT)
    except StopAsyncIteration:
        first = None
    return stream, first


@router.get("/watch/{token}", response_class=HTMLResponse)
async def watch(token: str, request: Request):
    try:
        f = await _get_file(token)
    except HTTPException as exc:
        if exc.status_code == 403:
            return _error_page(403, "Link expired",
                               "This watch link is invalid or has expired. "
                               "Ask the bot for the file again to get a fresh link.")
        return _error_page(404, "File not found",
                           "This file is no longer indexed. "
                           "It may have been removed from the channel.")
    base = str(request.base_url).rstrip("/")
    dl_url = f"{base}/dl/{token}"
    name = (f.file_name or "Video").rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = _content_type(f)
    playable = mime.startswith(("video/", "audio/")) or \
        name.lower().endswith((".mp4", ".webm", ".mov", ".m4v",
                               ".mp3", ".ogg", ".wav", ".m4a"))
    # TMDB enrichment for the poster (best-effort, cached 30 days).
    meta: dict | None = None
    try:
        guess = _title_guess(name)
        if guess:
            meta = await tmdb.get_movie(guess)
    except Exception:  # noqa: BLE001
        meta = None
    poster = (meta or {}).get("poster_url") or ""

    # File details — DB values first, filename detection as fallback.
    from app.textutil import detect_quality_language, extract_year
    det_q, det_l = detect_quality_language(name)
    quality = f.quality or det_q or "—"
    language = f.language or det_l or "—"
    year = extract_year(name)
    year_s = str(year) if year else "—"
    fmt = ("." + ext) if ext else "—"

    username = await _bot_username()
    bot_url = f"https://telegram.me/{username}" if username else "#"

    html_page = _template().replace("__TITLE__", escape(name))
    # JS-string-safe filename: a full JSON string literal is valid JS.
    js_name = json.dumps(name.replace("\r", "").replace("\n", ""))
    html_page = (html_page
                 .replace("__LOGO_URL__", _LOGO_URL)
                 .replace("__BOT_URL__", bot_url)
                 .replace("__DL_URL__", dl_url)
                 .replace("__DL_DL_URL__", dl_url + "?dl=1")
                 .replace("__PLAYABLE__", "true" if playable else "false")
                 .replace("__SIZE__", str(f.file_size or 0))
                 .replace("__SIZE_H__", _fmt_size(f.file_size))
                 .replace("__MIME__", escape(mime))
                 .replace("__QUALITY__", escape(quality))
                 .replace("__YEAR__", escape(year_s))
                 .replace("__LANGUAGE__", escape(language))
                 .replace("__FORMAT__", escape(fmt))
                 .replace("__POSTER__", escape(poster))
                 .replace("__FILENAME_JSON__", js_name))
    return HTMLResponse(html_page,
                        headers={"Cache-Control": "no-store",
                                 "X-Content-Type-Options": "nosniff"})


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
    try:
        f = await _get_file(token)
    except HTTPException as exc:
        # video players / download managers need real status codes
        raise exc
    client = bot_app.bot
    if not client:
        raise HTTPException(503, "bot not ready")
    # Explicit ⬇ Download hits (page button uses ?dl=1) count toward the
    # per-file download counter. Web-player streams carry no marker, so
    # plays are never counted as downloads. HEAD/range-resumes don't count.
    is_explicit_dl = (request.method == "GET"
                      and request.query_params.get("dl") == "1"
                      and not request.headers.get("range"))
    if is_explicit_dl:
        asyncio.create_task(bump_file_downloads(f.id))
    size = f.file_size or 0
    if size <= 0:
        raise HTTPException(404, "unknown file size")

    etag = _etag(f, size)
    range_header = request.headers.get("range")

    # If-None-Match: full GET only.
    if (request.method == "GET" and not range_header
            and request.headers.get("if-none-match") == etag):
        return Response(status_code=304, headers={"ETag": etag})

    # If-Range with a stale validator -> ignore the Range, send all.
    if (range_header and request.headers.get("if-range")
            and request.headers.get("if-range") != etag):
        range_header = None

    # We serve single ranges only.
    if range_header and "," in range_header:
        return Response(status_code=416,
                        headers={"Content-Range": f"bytes */{size}"})
    try:
        offset, length = _parse_range(range_header, size)
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
        "Content-Disposition": _content_disposition(
            filename, attachment=is_explicit_dl),
        "ETag": etag,
        "Cache-Control": "private, no-transform",
        "X-Content-Type-Options": "nosniff",
    }
    partial = offset != 0 or length != size
    if partial:
        headers["Content-Range"] = f"bytes {offset}-{offset + length - 1}/{size}"

    if request.method == "HEAD":
        return Response(status_code=206 if partial else 200, headers=headers)

    # Open the MTProto stream with the first chunk pre-fetched so
    # FileReferenceExpired / stalls surface BEFORE the response starts.
    file_id = f.file_id
    stream = None
    first: bytes | None = None
    try:
        for attempt in (0, 1):
            try:
                async with _stream_sem:
                    stream, first = await _open_stream(
                        client, file_id, offset, length)
                break
            except FileReferenceExpired:
                if attempt == 1:
                    raise HTTPException(410, "file reference expired")
                log.info("file_reference expired for %d, refreshing", f.id)
                file_id = await _refresh_file_id(f)
    except _RESUMEABLE as exc:
        log.warning("telegram upstream stall on first chunk: %r", exc)
        raise HTTPException(503, "upstream timeout, retry")

    async def gen():
        nonlocal stream, first, file_id
        sent = 0
        resumes = 0
        cur_first = first
        while True:
            try:
                if cur_first is not None:
                    yield cur_first
                    sent += len(cur_first)
                    cur_first = None
                async with _stream_sem:
                    async for chunk in stream:
                        yield chunk
                        sent += len(chunk)
                return  # done
            except FileReferenceExpired:
                try:
                    file_id = await _refresh_file_id(f)
                except FileReferenceExpired:
                    log.warning("cannot refresh file_reference, truncating")
                    return
            except _RESUMEABLE as exc:
                log.warning("stream interrupted at %d/%d: %r",
                            sent, length, exc)
            if sent >= length:
                return
            resumes += 1
            if resumes > _MAX_RESUMES:
                log.warning("too many stream resumes, aborting")
                return
            # Resume from the last byte the client actually got.
            try:
                async with _stream_sem:
                    stream, cur_first = await _open_stream(
                        client, file_id, offset + sent, length - sent)
            except Exception as exc:  # noqa: BLE001
                log.warning("stream resume failed: %r", exc)
                return

    return StreamingResponse(gen(),
                             status_code=206 if partial else 200,
                             headers=headers)
