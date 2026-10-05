"""Subtitle search + download via the keyless OpenSubtitles REST API.

Uses ``rest.opensubtitles.org`` (no API key needed — just an
``X-User-Agent`` header) for search and ``dl.opensubtitles.org`` for the
actual .srt download. Shared by the web player's subtitle picker and the
Telegram "Get Subtitle" button.
"""
from __future__ import annotations

import logging
import re
import urllib.parse

import httpx

log = logging.getLogger(__name__)

_SEARCH_BASE = "https://rest.opensubtitles.org/search"
_DL_BASE = "https://dl.opensubtitles.org/en/download/filead"
_UA = {"X-User-Agent": "MoovidexBot/1.0"}

# In-memory cache: sub_id -> file name (for Telegram sends).
_name_cache: dict[str, str] = {}


def _clean_query(file_name: str) -> str:
    """Reduce a release file name to a searchable title.

    Keeps it deliberately simple — the API does fulltext matching.
    """
    s = re.sub(r"[._]+", " ", file_name or "")
    s = re.sub(r"(?i)\b(s\d{1,2}e\d{1,2}|season\s*\d+|episode\s*\d+)\b.*$", "", s)
    s = re.sub(r"(?i)\b(2160p|1080p|720p|480p|4k|uhd|hd|web-?dl|webrip|"
               r"bluray|brrip|bdrip|dvdrip|hdtv|x264|x265|hevc|10bit|"
               r"ddp?\d?\.?\d?|aac|ac3|dts|hindi|tamil|telugu|malayalam|"
               r"english|dual|multi|esub|subs?)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Keep at most "Title Year".
    m = re.search(r"(19\d{2}|20\d{2})", s)
    if m:
        s = s[:m.end()].strip()
    return s[:80]


async def search_subtitles(title: str, languages: str = "eng",
                           limit: int = 10) -> list[dict]:
    """Search subtitles. Returns [{id, lang, name, downloads, rating}]."""
    q = _clean_query(title)
    if not q:
        return []
    url = (f"{_SEARCH_BASE}/query-{urllib.parse.quote(q)}"
           f"/sublanguageid-{languages}")
    try:
        async with httpx.AsyncClient(timeout=20) as hc:
            r = await hc.get(url, headers=_UA)
            if r.status_code != 200:
                log.warning("subtitle search HTTP %d for %r",
                            r.status_code, q)
                return []
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("subtitle search failed for %r: %s", q, exc)
        return []
    out = []
    for row in data or []:
        if (row.get("SubFormat") or "").lower() != "srt":
            continue
        sid = str(row.get("IDSubtitleFile") or "")
        if not sid or sid == "0":
            continue
        name = row.get("SubFileName") or ""
        _name_cache[sid] = name
        try:
            dl_count = int(row.get("SubDownloadsCnt") or 0)
        except (TypeError, ValueError):
            dl_count = 0
        out.append({
            "id": sid,
            "lang": row.get("SubLanguageID") or "",
            "name": name,
            "movie": row.get("MovieName") or "",
            "year": row.get("MovieYear") or "",
            "downloads": dl_count,
            "rating": row.get("SubRating") or "",
        })
    out.sort(key=lambda x: -x["downloads"])
    return out[:limit]


async def download_subtitle(sub_id: str) -> tuple[bytes, str] | None:
    """Download one subtitle file. Returns (bytes, file_name) or None."""
    sid = str(sub_id)
    url = f"{_DL_BASE}/{urllib.parse.quote(sid)}"
    try:
        async with httpx.AsyncClient(timeout=30) as hc:
            r = await hc.get(url, headers=_UA)
            if r.status_code != 200:
                log.warning("subtitle download HTTP %d for %s",
                            r.status_code, sid)
                return None
            data = r.content
    except Exception as exc:  # noqa: BLE001
        log.warning("subtitle download failed for %s: %s", sid, exc)
        return None
    if not data or b"-->" not in data[:2000]:
        log.warning("subtitle download for %s didn't look like SRT", sid)
        return None
    name = _name_cache.get(sid) or f"subtitle_{sid}.srt"
    if not name.lower().endswith(".srt"):
        name += ".srt"
    return data, name
