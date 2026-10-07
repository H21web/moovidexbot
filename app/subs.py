"""Subtitle search + download via OpenSubtitles.

Uses ``rest.opensubtitles.org`` (no API key needed — just an
``X-User-Agent`` header) for search and ``dl.opensubtitles.org`` for the
actual .srt download. Shared by the web player's subtitle picker and the
Telegram "Get Subtitle" button.

Datacenter IP problem: OpenSubtitles' Cloudflare sometimes returns
HTTP 403 for requests from cloud hosts (Render/Voroa IPs). We retry
through proxies with a STRICT total time budget so a dead proxy can
never stall a search. Set SUBS_PROXY_URL to your own Cloudflare
Worker (see subs-proxy-worker.js) for the reliable keyless path.
Official api.opensubtitles.com (OPENSUBTITLES_API_KEY) stays as the
last-resort fallback.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse

import httpx

import os

log = logging.getLogger(__name__)

_SEARCH_BASE = "https://rest.opensubtitles.org/search"
_DL_BASE = "https://dl.opensubtitles.org/en/download/filead"
_UA = {"X-User-Agent": "MoovidexBot/1.0"}

# Total budget for one subtitle search — never stall the user longer.
_SEARCH_BUDGET_S = 18.0
# Per-attempt timeout (each proxy gets this long, max).
_ATTEMPT_S = 8.0

# CORS proxies — used ONLY when the direct request gets HTTP 403
# (Cloudflare/datacenter-IP block). The request then egresses from the
# proxy's IP instead of the host's blocked IP.
_CORS_PROXIES = [
    "https://api.allorigins.win/raw?url=",
    "https://api.cors.lol/?url=",
]


def _custom_proxy() -> str:
    """User's own CORS proxy (e.g. a free Cloudflare Worker).

    Set SUBS_PROXY_URL=https://xxx.workers.dev — the worker must accept
    ?url=<encoded-target> and return the target's response body.
    """
    return (os.environ.get("SUBS_PROXY_URL") or "").strip().rstrip("/")

# Browser-like headers — sometimes the 403 is header-based, not IP-based.
_BROWSER_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "X-User-Agent": "MoovidexBot/1.0",
}

# Tiny result cache: (query, langs) -> (time, results). Avoids repeating
# a slow failing search when the user reopens the player.
_CACHE_TTL = 300.0
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def _cache_get(q: str, langs: str) -> list[dict] | None:
    hit = _cache.get((q, langs))
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def _cache_put(q: str, langs: str, out: list[dict]) -> None:
    _cache[(q, langs)] = (time.time(), out)
    if len(_cache) > 200:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:50]:
            _cache.pop(k, None)

# Official API (fallback when OPENSUBTITLES_API_KEY is set — reliable,
# the keyless endpoint is Cloudflare-flaky from datacenter IPs).
_OS_API = "https://api.opensubtitles.com/api/v1"

# In-memory cache: sub_id -> file name (for Telegram sends).
_name_cache: dict[str, str] = {}


async def _get(url: str, deadline: float) -> httpx.Response | None:
    """GET with fallbacks when OpenSubtitles 403s the host.

    Order: direct -> browser headers -> custom worker -> public proxies.
    Every attempt respects the shared deadline — returns None when the
    budget is exhausted so a dead proxy can never stall the search.
    """
    async def _try(u: str, headers: dict, tag: str):
        if time.time() >= deadline:
            return None
        try:
            async with httpx.AsyncClient(
                    timeout=max(0.1, min(_ATTEMPT_S,
                                        deadline - time.time()))) as ahc:
                r = await ahc.get(u, headers=headers)
                log.debug("subs %s -> HTTP %d", tag, r.status_code)
                return r
        except Exception as exc:  # noqa: BLE001
            log.warning("subs %s failed: %s", tag, exc)
            return None

    # 1. direct
    r = await _try(url, _UA, "direct")
    if r is None:
        return None
    if r.status_code != 403:
        return r
    log.warning("OpenSubtitles 403 for %s — trying fallbacks", url[:70])
    # 2. browser headers (the block is sometimes header-based)
    r = await _try(url, _BROWSER_UA, "browser-headers")
    if r is not None and r.status_code == 200:
        log.info("OpenSubtitles OK with browser headers")
        return r
    # 3. user's own proxy (Cloudflare Worker) — most reliable
    custom = _custom_proxy()
    if custom:
        r = await _try(custom + "/?url=" + urllib.parse.quote(url, safe=""),
                       _BROWSER_UA, "custom-proxy")
        if r is not None and r.status_code == 200:
            log.info("OpenSubtitles OK via custom proxy")
            return r
    # 4. public CORS proxies
    for proxy in _CORS_PROXIES:
        r = await _try(proxy + urllib.parse.quote(url, safe=""),
                       _BROWSER_UA, f"proxy {proxy[:32]}")
        if r is not None and r.status_code == 200:
            log.info("OpenSubtitles OK via %s", proxy)
            return r
    return None


def _clean_query(file_name: str) -> str:
    """Reduce a release file name to a searchable title.

    Keeps it deliberately simple — the API does fulltext matching.
    """
    s = re.sub(r"[._]+", " ", file_name or "")
    s = re.sub(r"[()\[\]]", " ", s)  # v10.12.2: drop parens ("Title (2025")
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


async def _search_keyless(q: str, languages: str,
                        deadline: float) -> list[dict]:
    """Keyless rest.opensubtitles.org search (with proxy fallbacks)."""
    # NB: spaces must be "+" (quote_plus) — "%20" gets a broken redirect.
    url = (f"{_SEARCH_BASE}/query-{urllib.parse.quote_plus(q)}"
           f"/sublanguageid-{languages}")
    r = await _get(url, deadline)
    if r is None or r.status_code != 200:
        log.warning("subtitle search failed for %r (budget exhausted)",
                    q)
        return []
    data = r.json()
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
    return out


async def _search_official(q: str, languages: str) -> list[dict]:
    """Official api.opensubtitles.com search (needs OPENSUBTITLES_API_KEY)."""
    api_key = (os.environ.get("OPENSUBTITLES_API_KEY") or "").strip()
    if not api_key:
        return []
    langs = ",".join(languages.split(","))  # eng -> eng (ISO 639-2)
    url = f"{_OS_API}/subtitles"
    headers = {"Api-Key": api_key, "User-Agent": "MoovidexBot/1.0",
               "Accept": "application/json"}
    params = {"query": q, "languages": langs}
    async with httpx.AsyncClient(timeout=20) as hc:
        r = await hc.get(url, headers=headers, params=params)
        if r.status_code != 200:
            log.warning("official subtitle search HTTP %d for %r",
                        r.status_code, q)
            return []
        data = r.json()
    out = []
    for item in (data.get("data") or []):
        attr = item.get("attributes") or {}
        files = attr.get("files") or []
        if not files:
            continue
        f0 = files[0]
        sid = str(f0.get("file_id") or "")
        if not sid:
            continue
        name = f0.get("file_name") or ""
        _name_cache[sid] = name
        # Official downloads need a user token; stash the file_id so
        # download_subtitle can use the official flow when configured.
        _official_ids[sid] = sid
        out.append({
            "id": sid,
            "lang": attr.get("language") or "",
            "name": name,
            "movie": (attr.get("feature_details") or {}).get("title") or "",
            "year": str((attr.get("feature_details") or {}).get("year") or ""),
            "downloads": attr.get("download_count") or 0,
            "rating": "",
        })
    return out


# sub_ids that came from the official API (download flow differs).
_official_ids: set[str] = set()


async def search_subtitles(title: str, languages: str = "eng",
                           limit: int = 10) -> list[dict]:
    """Search subtitles. Returns [{id, lang, name, downloads, rating}].

    Total time budget is capped (_SEARCH_BUDGET_S) so subtitle hunting
    can never stall the bot; results are cached 5 minutes.
    """
    q = _clean_query(title)
    if not q:
        return []
    cached = _cache_get(q, languages)
    if cached is not None:
        return cached[:limit]
    deadline = time.time() + _SEARCH_BUDGET_S
    try:
        out = await _search_keyless(q, languages, deadline)
    except Exception as exc:  # noqa: BLE001
        log.warning("subtitle search failed for %r: %s", q, exc)
        out = []
    if not out and time.time() < deadline:
        # Fall back to the official API when a key is configured.
        try:
            out = await _search_official(q, languages)
        except Exception as exc:  # noqa: BLE001
            log.warning("official subtitle search failed for %r: %s", q, exc)
    out.sort(key=lambda x: -x["downloads"])
    out = out[:limit]
    _cache_put(q, languages, out)
    return out


async def download_subtitle(sub_id: str) -> tuple[bytes, str] | None:
    """Download one subtitle file. Returns (bytes, file_name) or None."""
    sid = str(sub_id)
    url = f"{_DL_BASE}/{urllib.parse.quote(sid)}"
    deadline = time.time() + _SEARCH_BUDGET_S
    try:
        r = await _get(url, deadline)
        if r is None or r.status_code != 200:
            log.warning("subtitle download failed for %s (budget exhausted)",
                        sid)
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
