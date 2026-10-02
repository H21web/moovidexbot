"""v9 search module — fast, accurate, AI-free on the hot path.

Pipeline:

1. **Parse** — local keyword-first parse (``textutil.parse_query``):
   year/language/quality/season/episode come out of the query itself.
   No AI parse on the search path (speed + quota).
2. **Multi-sweep (parallel)** — the DB is swept with several query
   variants (raw, cleaned title, title-only, title+year, language
   word stripped) via ``asyncio.gather``; hits merge by best score per
   file id. One query variant missing never sinks the search.
3. **Fuzzy retry** — when hits are few, a second pass runs with the
   trigram threshold lowered (0.15) and no filters, so typos and
   mistagged files still surface.
4. **Rerank** — personal taste, then score -> quality -> size; the
   most-downloaded file wins best pick when any downloads exist.
5. **Confidence** — the best hit's relevance score decides ``ok`` or
   ``uncertain`` (both render; ``uncertain`` just means weak matches).

AI appears only in the no-results recovery chain
(:mod:`app.ai_assist`) — never on a successful search. The results
render instantly; enrichment (poster/info) fills in afterwards via a
background message edit owned by the search handler.
"""
from __future__ import annotations

import asyncio
import logging
import re

from sqlalchemy import func, select

from app import personalize
from app.ai_search import _query_season_episode
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

_FUZZY_FIELDS = (
    "id", "file_id", "file_name", "file_size", "mime_type", "caption",
    "channel_id", "message_id", "quality", "language", "title_key",
    "downloads",
)


async def _ai_parse_if_needed(user_id: int | None, raw: str,
                              parsed: dict) -> dict:
    """v9.1: local parse only — no AI on the search path (speed + quota).

    AI is reserved for the no-results spell-correction chain.
    """
    title = (parsed.get("query") or "").strip()
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

    # --- multi-sweep (parallel): every variant, best score per file id --
    variants = _sweep_queries(raw, parsed)
    merged: dict[int, dict] = {}

    async def _one(variant: str, log_q: bool) -> tuple[str, list]:
        try:
            items, _ = await search_files(variant, user_id=user_id,
                                          log_query=log_q)
            return variant, items
        except Exception as exc:  # noqa: BLE001
            log.warning("v9 sweep %r failed: %s", variant, exc)
            return variant, []

    if variants:
        for variant, items in await asyncio.gather(
                *(_one(v, i == 0) for i, v in enumerate(variants))):
            for it in items:
                fid = it.get("id")
                if fid is None:
                    continue
                prev = merged.get(fid)
                if prev is None or (it.get("score") or 0) > (
                        prev.get("score") or 0):
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
