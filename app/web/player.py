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
    playable = (f.mime_type or "").startswith(("video/", "audio/")) or \
        name.lower().endswith((".mp4", ".webm", ".mov", ".m4v", ".mp3", ".ogg", ".wav", ".m4a"))
    html_page = _template().replace("__TITLE__", escape(name)) \
        .replace("__DL_URL__", dl_url) \
        .replace("__PLAYABLE__", "true" if playable else "false") \
        .replace("__SIZE__", str(f.file_size or 0))
    return HTMLResponse(html_page)


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
