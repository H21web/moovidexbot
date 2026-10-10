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

# v10.15.2: user authentication — anonymous API-key requests get
# ~5 downloads/day per IP; a logged-in free user gets the account
# quota (~200/day). The old XML-RPC API is VIP-only since 2024, so
# this is the only legitimate "another way".
_login_token: str | None = None
_login_ts: float = 0.0
_LOGIN_TTL_S = 20 * 3600  # tokens live 24h; refresh a bit early
_login_warned = False

# v10.15.2: downloaded .srt bytes are cached — repeat downloads of the
# same subtitle don't burn quota. Small (~100KB each), capped.
_dl_cache: dict[str, tuple[float, bytes, str]] = {}
_DL_CACHE_TTL_S = 7 * 24 * 3600
_DL_CACHE_CAP = 300


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


def _headers(auth: str | None = None) -> dict:
    h = {"Api-Key": _api_key(), "User-Agent": _UA,
         "Accept": "application/json", "Content-Type": "application/json"}
    if auth:
        h["Authorization"] = f"Bearer {auth}"
    return h


def _user_creds() -> tuple[str, str]:
    return ((settings.OPENSUBTITLES_USERNAME or "").strip(),
            (settings.OPENSUBTITLES_PASSWORD or "").strip())


async def _ensure_login() -> str | None:
    """Bearer token for the configured user, logging in when needed.

    Returns None when no credentials are configured (anonymous mode)
    or login failed — callers then proceed without Authorization.
    """
    global _login_token, _login_ts, _login_warned
    user, pwd = _user_creds()
    if not user or not pwd:
        if not _login_warned:
            _login_warned = True
            log.warning("OPENSUBTITLES_USERNAME/PASSWORD not set — "
                        "anonymous quota (~5 downloads/day). Set them for "
                        "the free-user quota (~200/day).")
        return None
    if _login_token and time.time() - _login_ts < _LOGIN_TTL_S:
        return _login_token
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_BUDGET_S) as c:
            r = await c.post(f"{_BASE}/login", headers=_headers(),
                             json={"username": user, "password": pwd})
        if r.status_code != 200:
            log.warning("opensubtitles login HTTP %d", r.status_code)
            return _login_token  # keep stale token if we have one
        tok = (r.json() or {}).get("token")
        if tok:
            _login_token, _login_ts = tok, time.time()
            log.info("opensubtitles: logged in as %s", user)
        return _login_token
    except Exception as exc:  # noqa: BLE001
        log.warning("opensubtitles login failed: %s", exc)
        return _login_token


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
        token = await _ensure_login()
        params = {"query": q, "order_by": "download_count",
                  "order_direction": "desc"}
        langs = _langs_639_1(languages)
        if langs:
            params["languages"] = langs
        async with httpx.AsyncClient(timeout=_BUDGET_S) as c:
            r = await c.get(f"{_BASE}/subtitles", headers=_headers(token),
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
    (None, None) when unavailable (no API key, bad id, HTTP error,
    quota exhausted).

    v10.15.2: results are cached (repeat downloads don't burn quota)
    and the request is authenticated when OPENSUBTITLES_USERNAME /
    PASSWORD are set (free-user quota instead of ~5/day anonymous).
    """
    if not _api_key():
        return None, None
    try:
        file_id = int(sub_id)
    except (TypeError, ValueError):
        return None, None
    # v10.15.2: serve repeats from cache — zero quota cost.
    ck = str(file_id)
    hit = _dl_cache.get(ck)
    if hit and time.time() - hit[0] < _DL_CACHE_TTL_S:
        return hit[1], hit[2]
    try:
        import httpx
        token = await _ensure_login()

        async def _dl(tok: str | None):
            async with httpx.AsyncClient(timeout=_BUDGET_S) as c:
                r = await c.post(f"{_BASE}/download",
                                 headers=_headers(tok),
                                 json={"file_id": file_id})
                if r.status_code == 401 and tok:
                    return "retry", None
                if r.status_code == 403:
                    log.warning("opensubtitles: download quota exhausted "
                                "for %s (HTTP 403)", sub_id)
                    return None, None
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
                return d.content, None

        data, _ = await _dl(token)
        if data == "retry":
            # Token expired mid-flight — log in fresh and try once more.
            global _login_token
            _login_token = None
            token = await _ensure_login()
            data, _ = await _dl(token)
        if not data or data == "retry":
            return None, None
    except Exception as exc:  # noqa: BLE001
        log.warning("opensubtitles download failed for %s: %s", sub_id, exc)
        return None, None
    if b"-->" not in data[:2000]:
        log.warning("subtitle %s didn't look like SRT", sub_id)
        return None, None
    name = f"subtitle_{file_id}.srt"
    # v10.15.2: cache it (cap the dict).
    _dl_cache[ck] = (time.time(), data, name)
    while len(_dl_cache) > _DL_CACHE_CAP:
        oldest = min(_dl_cache, key=lambda k: _dl_cache[k][0])
        _dl_cache.pop(oldest, None)
    return data, name
