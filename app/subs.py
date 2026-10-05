"""Subtitle search + download via SubDL (subdl.com).

Requires SUBDL_API_KEY env var (free from subdl.com).
Search: GET api.subdl.com/api/v1/subtitles
Download: dl.subdl.com + url -> .zip -> extract .srt
"""
from __future__ import annotations

import io
import logging
import os
import re
import urllib.parse
import zipfile

import httpx

log = logging.getLogger(__name__)

_API_BASE = "https://api.subdl.com/api/v1/subtitles"
_DL_BASE = "https://dl.subdl.com"
_UA = {"User-Agent": "MoovidexBot/1.0"}

# sub_id -> download url (in-memory; bot is long-running).
_url_cache: dict[str, str] = {}
_name_cache: dict[str, str] = {}

_LANG_MAP = {"eng": "EN", "mal": "ML", "hin": "HI", "tam": "TA",
             "tel": "TE", "kan": "KN"}


def _api_key() -> str:
    return (os.environ.get("SUBDL_API_KEY") or "").strip()


def _clean_query(file_name: str) -> tuple[str, str]:
    """Reduce a release file name to (title, year)."""
    s = (file_name or "").rsplit(".", 1)[0]  # drop extension
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"[()\[\]]", " ", s)
    s = re.sub(r"(?i)\b(s\d{1,2}e\d{1,2}|season\s*\d+|episode\s*\d+)\b.*$", "", s)
    year = ""
    m = re.search(r"(19\d{2}|20\d{2})", s)
    if m:
        year = m.group(1)
    s = re.sub(r"(?i)\b(2160p|1080p|720p|480p|4k|uhd|hd|web-?dl|webrip|"
               r"bluray|brrip|bdrip|dvdrip|hdtv|x264|x265|hevc|10bit|"
               r"ddp?\d?\.?\d?|aac|ac3|dts|hindi|tamil|telugu|malayalam|"
               r"english|dual|multi|esub|subs?|19\d{2}|20\d{2})\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80], year


async def search_subtitles(title: str, languages: str = "eng",
                           limit: int = 10) -> list[dict]:
    """Search subtitles. Returns [{id, lang, name, downloads}]."""
    api_key = _api_key()
    if not api_key:
        log.warning("SUBDL_API_KEY not set — subtitle search disabled")
        return []
    q, year = _clean_query(title)
    if not q:
        return []
    langs = ",".join(_LANG_MAP.get(l.strip().lower(), l.strip().upper())
                     for l in languages.split(",") if l.strip())
    params = {"api_key": api_key, "film_name": q, "languages": langs,
              "type": "movie"}
    if year:
        params["year"] = year
    url = f"{_API_BASE}?{urllib.parse.urlencode(params)}"
    try:
        async with httpx.AsyncClient(timeout=20) as hc:
            r = await hc.get(url, headers=_UA)
            if r.status_code != 200:
                log.warning("subdl search HTTP %d for %r", r.status_code, q)
                return []
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("subdl search failed for %r: %s", q, exc)
        return []
    if not data or data.get("status") is False:
        log.warning("subdl search status=false for %r", q)
        return []
    out = []
    for i, hit in enumerate(data.get("subtitles") or []):
        dl_url = hit.get("url") or ""
        if not dl_url:
            continue
        sid = f"subdl{i}"
        full_url = _DL_BASE + dl_url if dl_url.startswith("/") else dl_url
        _url_cache[sid] = full_url
        name = hit.get("name") or hit.get("file_name") or ""
        _name_cache[sid] = name
        out.append({
            "id": sid,
            "lang": (hit.get("language") or "").lower(),
            "name": name,
            "movie": q,
            "year": year,
            "downloads": hit.get("downloads") or 0,
            "rating": "",
        })
        if len(out) >= limit:
            break
    return out


async def download_subtitle(sub_id: str) -> tuple[bytes, str] | None:
    """Download one subtitle (zip -> extract .srt). Returns (bytes, name)."""
    sid = str(sub_id)
    url = _url_cache.get(sid)
    if not url:
        log.warning("subdl: unknown sub_id %s", sid)
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as hc:
            r = await hc.get(url, headers=_UA)
            if r.status_code != 200:
                log.warning("subdl download HTTP %d for %s",
                            r.status_code, sid)
                return None
            zdata = r.content
    except Exception as exc:  # noqa: BLE001
        log.warning("subdl download failed for %s: %s", sid, exc)
        return None
    # Extract the .srt from the zip.
    try:
        with zipfile.ZipFile(io.BytesIO(zdata)) as zf:
            srt_names = [n for n in zf.namelist()
                         if n.lower().endswith(".srt")]
            if not srt_names:
                log.warning("subdl zip for %s has no .srt", sid)
                return None
            # Prefer English-named, else first.
            srt_name = srt_names[0]
            data = zf.read(srt_name)
    except Exception as exc:  # noqa: BLE001
        log.warning("subdl zip extract failed for %s: %s", sid, exc)
        return None
    if not data or b"-->" not in data[:2000]:
        log.warning("subdl extracted file for %s didn't look like SRT", sid)
        return None
    name = _name_cache.get(sid) or "subtitle.srt"
    if not name.lower().endswith(".srt"):
        name = name.rsplit(".", 1)[0] + ".srt" if "." in name else name + ".srt"
    return data, name
