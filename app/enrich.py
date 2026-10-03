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

import asyncio
import json
import logging
import re

import httpx

from app import tmdb
from app.config import settings

log = logging.getLogger(__name__)

_IMDB_RE = re.compile(r"imdb\.com/title/(tt\d{7,8})", re.IGNORECASE)
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

# --- search API -----------------------------------------------------------
async def _websearch_raw(query: str, num: int = 6) -> list[dict] | None:
    """Raw ``/search`` call. ``None`` on failure."""
    base = (settings.WEBSEARCH_API_URL or "").rstrip("/")
    if not base:
        return None
    try:
        r = await _get_client().get(f"{base}/search",
                                    params={"q": query[:200], "num": num})
        r.raise_for_status()
        return (r.json() or {}).get("results") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("enrich websearch failed: %s", exc)
        return None


async def _imdb_id_via_websearch(keywords: str) -> str | None:
    """Find an IMDB title id through the free web-search API."""
    results = await _websearch_raw(f"{keywords} imdb")
    if not results:
        return None
    for item in results:
        url = (item.get("url") or "")
        m = _IMDB_RE.search(url)
        if m:
            return m.group(1)
        # sometimes the id hides in the snippet/title
        m = _IMDB_RE.search((item.get("title") or "") + " " +
                            (item.get("snippet") or ""))
        if m:
            return m.group(1)
    return None


# --- main pipeline ----------------------------------------------------------
# v10.2: the two slow lookups (web-search -> imdb id, TMDB title search)
# now run CONCURRENTLY instead of sequentially — roughly halves the
# time-to-poster on a cold cache.
async def enrich_title(keywords: str, year: int | None = None,
                       user_id: int | None = None) -> dict | None:
    """Run the enrichment pipeline for a keyword query."""
    import time
    keywords = (keywords or "").strip()
    if not keywords:
        return None
    cache_key = (keywords.lower(), year)
    hit = _cache.get(cache_key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]

    # 1-2. web-search -> imdb id -> TMDB, and plain TMDB title search,
    # raced in parallel; the imdb-anchored result wins when present.
    imdb_id, tmdb_meta = await asyncio.gather(
        _imdb_id_via_websearch(keywords),
        tmdb.get_movie(keywords, year),
    )
    meta: dict | None = None
    if imdb_id:
        meta = await tmdb.find_by_imdb(imdb_id)
        if meta:
            meta["source"] = "tmdb_imdb"
    if meta is None and tmdb_meta:
        meta = dict(tmdb_meta)
        meta["imdb_id"] = imdb_id
        meta["source"] = "tmdb_search"

    if meta is not None:
        _cache[cache_key] = (time.time(), meta)
    # P3#17: evict the oldest ~100 instead of nuking the whole cache.
    if len(_cache) > 500:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return meta


# --- 2nd search logic: title parsed from web-search results ------------------
# v10.2: when the DB has nothing for a query, the free web-search API is
# asked for "<query> movie" and the canonical title is parsed out of the
# top results (IMDb / Wikipedia result titles look like
# "Avengers: Endgame (2019) - IMDb"). That title is then searched in the
# DB — a second chance before AI is consulted.
_SITE_SUFFIX_RE = re.compile(
    r"\s*[-|–—:]\s*(IMDb|Wikipedia|Rotten Tomatoes|IMDB|Letterboxd).*$",
    re.IGNORECASE,
)
# v10.7: bing_html results carry many site names — strip " | Site",
# " - Site" for the common ones, then a trailing year / "Movie".
_PIPE_SUFFIX_RE = re.compile(r"\s*\|\s*[^|]+$")
_DASH_SITE_RE = re.compile(
    r"\s+[-–—]\s+(wikipedia|imdb|rotten tomatoes|letterboxd|allocin\u00e9|"
    r"moviefone|netflix|youtube|senscritique|roger ?ebert|mubi|"
    r"prime video|disney|hotstar|jio|eros ?now|zee5|sonyliv|voot|"
    r"convertworld|dictionnaire|film).*$",
    re.IGNORECASE,
)
_TRAIL_MOVIE_RE = re.compile(
    r"\s+(movie|film|full(\s+hd)?|hd|extended(\s+edition)?|"
    r"director'?s\s+cut|unrated)\s*$", re.IGNORECASE)
_YEAR_PAREN_RE = re.compile(r"\s*\((?:19|20)\d{2}[^)]*\)\s*$")
_FILM_SUFFIX_RE = re.compile(r"\s*\(\s*film\s*\)\s*$", re.IGNORECASE)


def _clean_web_title(raw: str) -> str | None:
    """Turn a search-result title into a plain movie/series title."""
    t = (raw or "").strip()
    if not t:
        return None
    t = _PIPE_SUFFIX_RE.sub("", t)
    t = _DASH_SITE_RE.sub("", t)
    t = _SITE_SUFFIX_RE.sub("", t)
    t = _TRAIL_MOVIE_RE.sub("", t)
    t = _TRAIL_MOVIE_RE.sub("", t)
    t = _YEAR_PAREN_RE.sub("", t)
    t = _FILM_SUFFIX_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—:|")
    if len(t) < 2 or len(t) > 120:
        return None
    # Skip navigational junk ("IMDb", "Watch ... online").
    low = t.lower()
    if low in {"imdb", "wikipedia"} or low.startswith(("watch ", "download ")):
        return None
    return t


# --- smart title candidates (v10.7 rework) --------------------------------
# The old code took the FIRST Wikipedia/IMDb hit blindly — for
# "avengers endgame" Bing returns the franchise page first, so the bot
# "corrected" to "Avengers (Marvel Cinematic Universe)" and searched
# the DB for the wrong title. Now every result title is scored against
# the user's query and junk pages are rejected outright.
_TITLE_JUNK_RE = re.compile(
    r"\((film series|film franchise|franchise|disambiguation|saga)\)",
    re.IGNORECASE)
_QUERY_STOP = {"movie", "film", "full", "hd", "watch", "online",
              "download", "new", "latest", "tamil", "hindi", "telugu",
              "malayalam", "english"}
_CANDIDATE_MIN_SCORE = 0.35


def _word_set(text: str) -> set[str]:
    # "K.G.F" -> "kgf" so acronym titles match plain queries
    t = re.sub(r"(?<=[a-z0-9])\.(?=[a-z0-9])", "", (text or "").lower())
    return set(re.findall(r"[a-z0-9]+", t))


def _candidate_score(query: str, title: str, url: str) -> float:
    """How well does this result title match the user's query?

    Word overlap drives the score; extra junk words in the title
    penalize it; IMDb title pages (specific movies) get a bonus.
    """
    qw = _word_set(query) - _QUERY_STOP
    tw = _word_set(title) - {"movie", "film", "full"}
    if not qw or not tw:
        return 0.0
    overlap = len(qw & tw) / len(qw)
    extra = len(tw - qw) / len(tw)
    score = overlap - 0.15 * extra
    if "imdb.com/title/tt" in (url or ""):
        score += 0.25  # an IMDb title page is a specific movie
    return score


async def web_title_candidates(query: str, limit: int = 5,
                                 sid: str | None = None) -> list[str]:
    """Best-guess movie/series titles from web-search results.

    Returns cleaned titles ordered best-first, each verified to
    actually resemble the user's query. Empty list when nothing
    usable. Never raises.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    results = await _websearch_raw(f"{q} movie", num=8)
    if not results:
        return []
    scored: list[tuple[float, str]] = []
    for item in results:
        title = _clean_web_title(item.get("title") or "")
        if not title:
            continue
        if _TITLE_JUNK_RE.search(title):
            continue  # franchise / series / disambiguation page
        s = _candidate_score(q, title, item.get("url") or "")
        if s >= _CANDIDATE_MIN_SCORE:
            scored.append((s, title))
    scored.sort(key=lambda x: -x[0])
    out: list[str] = []
    seen: set[str] = set()
    for _, t in scored:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
        if len(out) >= limit:
            break
    if out:
        log.info("[s:%s] web candidates %r -> %r", sid or "-",
                 q[:60], [t[:40] for t in out])
    return out


async def parse_title_from_web(query: str) -> str | None:
    """Legacy single-title wrapper — first candidate or ``None``."""
    cands = await web_title_candidates(query, limit=1)
    return cands[0] if cands else None
