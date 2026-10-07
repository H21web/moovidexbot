"""Title enrichment for search results (v9.2).

Pipeline:

1. Movie info the main way: web-search API -> ``imdb.com/title/ttXXXXXXX``
   -> TMDB ``/find`` -> poster, plot, rating, year, genres.
2. Fallback: plain TMDB title search.
3. Still nothing -> AI identifies the title from its own knowledge
   (title/year ONLY — the search-API response is never fed to the AI).
   Replies ``NONE`` unless confident; no poster from this path.
4. Everything failed -> ``None`` -> caller shows files without info header.

Returns a dict ``{title, year, plot, rating, poster_url, genres, imdb_id,
source}`` or ``None``. ``source`` is one of ``"tmdb_imdb"``,
``"tmdb_search"``.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from app import tmdb
from app.config import settings

log = logging.getLogger(__name__)

_TTL = 60 * 60 * 6  # 6h in-process cache
# P1#15: key includes the year — ("kgf", 2018) and ("kgf", 2022) are
# different lookups.
_cache: dict[tuple[str, int | None], tuple[float, dict | None]] = {}

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """P2#32: one shared client (tmdb._get_client pattern) — no fresh
    client per websearch call."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_connections=20,
                                max_keepalive_connections=10),
        )
    return _client


# --- main pipeline ----------------------------------------------------------
# v10.2: the two slow lookups (web-search -> imdb id, TMDB title search)
# now run CONCURRENTLY instead of sequentially — roughly halves the
# time-to-poster on a cold cache.
async def enrich_title(keywords: str, year: int | None = None,
                       user_id: int | None = None) -> dict | None:
    """Run the enrichment pipeline for a keyword query.

    v10.8.7: JustWatch API first (fast, one call) -> imdbId -> TMDB
    for authoritative metadata. Falls back to plain TMDB title
    search, then to a JustWatch-only card (title/year/images) when
    TMDB is unavailable.
    """
    keywords = (keywords or "").strip()
    if not keywords:
        return None
    cache_key = (keywords.lower(), year)
    hit = _cache.get(cache_key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]

    jw_items = await justwatch_titles(keywords, limit=3)
    jw = jw_items[0] if jw_items else None
    imdb_id = (jw or {}).get("imdb_id")

    meta: dict | None = None
    if imdb_id:
        meta = await tmdb.find_by_imdb(imdb_id)
        if meta:
            meta["source"] = "tmdb_imdb"
    if meta is None:
        tmdb_meta = await tmdb.get_movie(keywords, year)
        if tmdb_meta:
            meta = dict(tmdb_meta)
            meta["imdb_id"] = imdb_id
            meta["source"] = "tmdb_search"
    if meta is None and jw:
        # No TMDB hit (key missing?) — JustWatch-only card still
        # shows title/year + backdrop preview.
        meta = {"title": jw["title"], "year": jw.get("year"),
                "rating": None, "plot": "", "poster_url": jw.get("poster"),
                "genres": [], "imdb_id": imdb_id,
                "kind": "tv" if jw.get("type") == "series" else "movie",
                "source": "justwatch"}

    if meta is not None:
        # JustWatch images: backdrop (1920px) is the cinematic pick
        # for the title-link preview; poster fills in when missing.
        if jw:
            meta.setdefault("backdrop_url", jw.get("backdrop"))
            if not meta.get("poster_url"):
                meta["poster_url"] = jw.get("poster")
        _cache[cache_key] = (time.time(), meta)
    # P3#17: evict the oldest ~100 instead of nuking the whole cache.
    if len(_cache) > 500:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return meta



# --- Direct JustWatch GraphQL (v10.12.5, primary) -------------------------
# simple-justwatch-python-api hits apis.justwatch.com directly — no
# middleman wrapper. Sync library, so calls go through to_thread.
# Falls back to the iamidiotareyoutoo wrapper on any failure.
def _jw_direct_sync(query: str, limit: int) -> list[dict]:
    from simplejustwatchapi.justwatch import search as jw_search
    entries = jw_search(query[:100], country="IN", language="en",
                        count=min(max(limit, 3), 8))
    out: list[dict] = []
    for e in entries or []:
        title = (e.title or "").strip()
        if not title or len(title) > 120:
            continue
        typ = "series" if (e.object_type or "").upper() == "SHOW" else "movie"
        backdrops = list(e.backdrops or [])
        out.append({
            "title": title,
            "year": e.release_year,
            "type": typ,
            "reason": "justwatch-direct",
            "imdb_id": (e.imdb_id or "").strip() or None,
            "tmdb_id": getattr(e, "tmdb_id", None),
            "backdrop": backdrops[-1] if backdrops else None,
            "poster": e.poster,
        })
        if len(out) >= limit:
            break
    return out


async def _justwatch_direct_titles(query: str, limit: int) -> list[dict]:
    """Title candidates via direct JustWatch GraphQL. Never raises."""
    import asyncio as _aio
    try:
        out = await _aio.to_thread(_jw_direct_sync, query, limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("justwatch direct failed: %s", exc)
        return []
    # same sanity check as the wrapper path
    sane = [it for it in out if _sane_title(query, it["title"])]
    if sane:
        log.info("justwatch-direct candidates %r -> %r", query[:60],
                 [t["title"][:40] for t in sane])
    return sane


def _sane_title(query: str, title: str) -> bool:
    """Normalized containment check (shared by both JustWatch paths)."""
    nq = _norm_alnum(query)
    nt = _norm_alnum(title)
    return bool(nq and nt and (nq in nt or nt in nq))


async def _justwatch_wrapper_titles(query: str, limit: int) -> list[dict]:
    """Title candidates from the iamidiotareyoutoo JustWatch wrapper.

    Fallback when the direct GraphQL path fails. Never raises.
    """
    # https://imdb.iamidiotareyoutoo.com/justwatch?q=<query>&L=en_IN
    _JW_BASE = "https://imdb.iamidiotareyoutoo.com/justwatch"
    q = (query or "").strip()
    try:
        r = await _get_client().get(_JW_BASE,
                                    params={"q": q[:100], "L": "en_IN"})
        r.raise_for_status()
        data = r.json() or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("justwatch wrapper failed: %s", exc)
        return []
    if not data.get("ok"):
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in (data.get("description") or []):
        title = (item.get("title") or "").strip()
        if not title or len(title) > 120:
            continue
        # Sanity: query and title must contain one another once
        # normalized ("spiderman" in "theamazingspiderman"). The API
        # doesn't fuzzy-match, so junk like "kerma" -> "Hum Dil De
        # Chuke Sanam" is rejected here.
        if not _sane_title(q, title):
            continue
        try:
            year = int(item.get("year")) if item.get("year") else None
        except (TypeError, ValueError):
            year = None
        typ = ("series" if (item.get("type") or "").upper() == "SHOW"
               else "movie")
        key = (title.lower(), typ)
        if key in seen:
            continue
        seen.add(key)
        photos = item.get("photo_url") or []
        drops = item.get("backdrops") or []
        out.append({"title": title, "year": year, "type": typ,
                    "reason": "justwatch",
                    "imdb_id": (item.get("imdbId") or "").strip() or None,
                    "backdrop": drops[-1] if drops else None,
                    "poster": photos[0] if photos else None})
        if len(out) >= limit:
            break
    if out:
        log.info("justwatch-wrapper candidates %r -> %r", q[:60],
                 [t["title"][:40] for t in out])
    return out


def _norm_alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# v10.8.9: cache JustWatch answers 6h — repeated zero-result searches
# reuse them with zero HTTP calls.
_JW_TTL = 60 * 60 * 6
_jw_cache: dict[str, tuple[float, list[dict]]] = {}


def _jw_cache_get(q: str) -> list[dict] | None:
    hit = _jw_cache.get((q or "").strip().lower())
    if hit and time.time() - hit[0] < _JW_TTL:
        return hit[1]
    return None


def _jw_cache_put(q: str, items: list[dict]) -> None:
    key = (q or "").strip().lower()
    if not key:
        return
    _jw_cache[key] = (time.time(), items)
    if len(_jw_cache) > 500:
        for k in sorted(_jw_cache, key=lambda k: _jw_cache[k][0])[:100]:
            _jw_cache.pop(k, None)


async def justwatch_titles(query: str, limit: int = 5) -> list[dict]:
    """Title candidates from JustWatch.

    v10.12.5: direct GraphQL (simple-justwatch-python-api) first,
    iamidiotareyoutoo wrapper as fallback.

    Returns ``[{title, year, type, imdb_id, backdrop, poster}]`` —
    type is ``"movie"``/``"series"``; ``backdrop``/``poster`` are
    JustWatch image URLs (backdrop preferred for previews).
    Results are sanity-checked against the query (normalized
    containment) so unrelated API hits are dropped. Empty list on
    failure. Never raises.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    cached = _jw_cache_get(q)
    if cached is not None:
        log.debug("justwatch cache hit for %r", q[:50])
        return cached[:limit]
    out = await _justwatch_direct_titles(q, limit)
    if not out:
        out = await _justwatch_wrapper_titles(q, limit)
    _jw_cache_put(q, out)
    return out[:limit]
