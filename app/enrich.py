"""Title enrichment for search results: search API -> IMDB id -> TMDB.

Priority pipeline (keyword-based, no AI needed for the first three steps):

1. Query the free web-search API for ``"<keywords> imdb"`` and pull the
   first ``imdb.com/title/ttXXXXXXX`` link -> ``imdb_id``.
2. TMDB ``/find/{imdb_id}?external_source=imdb_id`` -> poster, plot,
   rating, year, genres (cached 30 days).
3. Fallback: plain TMDB title search by keywords.
4. Last resort: Groq returns a small JSON blob with the movie data
   (uses one quota unit, only when everything else failed).

Returns a dict ``{title, year, plot, rating, poster_url, genres, imdb_id,
source}`` or ``None`` when nothing could be found. ``source`` is one of
``"tmdb_imdb" | "tmdb_search" | "groq"``.
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
_TTL = 60 * 60 * 6  # 6h in-process cache
_cache: dict[str, tuple[float, dict | None]] = {}

GROQ_META_SYSTEM = (
    "You are a movie database. Reply with ONLY a JSON object, no other text: "
    '{"title": "...", "year": 2022, "plot": "1-2 sentence plot", '
    '"rating": 7.5, "genres": ["Action", "Drama"]}. '
    "Use your best knowledge; if unsure, still give your best guess. "
    "rating is 0-10."
)


async def _imdb_id_via_websearch(keywords: str) -> str | None:
    """Find an IMDB title id through the free web-search API."""
    base = (settings.WEBSEARCH_API_URL or "").rstrip("/")
    if not base:
        return None
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0)) as c:
            r = await c.get(f"{base}/search",
                            params={"q": f"{keywords[:200]} imdb", "num": 6})
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("enrich websearch failed: %s", exc)
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


async def _groq_movie_data(keywords: str, year: int | None) -> dict | None:
    """Last-resort movie data from Groq as JSON."""
    from app import ai as ai_mod
    if not ai_mod.is_configured():
        return None
    q = keywords if not year else f"{keywords} ({year})"
    raw = await ai_mod.groq_complete(
        GROQ_META_SYSTEM,
        f"Movie/series data for: {q}",
        max_tokens=300,
    )
    if not raw:
        return None
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        data = json.loads(raw[start:end])
    except Exception:
        return None
    title = (data.get("title") or keywords).strip()
    if not title:
        return None
    try:
        rating = round(float(data.get("rating") or 0), 1)
    except (TypeError, ValueError):
        rating = 0.0
    return {
        "title": title,
        "year": data.get("year") or year,
        "plot": (data.get("plot") or "").strip(),
        "rating": rating,
        "poster_url": None,
        "genres": [str(g) for g in (data.get("genres") or [])][:3],
        "imdb_id": None,
        "source": "groq",
    }


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
    if meta is None and user_id is not None:
        # Groq fallback: costs one quota unit.
        from app import ai as ai_mod
        if await ai_mod.quota_remaining(user_id) > 0:
            meta = await _groq_movie_data(keywords, year)
            if meta:
                await ai_mod.quota_use(user_id)

    _cache[keywords.lower()] = (time.time(), meta)
    # keep the cache small
    if len(_cache) > 500:
        _cache.clear()
    return meta
