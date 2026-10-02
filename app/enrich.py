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
``"tmdb_search"``, ``"groq_identify"``.
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

# --- AI prompts -----------------------------------------------------------
GROQ_IDENTIFY_SYSTEM = (
    "You identify a movie or TV series from its title. Reply with ONLY a "
    "JSON object, no other text: "
    '{"title": "...", "year": 2022, "plot": "1-2 sentence plot", '
    '"rating": 7.5, "genres": ["Action", "Drama"]}. '
    "Set year only if you are sure. rating is 0-10. "
    "If you cannot identify it with confidence, reply exactly: NONE."
)


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


# --- AI steps --------------------------------------------------------------
async def _groq_identify(keywords: str, year: int | None,
                       user_id: int) -> dict | None:
    """v9.2 AI fallback: title/year ONLY — never the search-API response.

    Returns a meta dict or ``None``. The model must reply ``NONE`` when
    unsure, so this never invents a movie. No poster from this path.
    """
    from app import ai as ai_mod
    if not ai_mod.is_configured():
        return None
    if await ai_mod.quota_remaining(user_id) <= 0:
        log.debug("enrich: AI identify skipped, no quota")
        return None
    prompt = f"Title: {keywords[:120]}" + (f" ({year})" if year else "")
    try:
        raw = await ai_mod.groq_complete(
            GROQ_IDENTIFY_SYSTEM, prompt,
            max_tokens=300, json_mode=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("enrich: AI identify failed: %s", exc)
        return None
    if not raw or raw.strip() == "NONE":
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        log.debug("enrich: AI identify bad JSON")
        return None
    if not isinstance(data, dict) or not (data.get("title") or "").strip():
        return None
    await ai_mod.quota_use(user_id)
    genres = [str(g) for g in (data.get("genres") or [])][:3]
    try:
        rating = float(data.get("rating") or 0.0)
    except (TypeError, ValueError):
        rating = 0.0
    meta = {
        "title": str(data["title"]).strip()[:120],
        "year": data.get("year") if isinstance(
            data.get("year"), int) else year,
        "plot": str(data.get("plot") or "")[:300],
        "rating": max(0.0, min(10.0, rating)),
        "poster_url": None,
        "genres": genres,
        "imdb_id": None,
        "source": "groq_identify",
    }
    log.info("enrich: AI identified %r", meta["title"][:60])
    return meta


# --- main pipeline ----------------------------------------------------------
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

    # 3. v9.2 AI fallback: title/year only, no search-API response.
    # P1#16: a None from the quota-gated AI path is never cached — a
    # later user WITH quota must still get a real answer.
    ai_path = False
    if meta is None and ai_on and user_id is not None:
        ai_path = True
        meta = await _groq_identify(keywords, year, user_id)

    if meta is not None or not ai_path:
        _cache[cache_key] = (time.time(), meta)
    # P3#17: evict the oldest ~100 instead of nuking the whole cache.
    if len(_cache) > 500:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return meta
