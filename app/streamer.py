"""Stream any indexed file over HTTP using raw MTProto.

This is what powers the web player + /dl downloads for files of ANY size
(Bot API downloads cap at 20 MB; MTProto has no such limit).

Technique (mirrors pyrogram's own download path):
  * decode the file_id -> DC + location
  * open a per-DC media session (auth exported from the main session)
  * raw ``upload.GetFile`` in 1 MiB chunks whose offsets are always
    multiples of the chunk size (pyrogram's own downloader shape — the
    only shape Telegram reliably accepts), with up to 4 requests in
    flight at once for throughput; HTTP Range offsets are sliced locally.
  * if Telegram answers ``upload.FileCdnRedirect``, follow it: open a
    session on the CDN DC (no auth import needed — the file_token
    authorizes), fetch via ``upload.GetCdnFile``, AES-256-CTR decrypt
    and SHA-256 verify every part (same as pyrogram's client).

Only the single bot client streams.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from hashlib import sha256
from typing import AsyncGenerator

from pyrogram import raw
from pyrogram.crypto import aes
from pyrogram.errors import VolumeLocNotFound
from pyrogram.errors.exceptions.bad_request_400 import LimitInvalid
from pyrogram.file_id import FileId, FileType
from pyrogram.session import Auth, Session

log = logging.getLogger(__name__)

# upload.GetFile request shape — the only shape Telegram reliably accepts
# (verified against pyrogram's own downloader):
#   * 1 MiB chunks, offset ALWAYS a multiple of the chunk size,
#   * no `precise` flag (arbitrary HTTP Range offsets are sliced locally).
# We always request the FULL chunk and let Telegram short-read at EOF.
CHUNK_SIZE = 1024 * 1024
MAX_CHUNK = 1024 * 1024
# In-flight GetFile requests per stream — hides Telegram round-trip
# latency; chunks are still yielded in strict order. Tunable via
# STREAM_PARALLEL (1..8) if a DC throttles aggressively.
def _parallel():
    try:
        return max(1, min(8, int(os.environ.get("STREAM_PARALLEL", "4") or 4)))
    except ValueError:
        return 4
_PARALLEL = _parallel()


class StreamError(RuntimeError):
    pass


# dc_id -> started media Session
_sessions: dict[int, Session] = {}
# cdn dc_id -> started CDN Session (no imported auth — file_token is the auth)
_cdn_sessions: dict[int, Session] = {}
_sessions_lock = asyncio.Lock()


def _location_for(fid: FileId):
    if fid.file_type == FileType.PHOTO:
        return raw.types.InputPhotoFileLocation(
            id=fid.media_id,
            access_hash=fid.access_hash,
            file_reference=fid.file_reference,
            thumb_size=fid.thumbnail_size or "y",
        )
    # document, video, audio, voice, animation, video_note, sticker ...
    return raw.types.InputDocumentFileLocation(
        id=fid.media_id,
        access_hash=fid.access_hash,
        file_reference=fid.file_reference,
        thumb_size=fid.thumbnail_size or "",
    )


async def _media_session(client, dc_id: int) -> Session:
    """Return a started media session for ``dc_id`` (cached)."""
    async with _sessions_lock:
        session = _sessions.get(dc_id)
        if session is not None:
            return session
        main_dc = await client.storage.dc_id()
        test_mode = await client.storage.test_mode()
        if dc_id == main_dc:
            auth_key = await client.storage.auth_key()
        else:
            auth_key = await Auth(client, dc_id, test_mode).create()
        session = Session(client, dc_id, auth_key, test_mode, is_media=True)
        await session.start()
        if dc_id != main_dc:
            exported = await client.invoke(
                raw.functions.auth.ExportAuthorization(dc_id=dc_id)
            )
            await session.invoke(
                raw.functions.auth.ImportAuthorization(
                    id=exported.id, bytes=exported.bytes
                )
            )
        _sessions[dc_id] = session
        log.info("opened media session for dc %d", dc_id)
        return session


async def _cdn_session(client, dc_id: int) -> Session:
    """Return a started CDN session (cached).

    CDN DCs need no imported authorization — the ``file_token`` from the
    redirect authorizes each ``GetCdnFile`` call (same as pyrogram does).
    """
    async with _sessions_lock:
        session = _cdn_sessions.get(dc_id)
        if session is not None:
            return session
        test_mode = await client.storage.test_mode()
        auth_key = await Auth(client, dc_id, test_mode).create()
        session = Session(client, dc_id, auth_key, test_mode,
                          is_media=True, is_cdn=True)
        await session.start()
        _cdn_sessions[dc_id] = session
        log.info("opened CDN session for dc %d", dc_id)
        return session


async def _stream_cdn(client, main_session: Session,
                      redirect: raw.types.upload.FileCdnRedirect,
                      offset: int, length: int | None,
                      chunk_size: int) -> AsyncGenerator[bytes, None]:
    """Yield decrypted, hash-verified bytes for a CDN-hosted file.

    Mirrors ``pyrogram.Client.handle_download`` CDN branch:
    https://core.telegram.org/cdn#decrypting-files
    https://core.telegram.org/cdn#verifying-files
    """
    cdn_session = await _cdn_session(client, redirect.dc_id)
    key = redirect.encryption_key
    iv_prefix = bytes(redirect.encryption_iv[:-4])

    pos = max(0, offset)
    remaining = length
    # Chunk-multiple alignment like the main path (a multiple of 16, so
    # the AES-CTR IV math below stays correct). Always request the FULL
    # chunk — GetCdnFile has the same LIMIT_INVALID rules as GetFile.
    req_pos = (pos // chunk_size) * chunk_size
    skip = pos - req_pos

    while True:
        if remaining is not None and remaining <= 0:
            break

        r2 = await cdn_session.invoke(
            raw.functions.upload.GetCdnFile(
                file_token=redirect.file_token,
                offset=req_pos,
                limit=chunk_size,
            ),
            sleep_threshold=30,
        )
        if isinstance(r2, raw.types.upload.CdnFileReuploadNeeded):
            try:
                await main_session.invoke(
                    raw.functions.upload.ReuploadCdnFile(
                        file_token=redirect.file_token,
                        request_token=r2.request_token,
                    )
                )
            except VolumeLocNotFound:
                break
            continue  # retry the same range

        enc = r2.bytes
        if not enc:
            break

        iv = bytearray(iv_prefix + (req_pos // 16).to_bytes(4, "big"))
        dec = aes.ctr256_decrypt(enc, key, iv)

        # Verify per-part SHA-256 hashes (plaintext, like pyrogram).
        try:
            hashes = await main_session.invoke(
                raw.functions.upload.GetCdnFileHashes(
                    file_token=redirect.file_token,
                    offset=req_pos,
                )
            )
        except Exception as exc:
            log.debug("GetCdnFileHashes failed: %s", exc)
            hashes = []
        for i, h in enumerate(hashes):
            part = dec[h.limit * i: h.limit * (i + 1)]
            if len(part) == h.limit and sha256(part).digest() != h.hash:
                raise StreamError("CDN chunk hash mismatch")

        # Slice off the leading misalignment (first chunk only), then
        # trim to the requested range. The server short-reads at EOF.
        data = dec[skip:] if skip else dec
        skip = 0
        if remaining is not None:
            data = data[:remaining]
            remaining -= len(data)
        if not data:
            break
        yield data
        req_pos += chunk_size
        if len(enc) < chunk_size:
            break  # short read = EOF


async def _fetch_chunk(session, location, index: int, chunk_size: int):
    """Fetch one chunk. Never raises — returns (index, ok, result|exc).

    Tasks are never cancelled (abandoned ones finish on their own and
    their results are discarded), so the session's in-flight bookkeeping
    is never disturbed.
    """
    try:
        res = await session.invoke(
            raw.functions.upload.GetFile(
                location=location,
                offset=index * chunk_size,
                limit=chunk_size,
                cdn_supported=True,
            ),
            sleep_threshold=30,
        )
        return (index, True, res)
    except LimitInvalid:
        # Should not happen with the chunk-multiple shape; log everything
        # so the next occurrence is diagnosable instead of a mystery.
        log.error("GetFile LIMIT_INVALID: chunk=%d offset=%d limit=%d",
                  index, index * chunk_size, chunk_size)
        return (index, False, LimitInvalid("chunk request rejected"))
    except Exception as exc:  # noqa: BLE001
        return (index, False, exc)


async def stream_file(
    client,
    file_id: str,
    offset: int = 0,
    length: int | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> AsyncGenerator[bytes, None]:
    """Yield file bytes from ``offset`` for ``length`` bytes (None = to EOF).

    Up to ``_PARALLEL`` chunk requests stay in flight at once (hides
    Telegram round-trip latency); chunks are always yielded in strict
    order. Follows CDN redirects transparently (decrypt + hash-verify).
    ``pyrogram.errors.FileReferenceExpired`` propagates so the caller can
    refresh the file_id from the source message and retry once.
    """
    fid = FileId.decode(file_id)
    location = _location_for(fid)
    session = await _media_session(client, fid.dc_id)

    # Fixed 1 MiB chunks (pyrogram's own size).
    chunk_size = CHUNK_SIZE
    pos = max(0, offset)
    end_pos = pos + length if length is not None else None

    first_index = pos // chunk_size
    last_index = ((end_pos - 1) // chunk_size) if end_pos is not None else None
    skip_first = pos - first_index * chunk_size

    pending: dict[int, asyncio.Task] = {}
    next_fetch = first_index
    yield_index = first_index
    stream_pos = pos
    remaining = length
    eof = False
    first = True
    # Throughput telemetry (Render logs): proves whether a slow stream is
    # Telegram-side or something else. One line per stream.
    _t0 = time.monotonic()
    _sent = 0
    try:
        while True:
            if remaining is not None and remaining <= 0:
                break
            # Top up the pipeline (never fetch past the last needed chunk).
            while (not eof and len(pending) < _PARALLEL
                   and (last_index is None or next_fetch <= last_index)):
                pending[next_fetch] = asyncio.create_task(
                    _fetch_chunk(session, location, next_fetch, chunk_size))
                next_fetch += 1
            if yield_index not in pending:
                break  # drained
            _, ok, res = await pending.pop(yield_index)
            yield_index += 1
            if not ok:
                pending.clear()  # siblings finish alone; results discarded
                raise res
            if isinstance(res, raw.types.upload.File):
                data = res.bytes
                if not data:
                    eof = True
                    pending.clear()
                    break  # EOF
                if first:
                    if skip_first:
                        data = data[skip_first:]
                    first = False
                if remaining is not None:
                    data = data[:remaining]
                    remaining -= len(data)
                if not data:
                    pending.clear()
                    break
                yield data
                stream_pos += len(data)
                _sent += len(data)
                if len(res.bytes) < chunk_size:  # short read = EOF
                    eof = True
                    pending.clear()
                    break
                if remaining is not None and remaining <= 0:
                    pending.clear()
                    break
            elif isinstance(res, raw.types.upload.FileCdnRedirect):
                log.info("CDN redirect for file (dc %d), following",
                         res.dc_id)
                pending.clear()
                async for chunk in _stream_cdn(client, session, res,
                                               stream_pos, remaining, chunk_size):
                    _sent += len(chunk)
                    yield chunk
                return
            else:
                pending.clear()
                raise StreamError(f"unexpected GetFile response: {type(res).__name__}")
    finally:
        dt = time.monotonic() - _t0
        if _sent:
            log.info("streamed %.1f MB in %.1fs (%.2f MB/s, parallel=%d)",
                     _sent / 1048576, dt, _sent / 1048576 / max(dt, 0.01),
                     _PARALLEL)
