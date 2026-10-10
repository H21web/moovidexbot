"""Subtitles via the official OpenSubtitles REST API.

v10.14 rewrite: ONLY https://api.opensubtitles.com/api/v1 is used —
no keyless endpoints, no proxies. Requires OPENSUBTITLES_API_KEY
(free at opensubtitles.com). Without a key every function degrades to
empty/None and the callers show their normal "not available" paths.
"""
from __future__ import annotations

import logging
import time

from app.config import settings
from app.textutil import clean_title

log = logging.getLogger(__name__)

_BASE = "https://api.opensubtitles.com/api/v1"
_UA = "MoovidexBot/1.0"
_BUDGET_S = 12.0
_CACHE_TTL_S = 300.0  # 5-minute in-memory search cache

# ISO 639-1 (API wire) <-> 639-2/B (player language picker).
_LANG3 = {"en": "eng", "ml": "mal", "hi": "hin", "ta": "tam",
          "te": "tel", "kn": "kan"}
_LANG1 = {v: k for k, v in _LANG3.items()}

_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_key_warned = False


def _api_key() -> str:
    # v10.14.1: warn ONCE (not per request) when the key is missing so a
    # dead subtitle button is diagnosable from the Render logs.
    global _key_warned
    key = (settings.OPENSUBTITLES_API_KEY or "").strip()
    if not key and not _key_warned:
        _key_warned = True
        log.warning("OPENSUBTITLES_API_KEY not set — subtitles disabled. "
                    "Get a free key at opensubtitles.com and set it as env.")
    return key


def _headers() -> dict:
    return {"Api-Key": _api_key(), "User-Agent": _UA,
            "Accept": "application/json", "Content-Type": "application/json"}


def _langs_639_1(languages: str) -> str:
    out = []
    for lang in (languages or "").split(","):
        lang = lang.strip().lower()
        if not lang:
            continue
        out.append(_LANG1.get(lang, lang[:2]))
    return ",".join(out)


async def search_subtitles(title: str, languages: str = "eng",
                           limit: int = 10) -> list[dict]:
    """Search subtitles. Returns [{id, lang, name, movie, year,
    downloads, rating}] — the shape player.py expects. ``id`` is the
    downloadable file_id. Empty list when no API key is configured."""
    if not _api_key():
        return []
    q = clean_title(title or "")[:120].strip()
    if not q:
        return []
    ck = (q.lower(), languages or "")
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1][:limit]
    out: list[dict] = []
    try:
        import httpx
        params = {"query": q, "order_by": "download_count",
                  "order_direction": "desc"}
        langs = _langs_639_1(languages)
        if langs:
            params["languages"] = langs
        async with httpx.AsyncClient(timeout=_BUDGET_S) as c:
            r = await c.get(f"{_BASE}/subtitles", headers=_headers(),
                            params=params)
        if r.status_code != 200:
            log.warning("opensubtitles search HTTP %d for %r",
                        r.status_code, q)
            return []
        data = (r.json() or {}).get("data") or []
        for item in data:
            attr = item.get("attributes") or {}
            files = attr.get("files") or []
            if not files or not files[0].get("file_id"):
                continue
            f0 = files[0]
            lang1 = (attr.get("language") or "").lower()
            fd = attr.get("feature_details") or {}
            out.append({
                "id": f0["file_id"],
                "lang": _LANG3.get(lang1, lang1),
                "name": f0.get("file_name") or "",
                "movie": fd.get("title") or "",
                "year": str(fd.get("year") or ""),
                "downloads": attr.get("download_count") or 0,
                "rating": "",
            })
    except Exception as exc:  # noqa: BLE001
        log.warning("opensubtitles search failed for %r: %s", q, exc)
        return []
    out.sort(key=lambda x: -x["downloads"])
    _cache[ck] = (time.time(), out)
    return out[:limit]


async def download_subtitle(sub_id) -> tuple[bytes | None, str | None]:
    """Download one subtitle file. Returns (bytes, file_name), or
    (None, None) when unavailable (no API key, bad id, HTTP error)."""
    if not _api_key():
        return None, None
    try:
        file_id = int(sub_id)
    except (TypeError, ValueError):
        return None, None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_BUDGET_S) as c:
            r = await c.post(f"{_BASE}/download", headers=_headers(),
                             json={"file_id": file_id})
            if r.status_code != 200:
                log.warning("opensubtitles download HTTP %d for %s",
                            r.status_code, sub_id)
                return None, None
            link = (r.json() or {}).get("link")
            if not link:
                return None, None
            d = await c.get(link, timeout=_BUDGET_S)
            if d.status_code != 200 or not d.content:
                return None, None
            data = d.content
    except Exception as exc:  # noqa: BLE001
        log.warning("opensubtitles download failed for %s: %s", sub_id, exc)
        return None, None
    if b"-->" not in data[:2000]:
        log.warning("subtitle %s didn't look like SRT", sub_id)
        return None, None
    return data, f"subtitle_{file_id}.srt"
