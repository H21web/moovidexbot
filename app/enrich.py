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




# --- web title candidates (v10.8.4) -------------------------------------------
# When the DB has nothing for a query, the free web-search API is asked
# for "<query> movie" BEFORE Groq AI is consulted — this saves Groq
# tokens (the free tier is only 200K/day). Each result title is cleaned
# (site suffixes, year, trailing junk) and scored against the query;
# junk pages (franchise / disambiguation) are rejected outright.
_SITE_SUFFIX_RE = re.compile(
    r"\s*[-|–—:]\s*(IMDb|Wikipedia|Rotten Tomatoes|IMDB|Letterboxd).*$",
    re.IGNORECASE,
)
_PIPE_SUFFIX_RE = re.compile(r"\s*\|\s*[^|]+$")
_DASH_SITE_RE = re.compile(
    r"\s+[-–—]\s+(wikipedia|imdb|rotten tomatoes|letterboxd|"
    r"moviefone|netflix|youtube|prime video|disney|hotstar|jio|"
    r"eros ?now|zee5|sonyliv|voot|mubi|film).*",
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
    low = t.lower()
    if low in {"imdb", "wikipedia"} or low.startswith(("watch ",
                                                       "download ")):
        return None
    return t


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
    Fallback: character-level similarity so pure-typo queries
    ("kerma" -> "Karma") aren't rejected when the search engine
    already fuzzy-matched them.
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
    if score < _CANDIDATE_MIN_SCORE and len(query.strip()) >= 4:
        import difflib
        seq = difflib.SequenceMatcher(None, query.lower(),
                                      title.lower()).ratio()
        if seq >= 0.65:
            score = max(score, seq * 0.8)
    return score


async def web_title_candidates(query: str, limit: int = 3) -> list[str]:
    """Best-guess movie/series titles from the web-search API.

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
        log.info("web title candidates %r -> %r", q[:60],
                 [t[:40] for t in out])
    return out


# --- JustWatch title API (v10.8.5) --------------------------------------------
# https://imdb.iamidiotareyoutoo.com/justwatch?q=<query>&L=en_IN
# Clean structured titles (no scraping): title, year, type (MOVIE/SHOW),
# posters. Used BEFORE the web-search API and Groq — free and fast.
# Note: this API does NOT fuzzy-match typos ("kerma" returns unrelated
# titles), so results are sanity-checked against the query; the
# web-search stage stays as the typo-tolerant fallback.
_JW_BASE = "https://imdb.iamidiotareyoutoo.com/justwatch"


def _norm_alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


async def justwatch_titles(query: str, limit: int = 5) -> list[dict]:
    """Title candidates from the JustWatch API.

    Returns ``[{title, year, type}]`` — type is ``"movie"``/``"series"``.
    Results are sanity-checked against the query (normalized
    containment) so unrelated API hits are dropped. Empty list on
    failure. Never raises.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    try:
        r = await _get_client().get(_JW_BASE,
                                    params={"q": q[:100], "L": "en_IN"})
        r.raise_for_status()
        data = r.json() or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("justwatch api failed: %s", exc)
        return []
    if not data.get("ok"):
        return []
    nq = _norm_alnum(q)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in (data.get("description") or []):
        title = (item.get("title") or "").strip()
        if not title or len(title) > 120:
            continue
        nt = _norm_alnum(title)
        # Sanity: query and title must contain one another once
        # normalized ("spiderman" in "theamazingspiderman"). The API
        # doesn't fuzzy-match, so junk like "kerma" -> "Hum Dil De
        # Chuke Sanam" is rejected here.
        if not nq or not nt or (nq not in nt and nt not in nq):
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
        out.append({"title": title, "year": year, "type": typ,
                    "reason": "justwatch"})
        if len(out) >= limit:
            break
    if out:
        log.info("justwatch candidates %r -> %r", q[:60],
                 [t["title"][:40] for t in out])
    return out
