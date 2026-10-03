"""v10.2 search module — fast on the hot path, smart on recovery.

Pipeline:

**Hot path** (every search, AI-free, ~parallel):

1. **Parse** — local keyword-first parse (``textutil.parse_query``):
   year/language/quality/season/episode come out of the query itself.
2. **Multi-sweep (parallel)** — the DB is swept with several query
   variants via ``asyncio.gather``; hits merge by best score per file id.
3. **Fuzzy retry** — when hits are thin, a low-threshold trigram pass so
   typos and mistagged files still surface.
   A best hit scoring >= ``GOOD_SCORE`` returns immediately.

**Recovery path** (only when the hot path is weak — the 3 logics):

4. **Spell correction** (local, quota-free) — ``"avangerrs"`` becomes
   ``"avengers"`` via Levenshtein against real indexed title words;
   the DB is swept once more with the fixed query.
5. **Web-search title parse** — the free search API is asked for
   ``"<query> movie"`` and the canonical title is parsed from the
   top results (IMDb/Wikipedia titles); the DB is swept with it.
6. **AI title extraction** (quota-gated) — Groq pulls the movie/series
   title out of the query; the DB is swept with it.

Then: personal taste re-rank, user-priority best pick, confidence
(``ok`` / ``uncertain`` / ``no_results``). Results render instantly;
enrichment (poster/info) fills in via a background edit.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time as _time

from sqlalchemy import func, select

from app import enrich as enrich_mod, personalize, spell
from app.bot.v8_ui import sort_best_first
from app.config import settings
from app.db import get_session_factory
from app.models import File
from app.search import RESULT_LIMIT, search_files
from app.textutil import extract_year, parse_query

log = logging.getLogger(__name__)

MIN_HITS = 8            # below this, the fuzzy retry kicks in
FUZZY_THRESHOLD = 0.15  # lowered trigram bar for the retry pass
def _query_season_episode(parsed: dict, files: list[dict]) -> list[dict]:
    """Narrow files to the query's season/episode; no-op when absent.

    (moved from app.ai_search, which is deleted — this helper never
    used AI.) If the filter would empty the set, the full set is kept
    (boost instead of filter) so a slightly-off tag never yields zero
    results.
    """
    season, episode = parsed.get("season"), parsed.get("episode")
    if not season and not episode:
        return files
    from app.bot.v8_ui import file_season_episode
    kept = []
    for f in files:
        s, e = file_season_episode(f.get("file_name"))
        if season and s != season:
            continue
        if episode and e != episode:
            continue
        kept.append(f)
    return kept or files


UNCERTAIN_SCORE = 1.0   # best hit below this -> "uncertain"
GOOD_SCORE = 2.0        # hot path at/above this skips recovery entirely

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


async def _hot_sweeps(user_id: int | None, raw: str, parsed: dict,
                    log_q: bool = True) -> dict[int, dict]:
    """Parallel DB sweeps + fuzzy retry. Returns {file_id: item}."""
    variants = _sweep_queries(raw, parsed)
    merged: dict[int, dict] = {}

    async def _one(variant: str, do_log: bool) -> tuple[str, list]:
        try:
            items, _ = await search_files(variant, user_id=user_id,
                                          log_query=do_log)
            return variant, items
        except Exception as exc:  # noqa: BLE001
            log.warning("v10 sweep %r failed: %s", variant, exc)
            return variant, []

    if variants:
        for variant, items in await asyncio.gather(
                *(_one(v, log_q and i == 0) for i, v in enumerate(variants))):
            for it in items:
                fid = it.get("id")
                if fid is None:
                    continue
                prev = merged.get(fid)
                if prev is None or (it.get("score") or 0) > (
                        prev.get("score") or 0):
                    merged[fid] = it

    if len(merged) < MIN_HITS:
        title = (parsed.get("title") or raw).strip()
        for it in await _fuzzy_sweep(title or raw):
            fid = it.get("id")
            if fid is None or fid in merged:
                continue
            merged[fid] = it
    return merged


def _best_score(merged: dict[int, dict]) -> float:
    return max((float(it.get("score") or 0.0) for it in merged.values()),
               default=0.0)


def _with_original_filters(new_parsed: dict, orig_parsed: dict) -> dict:
    """Keep the user's own filters when a correction replaces the title.

    v10.7.1: "avangerrs endgame 1080p" corrected to "Avengers: Endgame"
    must still search 1080p — the AI prompt strips technical words, so
    re-attach quality/language/year/season/episode from the original.
    """
    for k in ("quality", "language", "year", "season", "episode"):
        if orig_parsed.get(k) and not new_parsed.get(k):
            new_parsed[k] = orig_parsed[k]
    return new_parsed


async def smart_search(user_id: int | None, raw: str) -> dict:
    """Run the v10.2 search pipeline.

    Returns ``{"status", "files", "best", "best_reasons", "title",
    "parsed", "confidence", "corrected", "corrected_via"}``; status is
    ``"ok" | "uncertain" | "no_results"``. ``corrected`` is the
    auto-fixed query when a recovery stage fired (for display);
    ``corrected_via`` is ``"spell" | "web" | "ai" | None``; ``sid`` is
    the per-search trace id used in the log lines.
    """
    t0 = _time.time()
    sid = secrets.token_hex(2)
    raw = (raw or "").strip()
    log.info("[s:%s] \u25b6 query=%r uid=%s", sid, raw[:80], user_id)
    parsed = parse_query(raw)
    parsed = await _ai_parse_if_needed(user_id, raw, parsed)
    title = (parsed.get("title") or raw).strip()
    factory = get_session_factory(settings.DATABASE_URL)

    # --- hot path ------------------------------------------------------
    merged = await _hot_sweeps(user_id, raw, parsed, log_q=True)
    corrected: str | None = None
    corrected_via: str | None = None  # "spell" | "web" | "ai"
    score = _best_score(merged)
    el = lambda: int((_time.time() - t0) * 1000)
    log.info("[s:%s] hot: %d files best=%.2f (%dms)", sid, len(merged),
             score, el())

    # --- recovery: 1st logic — local spell correction ------------------
    if score < GOOD_SCORE:
        try:
            fixed = await spell.correct_query(raw, factory)
        except Exception as exc:  # noqa: BLE001
            log.debug("spell correction failed: %s", exc)
            fixed = None
        if fixed and fixed.lower() != raw.lower():
            retry = await _hot_sweeps(user_id, fixed, parse_query(fixed),
                                      log_q=False)
            rs = _best_score(retry)
            if rs > score:
                merged, score = retry, rs
                corrected = fixed
                corrected_via = "spell"
                parsed = parse_query(fixed)
                parsed["title"] = fixed
                title = fixed
                log.info("[s:%s] spell: %r -> %r (%d files best=%.2f) (%dms)",
                         sid, raw[:50], fixed[:50], len(retry), rs, el())
            else:
                log.info("[s:%s] spell: %r -> %r not better "
                         "(%.2f <= %.2f) (%dms)",
                         sid, raw[:50], fixed[:50], rs, score, el())
        else:
            log.info("[s:%s] spell: no correction (%dms)", sid, el())

    # --- recovery: 2nd logic: Search API title candidates ----------------
    # v10.7 rework: ONLY when the DB found zero files. Candidates are
    # scored against the query (junk/franchise pages rejected); the
    # first candidate with real DB files wins. A weak-but-real result
    # set is never hijacked by a web guess anymore.
    if not merged:
        try:
            candidates = await enrich_mod.web_title_candidates(raw, sid=sid)
        except Exception as exc:  # noqa: BLE001
            log.debug("web title parse failed: %s", exc)
            candidates = []
        log.info("[s:%s] web: %d candidate(s) %r (%dms)", sid,
                 len(candidates), [c[:40] for c in candidates[:3]], el())
        for web_title in candidates[:3]:
            new_parsed = _with_original_filters(parse_query(web_title),
                                                parsed)
            retry = await _hot_sweeps(user_id, web_title,
                                      new_parsed, log_q=False)
            log.info("[s:%s] web: tried %r -> %d files (%dms)", sid,
                     web_title[:50], len(retry), el())
            if retry:
                merged, score = retry, _best_score(retry)
                parsed = new_parsed
                parsed["title"] = web_title
                title = web_title
                corrected = web_title
                corrected_via = "web"
                break

    # --- recovery: 3rd logic — Grok AI title (v10.6, flow diagram) -----
    # Runs only when the Search API found no usable title. The original
    # user query only is sent to Grok — never any search-API response.
    if not merged:
        log.info("[s:%s] grok: asking (original query only) (%dms)",
                 sid, el())
        try:
            from app import ai as ai_mod
            ai_title = await ai_mod.ai_extract_title(user_id, raw, sid=sid)
        except Exception as exc:  # noqa: BLE001
            log.debug("AI title extract failed: %s", exc)
            ai_title = None
        if ai_title:
            log.info("[s:%s] grok: %r -> trying DB (%dms)", sid,
                     ai_title[:50], el())
            new_parsed = _with_original_filters(parse_query(ai_title),
                                                parsed)
            retry = await _hot_sweeps(user_id, ai_title,
                                      new_parsed, log_q=False)
            if retry:
                merged, score = retry, _best_score(retry)
                parsed = new_parsed
                parsed["title"] = ai_title
                title = ai_title
                corrected = ai_title
                corrected_via = "ai"

    items = list(merged.values())
    if not items:
        log.info("[s:%s] \u25c0 status=no_results title=%r (%dms)",
                 sid, title[:60], el())
        return {"status": "no_results", "files": [], "best": None,
                "best_reasons": [], "title": title, "parsed": parsed,
                "confidence": 0.0, "corrected": corrected,
                "corrected_via": corrected_via, "sid": sid}

    # --- rerank: taste -> score/quality/size ---------------------------
    try:
        items = await personalize.rerank(items, user_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("v10 personalize failed: %s", exc)
    items = sort_best_first(items)
    files = _query_season_episode(parsed, items)

    # --- best pick: the user's keywords + taste choose -----------------
    try:
        best, reasons = await personalize.choose_best(files, parsed, user_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("choose_best failed: %s", exc)
        best, reasons = None, []
    if best is None:
        top_dl = max(files, key=lambda f: f.get("downloads") or 0,
                     default=None)
        if top_dl and (top_dl.get("downloads") or 0) > 0:
            best = top_dl
        else:
            best = files[0]
        reasons = []
    else:
        files = [best] + [f for f in files if f.get("id") != best.get("id")]

    confidence = float(best.get("score") or 0.0)
    status = "ok" if confidence >= UNCERTAIN_SCORE else "uncertain"
    log.info("[s:%s] \u25c0 status=%s title=%r via=%s files=%d "
             "best=%r (%dms)",
             sid, status, title[:60], corrected_via, len(files),
             (best.get("file_name") or "")[:60], el())
    return {"status": status, "files": files, "best": best,
            "best_reasons": reasons, "title": title, "parsed": parsed,
            "confidence": confidence, "corrected": corrected,
            "corrected_via": corrected_via, "sid": sid}
