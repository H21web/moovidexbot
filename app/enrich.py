"""Title enrichment for search results (v8.7).

Pipeline:

1. Movie info the old way: web-search API -> ``imdb.com/title/ttXXXXXXX``
   -> TMDB ``/find`` -> poster, plot, rating, year, genres.
2. Fallback: plain TMDB title search.
3. Still nothing -> AI gets ONLY the web-search API response as context
   (grounded, never from its own memory) and extracts movie data from it.
4. That too fails -> AI parses the actual query into a better search
   query -> one more browse round on the search API -> meta from results.
5. Everything fell back -> ``None`` -> caller shows "no results".

Returns a dict ``{title, year, plot, rating, poster_url, genres, imdb_id,
source}`` or ``None``. ``source`` is one of ``"tmdb_imdb"``,
``"tmdb_search"``, ``"groq_websearch"``, ``"websearch_retry"``.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from app import tmdb
from app.config import settings

log = logging.getLogger(__name__)

_IMDB_RE = re.compile(r"imdb\.com/title/(tt\d{7,8})", re.IGNORECASE)
_YEAR_RE = re.compile(r"\((19\d{2}|20\d{2})\)")
_TTL = 60 * 60 * 6  # 6h in-process cache
_cache: dict[str, tuple[float, dict | None]] = {}

# --- AI prompts -----------------------------------------------------------
GROQ_GROUNDED_SYSTEM = (
    "You extract movie/series data ONLY from the web search results below. "
    "Reply with ONLY a JSON object, no other text: "
    '{"title": "...", "year": 2022, "plot": "1-2 sentence plot", '
    '"rating": 7.5, "genres": ["Action", "Drama"]}. '
    "Use ONLY facts present in the results — never invent anything. "
    "If the results do not identify a movie or series, reply exactly: NONE. "
    "rating is 0-10."
)

GROQ_REQUERY_SYSTEM = (
    "You turn a messy movie/series query into the single best web-search "
    "query to find that movie/series (title + year if known). "
    "Reply with ONLY the search query, no other text."
)


# --- search API -----------------------------------------------------------
async def _websearch_raw(query: str, num: int = 6) -> list[dict] | None:
    """Raw ``/search`` call. ``None`` on failure."""
    base = (settings.WEBSEARCH_API_URL or "").rstrip("/")
    if not base:
        return None
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0)) as c:
            r = await c.get(f"{base}/search",
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


# --- result -> meta helpers ------------------------------------------------
def _clean_result_title(raw: str) -> tuple[str, int | None]:
    """``"KGF: Chapter 2 (2022) - IMDb"`` -> ``("KGF: Chapter 2", 2022)``."""
    t = (raw or "").strip()
    t = re.sub(r"\s*[-–|]\s*IMDb\s*$", "", t, flags=re.IGNORECASE).strip()
    year: int | None = None
    m = _YEAR_RE.search(t)
    if m:
        year = int(m.group(1))
        t = (t[:m.start()] + t[m.end():]).strip()
    t = re.sub(r"\s+", " ", t).strip(" -–|")
    return t, year


def _looks_relevant(title: str, snippet: str, keywords: str) -> bool:
    """Lenient accuracy check: any query token (3+ chars) in title/snippet."""
    hay = f"{title} {snippet}".lower()
    for tok in re.findall(r"[a-z0-9]{3,}", keywords.lower()):
        if tok in hay:
            return True
    return False


def _meta_from_results(results: list[dict] | None, keywords: str,
                       source: str) -> dict | None:
    """Build a meta dict from raw search-API results (no TMDB)."""
    if not results:
        return None
    chosen: dict | None = None
    for item in results:
        if _IMDB_RE.search(item.get("url") or ""):
            chosen = item
            break
    if chosen is None:
        chosen = results[0]
    title, year = _clean_result_title(chosen.get("title") or "")
    snippet = (chosen.get("snippet") or "").strip()
    if not title or not _looks_relevant(title, snippet, keywords):
        return None
    imdb_m = _IMDB_RE.search(chosen.get("url") or "")
    return {
        "title": title,
        "year": year,
        "plot": snippet[:300],
        "rating": 0.0,
        "poster_url": None,
        "genres": [],
        "imdb_id": imdb_m.group(1) if imdb_m else None,
        "source": source,
    }


# --- AI steps --------------------------------------------------------------
async def _groq_from_websearch(keywords: str,
                               results: list[dict]) -> dict | None:
    """AI extracts movie data using ONLY the search API response (no memory).

    Returns ``None`` when the data doesn't identify a movie/series.
    """
    from app import ai as ai_mod
    ctx = "\n".join(
        f"[{i + 1}] {(x.get('title') or '').strip()}"
        + (f": {(x.get('snippet') or '').strip()}" if x.get("snippet") else "")
        + f" ({x.get('url') or ''})"
        for i, x in enumerate(results[:6]))
    raw = await ai_mod.groq_complete(
        GROQ_GROUNDED_SYSTEM,
        f"Query: {keywords[:150]}\n\nWeb search results:\n{ctx}",
        max_tokens=300,
    )
    if not raw or "NONE" in raw.upper():
        return None
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        data = json.loads(raw[start:end])
    except Exception:
        return None
    title = (data.get("title") or "").strip()
    if not title:
        return None
    try:
        rating = round(float(data.get("rating") or 0), 1)
    except (TypeError, ValueError):
        rating = 0.0
    return {
        "title": title,
        "year": data.get("year"),
        "plot": (data.get("plot") or "").strip(),
        "rating": rating,
        "poster_url": None,
        "genres": [str(g) for g in (data.get("genres") or [])][:3],
        "imdb_id": None,
        "source": "groq_websearch",
    }


async def _ai_requery(keywords: str) -> str | None:
    """AI parses the actual query into a better browse-search query."""
    from app import ai as ai_mod
    raw = await ai_mod.groq_complete(
        GROQ_REQUERY_SYSTEM,
        f"Movie query: {keywords[:150]}",
        max_tokens=60,
    )
    q = (raw or "").strip().strip("\"'")[:120]
    return q or None


# --- main pipeline ----------------------------------------------------------
async def enrich_title(keywords: str, year: int | None = None,
                       user_id: int | None = None) -> dict | None:
    """Run the enrichment pipeline for a keyword query."""
    import time
    keywords = (keywords or "").strip()
    if not keywords:
        return None
    hit = _cache.get(keywords.lower())
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]

    from app import ai as ai_mod
    ai_on = ai_mod.is_configured()

    # 1-2. the old way: search API -> imdb id -> TMDB; then TMDB search.
    meta: dict | None = None
    imdb_id = await _imdb_id_via_websearch(keywords)
    if imdb_id:
        meta = await tmdb.find_by_imdb(imdb_id)
        if meta:
            meta["source"] = "tmdb_imdb"
    if meta is None:
        meta = await tmdb.get_movie(keywords, year)
        if meta:
            meta = dict(meta)
            meta["imdb_id"] = imdb_id
            meta["source"] = "tmdb_search"

    # 3. AI with search-API data ONLY (grounded — never from AI memory).
    if meta is None and ai_on and user_id is not None:
        if await ai_mod.quota_remaining(user_id) > 0:
            results = await _websearch_raw(f"{keywords} movie")
            if results:
                meta = await _groq_from_websearch(keywords, results)
                if meta:
                    await ai_mod.quota_use(user_id)

    # 4. AI parses the actual query -> one more browse round.
    if meta is None and ai_on and user_id is not None:
        if await ai_mod.quota_remaining(user_id) > 0:
            better_q = await _ai_requery(keywords)
            await ai_mod.quota_use(user_id)
            if better_q and better_q.lower() != keywords.lower():
                results = await _websearch_raw(better_q)
                meta = _meta_from_results(results, better_q,
                                          "websearch_retry")

    _cache[keywords.lower()] = (time.time(), meta)
    # keep the cache small
    if len(_cache) > 500:
        _cache.clear()
    return meta
