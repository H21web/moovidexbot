"""v9 search module — the best search this bot has.

Pipeline:

1. **Parse** — local keyword-first parse (``textutil.parse_query``) is the
   priority: year/language/quality/season/episode come out of the query
   itself. Only *messy* queries (Manglish noise, "?", 6+ words, no clear
   title) spend 1 AI quota unit on a structured Groq parse.
2. **Multi-sweep** — the DB is swept with several query variants (raw,
   cleaned title, title-only, title+year) and hits merge by best score
   per file id. One query variant missing never sinks the search.
3. **Fuzzy retry** — when hits are few, a second pass runs with the
   trigram threshold lowered (0.25 -> 0.15) and the language filter
   dropped, so typos and mistagged files still surface.
4. **Rerank** — personal taste, then score -> quality -> size; the
   most-downloaded file wins best pick when any downloads exist.
5. **Confidence** — the best hit's relevance score decides: ``ok``,
   ``uncertain`` (weak matches — AI steps in), or ``no_results``.

Works fully with AI off. AI only *assists* (parse messy queries, judge
uncertain candidates, suggest corrections) — search never depends on it.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import func, select

from app import personalize
from app.ai_search import _query_season_episode, ai_parse_query
from app.bot.v8_ui import sort_best_first
from app.config import settings
from app.db import get_session_factory
from app.models import File
from app.search import RESULT_LIMIT, search_files
from app.textutil import extract_year, parse_query

log = logging.getLogger(__name__)

MIN_HITS = 8            # below this, the fuzzy retry kicks in
FUZZY_THRESHOLD = 0.15  # lowered trigram bar for the retry pass
UNCERTAIN_SCORE = 1.0   # best hit below this -> "uncertain"

_MESSY_RE = re.compile(
    r"(undo|aano|alle|aakumo|aakum|entha|enth|evide|eppol|aara|aar\?|"
    r"please|pls|vendum|venam|tharoo|tharu)",
    re.IGNORECASE)

_FUZZY_FIELDS = (
    "id", "file_id", "file_name", "file_size", "mime_type", "caption",
    "channel_id", "message_id", "quality", "language", "title_key",
    "downloads",
)


def _is_messy(raw: str, title: str) -> bool:
    if "?" in raw:
        return True
    if len(raw.split()) > 5:
        return True
    if _MESSY_RE.search(raw):
        return True
    return len(title) < 2


async def _ai_parse_if_needed(user_id: int | None, raw: str,
                              parsed: dict) -> dict:
    """Structured AI parse for messy queries only (1 quota unit)."""
    title = (parsed.get("query") or "").strip()
    if not _is_messy(raw, title):
        parsed = dict(parsed)
        parsed["title"] = title or raw.strip()
        return parsed
    try:
        from app import ai as ai_mod
        if ai_mod.is_configured() and await ai_mod.quota_remaining(
                user_id or 0) > 0:
            ai_parsed = await ai_parse_query(raw)
            await ai_mod.quota_use(user_id or 0)
            merged = dict(parsed)
            if ai_parsed.get("title"):
                merged["title"] = ai_parsed["title"]
            for k in ("year", "language", "quality"):
                if ai_parsed.get(k):
                    merged[k] = ai_parsed[k]
            log.info("v9 ai parse: %r -> %r", raw[:60], merged.get("title"))
            return merged
    except Exception as exc:  # noqa: BLE001
        log.debug("v9 ai parse failed: %s", exc)
    parsed = dict(parsed)
    parsed["title"] = title or raw.strip()
    return parsed


def _sweep_queries(raw: str, parsed: dict) -> list[str]:
    """Query variants to sweep the DB with (deduped, ordered)."""
    title = (parsed.get("title") or "").strip()
    year = parsed.get("year") or extract_year(raw)
    variants = [raw.strip()]
    if title and title.lower() != raw.strip().lower():
        variants.append(title)
    no_year = re.sub(r"\b(19\d{2}|20\d{2})\b", " ", title).strip()
    no_year = re.sub(r"\s+", " ", no_year)
    if no_year and no_year.lower() not in {v.lower() for v in variants}:
        variants.append(no_year)
    if year and title and str(year) not in title:
        variants.append(f"{title} {year}")
    # language word stripped (mistagged files still match)
    lang = (parsed.get("language") or "").strip()
    if lang:
        stripped = re.sub(re.escape(lang), " ", raw,
                          flags=re.IGNORECASE).strip()
        stripped = re.sub(r"\s+", " ", stripped)
        if len(stripped) >= 2 and stripped.lower() not in {
                v.lower() for v in variants}:
            variants.append(stripped)
    return [v for v in variants if len(v) >= 2][:5]


async def _fuzzy_sweep(q: str, limit: int = RESULT_LIMIT) -> list[dict]:
    """Low-threshold trigram sweep for typos (no filters)."""
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            sim = func.similarity(File.file_name, q)
            stmt = (
                select(File, sim.label("rank"))
                .where(sim > FUZZY_THRESHOLD)
                .order_by(sim.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()
            return [{f: getattr(r[0], f) for f in _FUZZY_FIELDS}
                    | {"score": float(r[1] or 0) * 0.7} for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.debug("v9 fuzzy sweep failed: %s", exc)
        return []


async def smart_search(user_id: int | None, raw: str) -> dict:
    """Run the v9 search pipeline.

    Returns ``{"status", "files", "best", "title", "parsed",
    "confidence"}``; status is ``"ok" | "uncertain" | "no_results"``.
    """
    raw = (raw or "").strip()
    parsed = parse_query(raw)
    parsed = await _ai_parse_if_needed(user_id, raw, parsed)
    title = (parsed.get("title") or raw).strip()

    # --- multi-sweep: every variant, best score per file id --------------
    merged: dict[int, dict] = {}
    first = True
    for variant in _sweep_queries(raw, parsed):
        try:
            items, _ = await search_files(variant, user_id=user_id,
                                          log_query=first)
        except Exception as exc:  # noqa: BLE001
            log.warning("v9 sweep %r failed: %s", variant, exc)
            continue
        first = False
        for it in items:
            fid = it.get("id")
            if fid is None:
                continue
            prev = merged.get(fid)
            if prev is None or (it.get("score") or 0) > (prev.get("score") or 0):
                merged[fid] = it

    # --- fuzzy retry when hits are thin ----------------------------------
    if len(merged) < MIN_HITS:
        for it in await _fuzzy_sweep(title or raw):
            fid = it.get("id")
            if fid is None or fid in merged:
                continue
            merged[fid] = it

    items = list(merged.values())
    if not items:
        return {"status": "no_results", "files": [], "best": None,
                "title": title, "parsed": parsed, "confidence": 0.0}

    # --- rerank: taste -> score/quality/size; downloads win best ---------
    try:
        items = await personalize.rerank(items, user_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("v9 personalize failed: %s", exc)
    items = sort_best_first(items)
    files = _query_season_episode(parsed, items)
    top_dl = max(files, key=lambda f: f.get("downloads") or 0, default=None)
    if top_dl and (top_dl.get("downloads") or 0) > 0:
        files = [top_dl] + [f for f in files if f.get("id") != top_dl.get("id")]
    best = files[0]

    confidence = float(best.get("score") or 0.0)
    status = "ok" if confidence >= UNCERTAIN_SCORE else "uncertain"
    log.info("v9 search %r: %d files, best score %.2f -> %s",
             raw[:60], len(files), confidence, status)
    return {"status": status, "files": files, "best": best,
            "title": title, "parsed": parsed, "confidence": confidence}
