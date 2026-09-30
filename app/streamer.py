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

CHUNK_SIZE = 1024 * 1024  # 1 MiB per GetFile call
MAX_CHUNK = 1024 * 1024


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

    while True:
        want = chunk_size if remaining is None else min(chunk_size, remaining)
        if want <= 0:
            break
        # AES-CTR works on 16-byte blocks: align the request down, decrypt,
        # then slice off the leading misalignment.
        req_pos = pos - (pos % 16)
        skip = pos - req_pos
        limit = want + skip + 16

        r2 = await cdn_session.invoke(
            raw.functions.upload.GetCdnFile(
                file_token=redirect.file_token,
                offset=req_pos,
                limit=limit,
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

        data = dec[skip: skip + want]
        if not data:
            break
        yield data
        pos += len(data)
        if remaining is not None:
            remaining -= len(data)
            if remaining <= 0:
                break
        if len(enc) < limit:
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

    chunk_size = max(4096, min(chunk_size, MAX_CHUNK))
    pos = max(0, offset)
    remaining = length

    while True:
        take = chunk_size if remaining is None else min(chunk_size, remaining)
        if take <= 0:
            break
        result = await session.invoke(
            raw.functions.upload.GetFile(
                location=location,
                offset=pos,
                limit=take,
                precise=True,
                cdn_supported=True,
            ),
            sleep_threshold=30,
        )
        if isinstance(result, raw.types.upload.File):
            data = result.bytes
            if not data:
                break
            yield data
            pos += len(data)
            if remaining is not None:
                remaining -= len(data)
                if remaining <= 0:
                    break
            if len(data) < take:  # EOF
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
