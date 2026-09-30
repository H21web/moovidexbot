"""Ultra-fast, relevant file search.

Query pipeline (all index-assisted, bounded to a handful of queries):

1. **Strict + filters** — every query word must appear as a case-insensitive
   substring of the file name or caption (the ``pg_trgm`` GIN indexes keep
   ``ILIKE '%word%'`` fast), with quality/language hard filters applied.
2. **Strict, filters dropped** — same words, no filters. Graceful when a
   file's metadata tags are missing; exact filter matches still rank first.
3. **Relaxed** — the two longest words only, so partial queries still hit.
4. **Trigram fallback** — whole-query similarity for typos.

Results are re-ranked in Python: the v1 relevance scorer
(``difflib.SequenceMatcher`` on tag-stripped names) plus boosts for
word order, year, quality/language, and season/episode matches.

The query is normalized first (``textutil.parse_query``): quality /
language / year / season / episode tokens become filters instead of
search terms, dots split words, and intent noise ("full movie download")
is dropped. Hot queries are cached in-memory for 5 minutes; every query
is logged to ``search_logs`` for stats and trending.
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

from sqlalchemy import func, select

from app.config import settings
from app.db import get_session_factory
from app.models import File, SearchLog
from app.textutil import (
    QUALITY_ORDER,
    clean_title,
    extract_year,
    parse_query,
    title_key,
)
from app.state import hot_get, hot_set

log = logging.getLogger(__name__)

RESULT_LIMIT = 60
TRIGRAM_MIN_HITS = 8
TRIGRAM_THRESHOLD = 0.25

ITEM_FIELDS = (
    "id", "file_id", "file_name", "file_size", "mime_type", "caption",
    "channel_id", "message_id", "quality", "language", "title_key",
)

# Tags stripped before relevance comparison (ported from v1 pm_filter).
_NOISE_WORDS_RE = re.compile(
    r"\b(4k|2160p|1080p|720p|480p|480|360p|cam|dvd|dvdrip|rip|web|webrip|"
    r"webdl|hdts|hdr|x264|x265|hevc|10bit|dual|multi|audio|hindi|tamil|"
    r"telugu|malayalam|kannada|english|eng|sub|esub|esubs|aac|dts|ddp)\b"
)


def _item_to_dict(row: File, score: float) -> dict:
    return {f: getattr(row, f) for f in ITEM_FIELDS} | {"score": float(score or 0)}


def _apply_filters(stmt, parsed: dict):
    if parsed.get("quality"):
        stmt = stmt.where(File.quality == parsed["quality"])
    if parsed.get("language"):
        stmt = stmt.where(File.language == parsed["language"])
    return stmt


def _ilike_escape(word: str) -> str:
    """Escape ILIKE wildcards in a user-supplied word."""
    return (
        word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _query_words(q: str) -> list[str]:
    """Split the cleaned query into matchable words (v1 behavior)."""
    words: list[str] = []
    for w in re.split(r"\s+", (q or "").strip()):
        w = w.strip(".,!?;:\"'()[]{}").strip()
        if len(w) >= 2:
            words.append(w)
    return words


async def _stage_contains(
    session, words: list[str], parsed: dict, use_filters: bool = True
) -> list[tuple[File, float]]:
    """Every word must appear (case-insensitive) in file_name or caption.

    Faithful port of the v1 ``get_search_results`` matcher. The pg_trgm
    GIN indexes on ``file_name``/``caption`` keep the ``ILIKE '%word%'``
    patterns index-assisted instead of full scans. ``use_filters=False``
    drops the quality/language hard filters for graceful degradation.
    """
    if not words:
        return []
    stmt = select(File)
    for w in words:
        pat = f"%{_ilike_escape(w)}%"
        stmt = stmt.where(File.file_name.ilike(pat) | File.caption.ilike(pat))
    if use_filters:
        stmt = _apply_filters(stmt, parsed)
    if not words and not (parsed.get("quality") or parsed.get("language")):
        # Nothing to match on (no words, no hard filters).
        return []
    stmt = stmt.limit(RESULT_LIMIT)
    rows = (await session.execute(stmt)).scalars().all()
    return [(row, 0.0) for row in rows]


async def _stage_trigram(
    session, q: str, parsed: dict, use_filters: bool = False
) -> list[tuple[File, float]]:
    sim = func.similarity(File.file_name, q)
    stmt = (
        select(File, sim.label("rank"))
        .where(sim > TRIGRAM_THRESHOLD)
        .order_by(sim.desc())
        .limit(RESULT_LIMIT)
    )
    if use_filters:
        stmt = _apply_filters(stmt, parsed)
    rows = (await session.execute(stmt)).all()
    return [(row[0], row[1]) for row in rows]


def _season_episode_hit(
    file_name: str | None, season: int | None, episode: int | None
) -> bool:
    """Does the filename reference the queried season/episode?"""
    if not file_name or season is None:
        return False
    t = file_name.lower()
    if episode is not None:
        # Glued forms first: S01E02, s01e02, 1x02.
        if re.search(rf"\bs0*{season}(?![0-9])\s*e0*{episode}(?![0-9])", t):
            return True
        if re.search(rf"(?<![0-9]){season}x0*{episode}(?![0-9])", t):
            return True
    # Separate markers: S02 / season 2 / season.2
    if not (
        re.search(rf"\bs0*{season}(?![0-9])", t)
        or re.search(rf"\bseason[\s._-]*0*{season}(?![0-9])", t)
    ):
        return False
    if episode is None:
        return True
    return (
        re.search(rf"\be0*{episode}(?![0-9])", t) is not None
        or re.search(rf"\bep(?:isode)?[\s._-]*0*{episode}(?![0-9])", t)
        is not None
    )
def _rank_items(query: str, items: list[dict], parsed: dict) -> list[dict]:
    """Re-rank hits by relevance.

    v1 ``sort_by_relevance`` tiers (SequenceMatcher on tag-stripped names)
    plus boosts: query words in order, and matches on the parsed year /
    quality / language / season+episode filters.
    """
    qn = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", query.lower())).strip()
    qwords = qn.split()

    def in_order(name: str) -> bool:
        pos = 0
        for w in qwords:
            i = name.find(w, pos)
            if i < 0:
                return False
            pos = i + len(w)
        return True

    def key(it: dict) -> tuple[float, int, int]:
        name = (it.get("file_name") or "").lower()
        name = re.sub(r"\s+", " ", name.replace(".", " ").replace("_", " ")).strip()
        year = extract_year(it.get("file_name")) or 0
        clean = _NOISE_WORDS_RE.sub(" ", name)
        clean = re.sub(r"\s+", " ", clean).strip()
        sim = SequenceMatcher(None, qn, clean).ratio() if qn and clean else 0.0
        if sim > 0.9:
            score = 3.0
        elif qn and name.startswith(qn):
            score = 2.5
        elif sim > 0.6:
            score = 2.0
        elif qn and qn in name:
            score = 1.0
        else:
            score = 0.0
        if qwords and in_order(name):
            score += 0.4
        if parsed.get("year") and year == parsed["year"]:
            score += 0.6
        if parsed.get("quality") and (
            it.get("quality") or ""
        ).lower() == parsed["quality"].lower():
            score += 0.5
        if parsed.get("language") and (
            it.get("language") or ""
        ).lower() == parsed["language"].lower():
            score += 0.5
        if _season_episode_hit(
            it.get("file_name"), parsed.get("season"), parsed.get("episode")
        ):
            score += 0.7
        return (score, year, it.get("file_size") or 0)

    items.sort(key=key, reverse=True)
    return items


async def search_files(
    raw_query: str,
    user_id: int | None = None,
    log_query: bool = True,
) -> tuple[list[dict], dict]:
    """Search the file index.

    Returns ``(items, parsed)`` where items are dicts (see ``ITEM_FIELDS`` +
    ``score``) ordered by relevance, and ``parsed`` is the normalized query.
    """
    parsed = parse_query(raw_query)
    q = parsed["query"]
    has_filters = bool(
        parsed["quality"]
        or parsed["language"]
        or parsed["year"]
        or parsed["season"] is not None
        or parsed["episode"] is not None
    )
    if len(q) < 2 and not has_filters:
        return [], parsed

    cache_key = (
        f"q:{q.lower()}|qlt:{parsed['quality']}|lng:{parsed['language']}"
        f"|yr:{parsed['year']}|s:{parsed['season']}|e:{parsed['episode']}"
    )
    cached = hot_get(cache_key)
    items: list[dict] | None = cached  # type: ignore[assignment]

    if items is None:
        items = []
        factory = get_session_factory(settings.DATABASE_URL)
        try:
            async with factory() as session:
                words = _query_words(q)
                # 1) strict words + hard quality/language filters
                hits = await _stage_contains(session, words, parsed,
                                             use_filters=True)
                seen = {f.id for f, _ in hits}
                # 2) strict words, filters dropped (metadata may be missing)
                if len(hits) < TRIGRAM_MIN_HITS and (
                    parsed["quality"] or parsed["language"]
                ):
                    for f, score in await _stage_contains(
                        session, words, parsed, use_filters=False
                    ):
                        if f.id not in seen:
                            hits.append((f, score))
                            seen.add(f.id)
                # 3) relaxed: two longest words (partial queries still hit)
                if len(hits) < TRIGRAM_MIN_HITS and len(words) > 2:
                    rwords = sorted(words, key=len, reverse=True)[:2]
                    for f, score in await _stage_contains(
                        session, rwords, parsed, use_filters=False
                    ):
                        if f.id not in seen:
                            hits.append((f, score * 0.8))
                            seen.add(f.id)
                # 4) trigram typo fallback, unfiltered
                if len(hits) < TRIGRAM_MIN_HITS:
                    for f, score in await _stage_trigram(
                        session, q, parsed, use_filters=False
                    ):
                        if f.id not in seen:
                            hits.append((f, score * 0.9))
                            seen.add(f.id)
                items = [_item_to_dict(f, s) for f, s in hits]
                items = _rank_items(q, items, parsed)[:RESULT_LIMIT]
        except Exception as exc:  # noqa: BLE001 - search must degrade, not crash
            log.warning("search failed for %r: %s", raw_query, exc)
            items = []
        hot_set(cache_key, items)

    if log_query:
        try:
            factory = get_session_factory(settings.DATABASE_URL)
            async with factory() as session:
                session.add(
                    SearchLog(query=raw_query.strip()[:200], user_id=user_id,
                              hits=len(items))
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - logging never breaks search
            log.debug("search log failed: %s", exc)

    return items, parsed


def group_by_title(items: list[dict]) -> list[dict]:
    """Group file items into movies: one card per title.

    Returns ``[{"key", "display", "year", "files": [...]}]`` ordered by the
    best file score in each group. Files inside a group are sorted by
    quality (best first), then size.
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for item in items:
        key = item.get("title_key") or title_key(item.get("file_name"))
        if not key:
            key = (item.get("file_name") or "").lower()
        if key not in groups:
            groups[key] = {
                "key": key,
                "display": clean_title(item.get("file_name")) or "Unknown",
                "year": extract_year(item.get("file_name")),
                "files": [],
                "best": item.get("score", 0),
            }
            order.append(key)
        g = groups[key]
        g["files"].append(item)
        g["best"] = max(g["best"], item.get("score", 0))

    def quality_rank(f: dict) -> tuple[int, int]:
        return (
            QUALITY_ORDER.get((f.get("quality") or "").lower(), -1),
            f.get("file_size") or 0,
        )

    movies = []
    for key in order:
        g = groups[key]
        g["files"].sort(key=quality_rank, reverse=True)
        movies.append(g)
    movies.sort(key=lambda m: m["best"], reverse=True)
    return movies


async def get_trending(days: int = 7, limit: int = 6) -> list[tuple[str, int]]:
    """Most-searched queries with hits in the last ``days`` days."""
    from datetime import datetime, timedelta, timezone

    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            stmt = (
                select(SearchLog.query, func.count().label("c"))
                .where(SearchLog.created_at >= since, SearchLog.hits > 0)
                .group_by(SearchLog.query)
                .order_by(func.count().desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()
            return [(r[0], r[1]) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.debug("trending failed: %s", exc)
        return []


async def get_recent_queries(user_id: int, limit: int = 5) -> list[str]:
    """Recent distinct queries by a user (for AI personalization)."""
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            stmt = (
                select(SearchLog.query)
                .where(SearchLog.user_id == user_id, SearchLog.hits > 0)
                .order_by(SearchLog.id.desc())
                .limit(limit * 3)
            )
            rows = (await session.execute(stmt)).scalars().all()
            seen: set[str] = set()
            out: list[str] = []
            for q in rows:
                if q not in seen:
                    seen.add(q)
                    out.append(q)
                if len(out) >= limit:
                    break
            return out
    except Exception:  # noqa: BLE001
        return []
