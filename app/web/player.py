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

_WATCH_HTML = os.path.join(os.path.dirname(__file__), "watch.html")
_watch_template: str | None = None

_LOGO_URL = (os.environ.get("LOGO_URL") or
             "https://h21web.github.io/cdn/assets/moovidex-logo.png").strip()
# BOT_USERNAME env overrides get_me() (e.g. after a bot rename).
_BOT_USERNAME_ENV = (os.environ.get("BOT_USERNAME") or "").strip().lstrip("@")

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


def _check_file_id(file_id: str) -> None:
    """Eager file_id sanity check (no network).

    Catches corrupt file_ids before HTTP headers are sent — the stream
    itself now starts immediately with no pre-flight fetch, so this is
    the fail-fast gate. Raises 410 on garbage.
    """
    from pyrogram.file_id import FileId
    try:
        FileId.decode(file_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(410, f"bad file reference: {exc}")


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
    # Poster: JustWatch last backdrop (cinematic), TMDB poster as fallback.
    # Best-effort, cached by the enrich layer.
    poster = ""
    try:
        guess = _title_guess(name)
        if guess:
            from app.enrich import justwatch_titles
            jw = await justwatch_titles(guess, limit=3)
            if jw and jw[0].get("backdrop"):
                poster = jw[0]["backdrop"]
            else:
                meta = await tmdb.get_movie(guess)
                poster = (meta or {}).get("poster_url") or ""
    except Exception:  # noqa: BLE001
        poster = ""

    # File details — DB values first, filename detection as fallback.
    from app.textutil import detect_quality_language, extract_year
    det_q, det_l = detect_quality_language(name)
    quality = f.quality or det_q or "—"
    language = f.language or det_l or "—"
    year = extract_year(name)
    year_s = str(year) if year else "—"
    fmt = ("." + ext) if ext else "—"

    username = _BOT_USERNAME_ENV or await _bot_username()
    bot_url = f"https://telegram.me/{username}" if username else "#"

    html_page = _template().replace("__TITLE__", escape(name))
    # JS-string-safe filename: a full JSON string literal is valid JS.
    js_name = json.dumps(name.replace("\r", "").replace("\n", ""))
    html_page = (html_page
                 .replace("__LOGO_URL__", _LOGO_URL)
                 .replace("__BOT_URL__", bot_url)
                 .replace("__DL_URL__", dl_url)
                 .replace("__DL_DL_URL__", dl_url + "?dl=1")
                 .replace("__GO_URL__", f"{base}/go/ext?token={token}")
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
    """Extract a clean title from a release filename.

    'KGF.Chapter.2.2022.1080p.WEB-DL.mkv' -> 'KGF Chapter 2 2022'
    'Show.Name.S01E02.720p.mkv' -> 'Show Name' (series: episode cut)
    """
    base = name.rsplit(".", 1)[0] if "." in name else name
    # Series: cut from the episode marker, keep the show name.
    base = re.split(r"\bS\d{1,2}E\d{1,3}\b", base, flags=re.IGNORECASE)[0]
    base = re.split(r"\bSeason\s*\d+\b", base, flags=re.IGNORECASE)[0]
    # Grab the year before stripping parentheticals like (2022).
    m = re.search(r"\b(19\d{2}|20\d{2})\b", base)
    year = m.group(1) if m else ""
    base = re.sub(r"[\[\(].*?[\]\)]", " ", base)
    base = re.sub(
        r"\b(480p|720p|1080p|1080i|2160p|4k|uhd|hdrip|webrip|web-dl|webdl|"
        r"bluray|brrip|bdrip|hdts|hdtv|dvdrip|dvdscr|x264|x265|hevc|h264|"
        r"h265|10bit|8bit|aac|ac3|ddp\d?\.?\d?|dd5\.1|dts|esub|esubs|subs|"
        r"hindi|tamil|telugu|malayalam|kannada)\b",
        " ", base, flags=re.IGNORECASE)
    base = re.sub(r"\b\d+(\.\d+)?\s*(gb|mb)\b", " ", base, flags=re.IGNORECASE)
    base = re.sub(r"[._]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip(" -")
    guess = base[:80].strip()
    if year and year not in guess:
        guess = f"{guess} {year}".strip()
    return guess


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


@router.get("/go/ext", response_class=HTMLResponse)
async def go_ext(token: str, player: str = "mx", request: Request = None):
    """External-player redirector.

    Telegram's in-app browser cannot fire ``intent://`` URLs, so the
    watch page opens this URL via ``Telegram.WebApp.openLink()`` in the
    system browser — which then fires the intent into the player app.
    Includes manual fallback links if the intent is swallowed.
    """
    try:
        f = await _get_file(token)
    except HTTPException as exc:
        if exc.status_code == 403:
            return _error_page(403, "Link expired",
                               "This link is invalid or has expired.")
        return _error_page(404, "File not found",
                           "This file is no longer indexed.")
    base = str(request.base_url).rstrip("/") if request else ""
    stream_url = f"{base}/dl/{token}"
    players = {
        "mx": ("com.mxtech.videoplayer.ad", "", "MX Player"),
        "vlc": ("org.videolan.vlc", "", "VLC"),
        "km": ("com.kmplayer", "", "KMPlayer"),
        "playit": ("com.playit.videoplayer", "", "PLAYit"),
        "s": ("com.young.simple.player",
              "com.young.simple.player.playback_online", "S Player"),
        "u": ("uplayer.video.player", "", "U Player"),
    }
    pkg, action, pname = players.get((player or "mx").lower(), players["mx"])
    intent = (f"intent:{stream_url}#Intent;"
              + (f"action={action};" if action else "")
              + f"package={pkg};type=video/*;"
              + f"S.browser_fallback_url={quote(stream_url, safe='')};end")
    store_url = f"https://play.google.com/store/apps/details?id={pkg}"
    name = escape((f.file_name or "Video").rsplit("/", 1)[-1])
    pname_e = escape(pname)
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Opening {pname_e}…</title></head>"
        "<body style='background:#0b0b10;color:#fff;font-family:sans-serif;"
        "text-align:center;padding:48px 24px'>"
        f"<h3 style='margin:0 0 8px'>{name}</h3>"
        f"<p style='color:#aaa' id='st'>Opening in {pname_e}…</p>"
        f"<p><a href='{intent}' style='color:#a06bff;font-size:18px'>"
        "Tap here if the player didn't open</a></p>"
        f"<p><a href='{stream_url}?dl=1' style='color:#888'>"
        "Download instead</a></p>"
        # If the app isn't installed the intent goes nowhere and this
        # page stays visible — after 2s offer the Play Store install.
        "<div id='noapp' style='display:none;margin-top:24px;"
        "border:1px solid #333;border-radius:12px;padding:16px'>"
        f"<p style='margin:0 0 8px'>Couldn't open {pname_e} — "
        "is it installed?</p>"
        f"<a href='{store_url}' style='display:inline-block;background:#a06bff;"
        "color:#fff;padding:10px 22px;border-radius:10px;text-decoration:none;"
        f"font-weight:700'>Install {pname_e}</a></div>"
        "<script>"
        f"window.location.href={json.dumps(intent)};"
        "setTimeout(function(){"
        "if(document.visibilityState==='visible'){"
        "document.getElementById('noapp').style.display='block';"
        f"document.getElementById('st').textContent={json.dumps(pname)}"
        "+' did not open.';}},2000);"
        "</script>"
        "</body></html>")


@router.get("/subs/pick/{token}", response_class=HTMLResponse)
async def subs_pick(token: str, request: Request):
    """Telegram Web App: subtitle language picker for a file.

    Lists available subtitle languages; tapping one makes the bot send
    the .srt to the user and closes the web app.
    """
    data = parse_watch_token(token)
    if not data:
        return _error_page(403, "Link expired",
                           "This link is invalid or has expired.")
    try:
        f = await _get_file(token)
    except HTTPException:
        return _error_page(404, "File not found",
                           "This file is no longer indexed.")
    from app import subs
    results = await subs.search_subtitles(f.file_name or "", "eng,mal,hin,tam",
                                          limit=30)
    # Group by language, keep the best per language.
    by_lang: dict[str, dict] = {}
    for r in results:
        lang = (r.get("lang") or "?").lower()
        if lang not in by_lang:
            by_lang[lang] = {"sub_id": r["id"], "count": 0,
                             "name": r.get("name") or ""}
        by_lang[lang]["count"] += 1
    lang_names = {"eng": "English", "mal": "Malayalam", "hin": "Hindi",
                  "tam": "Tamil", "tel": "Telugu", "kan": "Kannada"}
    buttons = []
    for lang, info in sorted(by_lang.items(),
                             key=lambda kv: -kv[1]["count"]):
        label = lang_names.get(lang, lang.upper())
        buttons.append(
            f"<button data-sub='{info['sub_id']}' "
            f"style='display:block;width:100%;margin:8px 0;padding:14px;"
            f"background:#1a1a24;border:1px solid #333;color:#fff;"
            f"border-radius:12px;font-size:16px'>"
            f"{escape(label)} <span style='color:#888;font-size:13px'>"
            f"({info['count']})</span></button>")
    body = "".join(buttons) if buttons else (
        "<p style='color:#888'>No subtitles found for this title.</p>")
    # Auto-translate: translate the top result to Malayalam / English.
    trans_html = ""
    if by_lang:
        first_id = next(iter(by_lang.values()))["sub_id"]
        trans_html = (
            "<p style='color:#888;font-size:13px;margin:16px 0 4px'>"
            "🌐 Auto-translate</p>"
            f"<button data-tr='mal:{first_id}' style='display:block;width:100%;"
            f"margin:8px 0;padding:14px;background:#1a1a24;border:1px solid #333;"
            f"color:#fff;border-radius:12px;font-size:16px'>"
            f"→ Malayalam</button>"
            f"<button data-tr='eng:{first_id}' style='display:block;width:100%;"
            f"margin:8px 0;padding:14px;background:#1a1a24;border:1px solid #333;"
            f"color:#fff;border-radius:12px;font-size:16px'>"
            f"→ English</button>")
    title = escape((f.file_name or "file")[:60])
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Subtitles</title>"
        "<script src='https://telegram.org/js/telegram-web-app.js'></script>"
        "</head>"
        "<body style='background:#0b0b10;color:#fff;font-family:sans-serif;"
        "padding:24px;max-width:480px;margin:0 auto'>"
        f"<h3 style='margin:0 0 4px'>📝 Subtitles</h3>"
        f"<p style='color:#888;font-size:13px;margin:0 0 16px'>{title}</p>"
        f"<div id='langs'>{body}{trans_html}</div>"
        "<p id='msg' style='color:#888;font-size:13px'></p>"
        "<script>"
        "var tg=(window.Telegram&&window.Telegram.WebApp)?window.Telegram.WebApp:null;"
        "if(tg){tg.ready();tg.expand();}"
        f"var token={json.dumps(token)};"
        "function sendSub(subId, to){"
        "var msg=document.getElementById('msg');msg.textContent='Sending…';"
        "var url='/subs/send?token='+encodeURIComponent(token)+"
        "'&sub_id='+encodeURIComponent(subId);"
        "if(to)url+='&to='+encodeURIComponent(to);"
        "fetch(url).then(function(r){return r.json();})"
        ".then(function(j){"
        "if(j.ok){msg.textContent='Sent! Closing…';"
        "setTimeout(function(){if(tg)tg.close();},800);}"
        "else{msg.textContent='Failed: '+(j.error||'try again');}"
        "}).catch(function(){msg.textContent='Failed. Try again.';});"
        "}"
        "document.getElementById('langs').addEventListener('click',function(e){"
        "var b=e.target.closest('button[data-sub]');"
        "if(b){sendSub(b.dataset.sub);return;}"
        "var t=e.target.closest('button[data-tr]');"
        "if(t){var p=t.dataset.tr.split(':');sendSub(p[1],p[0]);}"
        "});"
        "</script></body></html>")


@router.get("/subs/send")
async def subs_send(token: str, sub_id: str, to: str = ""):
    """Web-app action: download the subtitle and send it via the bot.

    ``to`` (e.g. ``mal``) auto-translates before sending.
    """
    data = parse_watch_token(token)
    if not data or not data.get("u"):
        return {"ok": False, "error": "bad token"}
    from app import subs
    to = re.sub(r"[^a-z]", "", (to or "").lower())[:5]
    if to:
        # Translate via our own endpoint logic.
        try:
            tr = await subs_translate(sub_id, to)
            sdata = tr.body
            # Extract filename from Content-Disposition.
            cd = tr.headers.get("content-disposition", "")
            m = re.search(r"filename\*=UTF-8''(.+)", cd)
            name = m.group(1) if m else f"subtitle.{to}.srt"
            from urllib.parse import unquote as _uq
            name = _uq(name)
        except HTTPException as exc:
            return {"ok": False, "error": exc.detail}
        except Exception as exc:  # noqa: BLE001
            log.warning("subs/send translate failed: %s", exc)
            return {"ok": False, "error": "translate failed"}
    else:
        got = await subs.download_subtitle(sub_id)
        if not got:
            return {"ok": False, "error": "download failed"}
        sdata, name = got
    client = bot_app.bot
    if not client:
        return {"ok": False, "error": "bot not ready"}
    try:
        await client.send_document(data["u"], document=sdata,
                                   file_name=name,
                                   caption=f"📝 {name}")
    except Exception as exc:  # noqa: BLE001
        log.warning("subs/send failed: %s", exc)
        return {"ok": False, "error": "send failed"}
    return {"ok": True}


@router.get("/subs/search")
async def subs_search(title: str, langs: str = "eng"):
    """Search subtitles (keyless OpenSubtitles). JSON list."""
    from app import subs
    langs = re.sub(r"[^a-z,]", "", (langs or "eng").lower()) or "eng"
    results = await subs.search_subtitles(title, langs, limit=12)
    return {"results": results}


@router.get("/subs/file/{sub_id}")
async def subs_file(sub_id: str):
    """Proxy one .srt so the web player can load it (same-origin)."""
    from app import subs
    got = await subs.download_subtitle(sub_id)
    if not got:
        raise HTTPException(404, "subtitle not found")
    data, name = got
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@router.get("/subs/translate/{sub_id}")
async def subs_translate(sub_id: str, to: str = "mal"):
    """Auto-translate a subtitle via Google Translate (free endpoint).

    Downloads the .srt, translates the text lines, returns a translated
    .srt. Best-effort — the unofficial endpoint can rate-limit.
    """
    import httpx as _httpx
    to = re.sub(r"[^a-z]", "", (to or "mal").lower())[:5] or "mal"
    from app import subs
    got = await subs.download_subtitle(sub_id)
    if not got:
        raise HTTPException(404, "subtitle not found")
    data, name = got
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        raise HTTPException(400, "cannot decode subtitle")
    # Parse SRT blocks, keep timing, translate text lines.
    blocks = re.split(r"\r?\n\r?\n", text.strip())
    texts = []
    idxs = []
    for bi, b in enumerate(blocks):
        lines = b.splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            t = "\n".join(lines[2:])
            # Strip basic HTML tags for translation.
            t_clean = re.sub(r"<[^>]+>", "", t).strip()
            if t_clean:
                texts.append(t_clean)
                idxs.append(bi)
    if not texts:
        raise HTTPException(400, "no translatable text")
    # Batch 25 lines per request.
    translated: list[str] = [""] * len(texts)
    try:
        async with _httpx.AsyncClient(timeout=30) as hc:
            for i in range(0, len(texts), 25):
                batch = texts[i:i + 25]
                r = await hc.get(
                    "https://translate.googleapis.com/translate_a/single",
                    params={"client": "gtx", "sl": "auto", "tl": to,
                            "dt": "t",
                            "q": batch})
                if r.status_code != 200:
                    raise HTTPException(502, "translate service busy")
                # Response: [[[translated, original, ...], ...], ...]
                j = r.json()
                for k, seg in enumerate(j[0]):
                    if i + k < len(translated) and seg and seg[0]:
                        translated[i + k] = seg[0]
                await asyncio.sleep(0.3)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("subtitle translate failed: %s", exc)
        raise HTTPException(502, "translate failed")
    # Rebuild SRT.
    out_blocks = []
    ti = 0
    for bi, b in enumerate(blocks):
        if ti < len(idxs) and bi == idxs[ti]:
            lines = b.splitlines()
            timing = lines[1] if len(lines) > 1 else ""
            num = lines[0] if lines else str(bi + 1)
            t = translated[ti] or texts[ti]
            out_blocks.append(f"{num}\n{timing}\n{t}")
            ti += 1
        else:
            out_blocks.append(b)
    out = "\n\n".join(out_blocks) + "\n"
    base = name.rsplit(".", 1)[0]
    return Response(
        content=out.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(base + '.' + to + '.srt')}"},
    )


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

    # No pre-flight fetch: the stream starts immediately and headers go
    # out at once (fast time-to-first-byte for seek/play). A corrupt
    # file_id fails fast here; an expired file_reference is caught by the
    # resume logic inside gen() and refreshed transparently.
    file_id = f.file_id
    _check_file_id(file_id)

    async def gen():
        nonlocal file_id
        end_pos = offset + length
        stream_pos = offset
        resumes = 0
        while True:
            try:
                async with _stream_sem:
                    stream = streamer.stream_file(
                        client, file_id,
                        offset=stream_pos, length=end_pos - stream_pos)
                    async for chunk in stream:
                        yield chunk
                        stream_pos += len(chunk)
                return  # done
            except FileReferenceExpired:
                try:
                    file_id = await _refresh_file_id(f)
                except FileReferenceExpired:
                    log.warning("cannot refresh file_reference, truncating")
                    return
            except _RESUMEABLE as exc:
                log.warning("stream interrupted at %d/%d: %r",
                            stream_pos - offset, length, exc)
            if stream_pos >= end_pos:
                return
            resumes += 1
            if resumes > _MAX_RESUMES:
                log.warning("too many stream resumes, aborting")
                return
            # Resume from the last byte the client actually got.

    return StreamingResponse(gen(),
                             status_code=206 if partial else 200,
                             headers=headers)
