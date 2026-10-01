"""TMDB metadata with a 30-day PostgreSQL cache.

Lightning-fast by design: a cache hit costs one indexed PK lookup and zero
API calls. Without a TMDB_API_KEY everything degrades gracefully to None and
the bot falls back to filename-derived info.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import get_session_factory
from app.models import TmdbCache
from app.textutil import extract_year, title_key

log = logging.getLogger(__name__)

CACHE_TTL = timedelta(days=30)
BASE_URL = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/w500"

_client: httpx.AsyncClient | None = None
# in-flight dedupe: cache_key -> asyncio task result not needed; small guard
_inflight: dict[str, float] = {}

GENRE_MAP = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy",
    80: "Crime", 99: "Documentary", 18: "Drama", 10751: "Family",
    14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
    9648: "Mystery", 10749: "Romance", 878: "Sci-Fi", 10770: "TV Movie",
    53: "Thriller", 10752: "War", 37: "Western",
}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"Accept": "application/json"},
        )
    return _client


def cache_key_for(title: str, year: int | None = None) -> str:
    return f"{title_key(title)}:{year or ''}"


async def get_movie(title: str, year: int | None = None) -> dict | None:
    """Return TMDB metadata dict or None.

    Dict: title, year, rating, plot, poster_url, genres (list of str).
    Cached in Postgres for 30 days; no API key -> None (graceful).
    """
    if not settings.TMDB_API_KEY:
        return None
    clean = title.strip()
    if not clean:
        return None
    key = cache_key_for(clean, year)
    factory = get_session_factory(settings.DATABASE_URL)

    async with factory() as session:
        row = await session.get(TmdbCache, key)
        if row and row.cached_at:
            cached_at = row.cached_at
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - cached_at < CACHE_TTL:
                return dict(row.payload)

    # simple in-flight guard so concurrent taps don't stampede TMDB
    now = time.time()
    if now - _inflight.get(key, 0) < 30:
        return None

    try:
        _inflight[key] = now
        payload = await _fetch_from_tmdb(clean, year)
    except Exception as exc:  # noqa: BLE001 - TMDB must never break the bot
        log.warning("TMDB lookup failed for %r: %s", clean, exc)
        return None
    finally:
        _inflight.pop(key, None)

    if payload:
        async with factory() as session:
            row = await session.get(TmdbCache, key)
            if row:
                row.payload = payload
                row.cached_at = datetime.now(timezone.utc)
            else:
                session.add(TmdbCache(key=key, payload=payload))
            await session.commit()
    return payload


async def find_by_imdb(imdb_id: str) -> dict | None:
    """Look up a title on TMDB by its IMDB id (``tt1234567``).

    Returns the same dict shape as ``get_movie``. ``None`` when the key
    is missing, TMDB errors, or no match exists.
    """
    if not settings.TMDB_API_KEY or not imdb_id:
        return None
    key = f"imdb:{imdb_id.strip()}"
    try:
        async with get_session_factory()() as session:
            row = await session.get(TmdbCache, key)
            if row and datetime.now(timezone.utc) - row.cached_at < CACHE_TTL:
                return dict(row.payload or {})
    except Exception:
        log.debug("tmdb imdb cache read failed", exc_info=True)
    try:
        resp = await _get_client().get(
            f"/find/{imdb_id.strip()}",
            params={
                "api_key": settings.TMDB_API_KEY,
                "external_source": "imdb_id",
                "language": "en-US",
            },
        )
        resp.raise_for_status()
        data = resp.json() or {}
        movies = data.get("movie_results") or []
        tvs = data.get("tv_results") or []
        if movies:
            m, kind, date_key, title_key_ = movies[0], "movie", "release_date", "title"
        elif tvs:
            m, kind, date_key, title_key_ = tvs[0], "tv", "first_air_date", "name"
        else:
            return None
        poster_path = m.get("poster_path")
        payload = {
            "title": m.get(title_key_) or imdb_id,
            "year": extract_year(m.get(date_key) or ""),
            "rating": round(float(m.get("vote_average") or 0), 1),
            "plot": (m.get("overview") or "").strip(),
            "poster_url": f"{POSTER_BASE}{poster_path}" if poster_path else None,
            "genres": [GENRE_MAP.get(g, str(g)) for g in (m.get("genre_ids") or [])][:3],
            "imdb_id": imdb_id,
            "kind": kind,
        }
    except Exception:
        log.warning("tmdb find_by_imdb failed for %s", imdb_id, exc_info=True)
        return None
    try:
        async with get_session_factory()() as session:
            row = await session.get(TmdbCache, key)
            if row:
                row.payload = payload
                row.cached_at = datetime.now(timezone.utc)
            else:
                session.add(TmdbCache(key=key, payload=payload))
            await session.commit()
    except Exception:
        log.debug("tmdb imdb cache write failed", exc_info=True)
    return payload


async def _fetch_from_tmdb(title: str, year: int | None) -> dict | None:
    params = {
        "api_key": settings.TMDB_API_KEY,
        "query": title,
        "include_adult": "false",
        "language": "en-US",
        "page": 1,
    }
    if year:
        params["year"] = str(year)
    resp = await _get_client().get("/search/movie", params=params)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None
    m = results[0]
    poster_path = m.get("poster_path")
    release = m.get("release_date") or ""
    return {
        "title": m.get("title") or title,
        "year": extract_year(release),
        "rating": round(float(m.get("vote_average") or 0), 1),
        "plot": (m.get("overview") or "").strip(),
        "poster_url": f"{POSTER_BASE}{poster_path}" if poster_path else None,
        "genres": [GENRE_MAP.get(g, str(g)) for g in (m.get("genre_ids") or [])][:3],
    }


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


GENRE_NAME_TO_ID = {v.lower(): k for k, v in GENRE_MAP.items()}


async def search_title(raw: str) -> dict | None:
    """Raw TMDB title search (movie, then TV) for "did you mean?" correction.

    Returns ``{"title", "year", "kind"}`` or None. Cached 30 days.
    """
    if not settings.TMDB_API_KEY:
        return None
    clean = (raw or "").strip()
    if len(clean) < 2:
        return None
    key = f"searchtitle:{title_key(clean)}"
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        row = await session.get(TmdbCache, key)
        if row and row.payload:
            return dict(row.payload)

    payload: dict | None = None
    try:
        for endpoint, kind in (("/search/movie", "movie"),
                               ("/search/tv", "series")):
            resp = await _get_client().get(
                endpoint,
                params={"api_key": settings.TMDB_API_KEY, "query": clean,
                        "include_adult": "false", "language": "en-US",
                        "page": 1})
            resp.raise_for_status()
            results = resp.json().get("results") or []
            if results:
                m = results[0]
                rd = m.get("release_date") or m.get("first_air_date") or ""
                payload = {
                    "title": m.get("title") or m.get("name") or clean,
                    "year": extract_year(rd),
                    "kind": kind,
                    "tmdb_id": m.get("id"),
                }
                break
    except Exception as exc:  # noqa: BLE001
        log.warning("TMDB title search failed for %r: %s", clean, exc)
        return None

    if payload:
        try:
            async with factory() as session:
                session.add(TmdbCache(key=key, payload=payload))
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("tmdb title cache write failed: %s", exc)
    return payload


async def discover(genre: str | None = None, year: int | None = None,
                   limit: int = 8) -> list[dict]:
    """TMDB discover: popular titles for a genre/year (for AI genre browse).

    Returns ``[{"title", "year"}]``. Cached 30 days.
    """
    if not settings.TMDB_API_KEY:
        return []
    gid = GENRE_NAME_TO_ID.get((genre or "").strip().lower()) if genre else None
    key = f"discover:{gid or ''}:{year or ''}:{limit}"
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        row = await session.get(TmdbCache, key)
        if row and row.payload:
            return list(row.payload.get("items", []))

    items: list[dict] = []
    try:
        params: dict = {"api_key": settings.TMDB_API_KEY,
                        "language": "en-US", "page": 1,
                        "sort_by": "popularity.desc",
                        "include_adult": "false"}
        if gid:
            params["with_genres"] = str(gid)
        if year:
            params["primary_release_year"] = str(year)
        resp = await _get_client().get("/discover/movie", params=params)
        resp.raise_for_status()
        for m in (resp.json().get("results") or [])[:limit]:
            items.append({"title": m.get("title") or "",
                          "year": extract_year(m.get("release_date") or "")})
        items = [i for i in items if i["title"]]
    except Exception as exc:  # noqa: BLE001
        log.warning("TMDB discover failed: %s", exc)
        return []

    if items:
        try:
            async with factory() as session:
                session.add(TmdbCache(key=key, payload={"items": items}))
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("tmdb discover cache write failed: %s", exc)
    return items


# ---------------------------------------------------------------------------
# Clean title resolution: never send raw user questions to TMDB.
# ---------------------------------------------------------------------------

_TITLE_Q_HEAD = {
    "entha", "enth", "evide", "eppol", "eppo", "aara", "aar", "ethra",
    "engine", "enthina", "enthelum", "what", "whats", "which", "who",
    "when", "where", "is", "are",
}
_TITLE_Q_TAIL = {
    "undo", "aano", "alle", "aakumo", "aakum", "lloo", "aayi",
    "aayirunno", "aayirunnu",
}
_TITLE_NOISE = {
    "movie", "movies", "cinema", "cinemayude", "padam", "padathinte",
    "film", "films", "filminte", "series", "serial", "show", "shows",
    "trailer", "teaser", "song", "songs", "review", "reviews", "story",
    "download", "watch", "online", "puthiya", "pazhaya", "latest",
    "new", "illa", "vannittundo", "release", "rilis", "kazhinjo",
}


def _strip_punct(w: str) -> str:
    return w.strip("?.!,;:'\"\"()[]")


def extract_title_candidate(query: str) -> str:
    """Pull a probable movie/series title out of a question-like query.

    "Kgf movie undo" -> "Kgf"; "kgf 2 trailer undo?" -> "kgf 2".
    Returns "" when nothing title-like remains.
    """
    words = (query or "").strip().rstrip("?").strip().split()
    while words and _strip_punct(words[0]).lower() in _TITLE_Q_HEAD:
        words.pop(0)
    while words and _strip_punct(words[-1]).lower() in _TITLE_Q_TAIL:
        words.pop()
    words = [w for w in words if _strip_punct(w).lower() not in _TITLE_NOISE]
    return " ".join(words).strip()


def _clean_web_title(title: str) -> str:
    """'K.G.F: Chapter 2 (2022) - IMDb' -> 'K.G.F: Chapter 2'."""
    t = (title or "").strip()
    t = re.sub(
        r"\s*[-–—|]\s*"
        r"(imdb|wikipedia|prime video|netflix|youtube|rotten tomatoes"
        r"|hotstar|jiocinema|sonyliv|zee5).*$",
        "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*\(\d{4}\)\s*$", "", t)
    t = re.sub(r"\s*[⭐️]+.*$", "", t)
    return t.strip(" -–—|")


async def _web_title_hint(query: str) -> str | None:
    """Ask the web search API; return a cleaned top-result title for TMDB."""
    base = (settings.WEBSEARCH_API_URL or "").rstrip("/")
    if not base:
        return None
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0)) as c:
            r = await c.get(f"{base}/search",
                            params={"q": query[:200], "num": 3})
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
    except Exception as exc:  # noqa: BLE001
        log.debug("web title hint failed: %s", exc)
        return None
    for item in results:
        title = _clean_web_title(str(item.get("title") or ""))
        if title and len(title) >= 2:
            return title
    return None


async def resolve_title(query: str) -> dict | None:
    """Resolve user text to a TMDB title entry.

    1. Extract a clean title candidate locally (never the raw question).
    2. TMDB search with the candidate.
    3. Fallback: web search API -> cleaned top title -> TMDB.

    Returns {"title", "year", "kind", "tmdb_id"} or None. Never raises.
    """
    try:
        candidate = extract_title_candidate(query)
        if candidate and len(candidate) >= 2:
            hit = await search_title(candidate)
            if hit:
                return hit
        web_title = await _web_title_hint(query)
        if web_title and web_title.lower() != candidate.lower():
            hit = await search_title(web_title)
            if hit:
                return hit
    except Exception as exc:  # noqa: BLE001
        log.debug("resolve_title failed for %r: %s", query, exc)
    return None
