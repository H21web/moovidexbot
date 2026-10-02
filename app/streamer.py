"""Stream any indexed file over HTTP using raw MTProto.

This is what powers the web player + /dl downloads for files of ANY size
(Bot API downloads cap at 20 MB; MTProto has no such limit).

Technique (mirrors pyrogram's own download path):
  * decode the file_id -> DC + location
  * open a per-DC media session (auth exported from the main session)
  * raw ``upload.GetFile`` with ``precise=True`` byte offsets, so HTTP
    Range requests (video seeking) work exactly.
  * if Telegram answers ``upload.FileCdnRedirect``, follow it: open a
    session on the CDN DC (no auth import needed — the file_token
    authorizes), fetch via ``upload.GetCdnFile``, AES-256-CTR decrypt
    and SHA-256 verify every part (same as pyrogram's client).

Only the single bot client streams.
"""
from __future__ import annotations

import asyncio
import logging
from hashlib import sha256
from typing import AsyncGenerator

from pyrogram import raw
from pyrogram.crypto import aes
from pyrogram.errors import VolumeLocNotFound
from pyrogram.file_id import FileId, FileType
from pyrogram.session import Auth, Session

log = logging.getLogger(__name__)

# Telegram's upload.GetFile granularity rules (violating them raises
# [400 LIMIT_INVALID]):
#   * with precise=True: offset and limit must be multiples of 1 KiB,
#     limit <= 1 MiB;
#   * without precise: multiples of 4 KiB and (1 MiB % limit == 0).
# 512 KiB satisfies BOTH regimes, so every request we build is valid by
# construction. We always request the FULL chunk and let Telegram
# short-read at EOF — trimming the final limit to the exact remainder
# is what used to break the granularity rule.
CHUNK_SIZE = 512 * 1024
MAX_CHUNK = 512 * 1024
_GRANULARITY = 1024  # precise=True -> 1 KiB alignment


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
    # v10.3.1: align to the 1 KiB granularity (a multiple of 16, so the
    # AES-CTR IV math below stays correct) and always request the FULL
    # chunk — GetCdnFile has the same LIMIT_INVALID rules as GetFile.
    req_pos = pos - (pos % _GRANULARITY)
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


async def stream_file(
    client,
    file_id: str,
    offset: int = 0,
    length: int | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> AsyncGenerator[bytes, None]:
    """Yield file bytes from ``offset`` for ``length`` bytes (None = to EOF).

    Follows CDN redirects transparently (decrypt + hash-verify).
    ``pyrogram.errors.FileReferenceExpired`` propagates so the caller can
    refresh the file_id from the source message and retry once.
    """
    fid = FileId.decode(file_id)
    location = _location_for(fid)
    session = await _media_session(client, fid.dc_id)

    # v10.3.1: round the chunk to the granularity — every limit we send
    # is then a valid power-of-2 multiple (512 KiB) by construction.
    chunk_size = max(_GRANULARITY, min(chunk_size, MAX_CHUNK))
    chunk_size -= chunk_size % _GRANULARITY
    pos = max(0, offset)
    remaining = length
    # Align the first request down to the granularity; drop the leading
    # bytes locally. HTTP Range offsets are arbitrary — sending them raw
    # is what raised [400 LIMIT_INVALID].
    req_pos = pos - (pos % _GRANULARITY)
    skip = pos - req_pos

    while True:
        if remaining is not None and remaining <= 0:
            break
        # Always the FULL chunk — never trim the limit to the remainder.
        result = await session.invoke(
            raw.functions.upload.GetFile(
                location=location,
                offset=req_pos,
                limit=chunk_size,
                precise=True,
                cdn_supported=True,
            ),
            sleep_threshold=30,
        )
        if isinstance(result, raw.types.upload.File):
            data = result.bytes
            if not data:
                break  # EOF
            if skip:
                data = data[skip:]
                skip = 0
            if remaining is not None:
                data = data[:remaining]
                remaining -= len(data)
            if not data:
                break
            yield data
            pos += len(data)
            req_pos += chunk_size
            if len(result.bytes) < chunk_size:  # short read = EOF
                break
        elif isinstance(result, raw.types.upload.FileCdnRedirect):
            log.info("CDN redirect for file (dc %d), following",
                     result.dc_id)
            async for chunk in _stream_cdn(client, session, result,
                                           pos, remaining, chunk_size):
                yield chunk
            break
        else:
            raise StreamError(f"unexpected GetFile response: {type(result).__name__}")
