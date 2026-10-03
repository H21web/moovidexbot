"""v10.8 search module — simple pipeline.

1. **Clean + instant search** — the message is cleaned
   (``textutil.parse_query``) and the DB is swept with several query
   variants via ``asyncio.gather``; hits merge by best score per file
   id. A low-threshold trigram pass (fuzzy) fires when hits are thin.
2. **Grok AI title extraction** — only when zero files were found.
   Grok gets the original query only and returns 1-5
   ``{title, year, type, reason}`` candidates; every candidate is
   verified against the DB (year applied for movies only, never
   series). One verified title -> used directly. Several ->
   ``status="choose"`` so the user picks. None -> ``no_results``.

No Search-API stage, no local spell stage — the pipeline is
instant search -> fuzzy -> AI, nothing else.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time as _time

from sqlalchemy import func, select

from app import personalize
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
    """Run the v10.8 search pipeline.

    1. Clean the message, instant DB search (parallel sweeps + fuzzy
       retry inside ``_hot_sweeps``).
    2. No files -> JustWatch title API (free, clean title+year+type).
    3. Still no files -> Grok AI title extraction (original query
       only), 1-5 titles; each verified against the DB.
    4. Exactly one verified title -> use it directly.
       Several verified titles -> ``status="choose"`` (user picks).
       Titles found but none with files -> ``status="suggest"``
       (tappable buttons; tap = request that title).
       None -> ``no_results``.

    Returns ``{"status", "files", "best", "best_reasons", "title",
    "parsed", "confidence", "corrected", "corrected_via", "sid",
    "choices", "suggestions", "raw"}``; status is
    ``"ok" | "uncertain" | "no_results" | "choose" | "suggest"``.
    ``corrected_via`` is ``"justwatch" | "ai" | None``; ``sid``
    is the per-search trace id used in the log lines.
    """
    t0 = _time.time()
    sid = secrets.token_hex(2)
    raw = (raw or "").strip()
    log.info("[s:%s] \u25b6 query=%r uid=%s", sid, raw[:80], user_id)
    parsed = parse_query(raw)
    parsed = await _ai_parse_if_needed(user_id, raw, parsed)
    title = (parsed.get("title") or raw).strip()
    orig_parsed = dict(parsed)

    # --- 1. instant search (normal + fuzzy) ------------------------------
    merged = await _hot_sweeps(user_id, raw, parsed, log_q=True)
    corrected: str | None = None
    corrected_via: str | None = None  # "ai" | None
    score = _best_score(merged)
    el = lambda: int((_time.time() - t0) * 1000)
    log.info("[s:%s] hot: %d files best=%.2f (%dms)", sid, len(merged),
             score, el())

    # --- 2. JustWatch title API (free, clean titles) ---------------------
    # --- 3. Grok AI title extraction (only if JustWatch found nothing) --
    choices: list[dict] = []
    suggestions: list[dict] = []  # titles with no DB files -> suggest card
    seen_sug: set[str] = set()

    def _suggest(title: str, year: int | None, typ: str | None) -> None:
        k = (title or "").strip().lower()
        if k and k not in seen_sug:
            seen_sug.add(k)
            suggestions.append({"title": (title or "").strip(),
                                "year": year, "type": typ})

    if not merged:
        from app import enrich as enrich_mod
        try:
            jw_titles = await enrich_mod.justwatch_titles(raw, limit=5)
        except Exception as exc:  # noqa: BLE001
            log.debug("justwatch failed: %s", exc)
            jw_titles = []
        for jt in jw_titles:
            t_parsed = _with_original_filters(parse_query(jt["title"]),
                                              orig_parsed)
            if jt.get("type") == "movie" and jt.get("year"):
                t_parsed["year"] = jt["year"]
            retry = await _hot_sweeps(user_id, jt["title"], t_parsed,
                                      log_q=False)
            log.info("[s:%s] justwatch: tried %r (%s) -> %d files "
                     "(%dms)", sid, jt["title"][:50], jt.get("type"),
                     len(retry), el())
            if retry:
                choices.append({"title": jt["title"],
                                "year": jt.get("year"),
                                "type": jt.get("type"),
                                "reason": "justwatch",
                                "via": "justwatch",
                                "hits": retry, "parsed": t_parsed})
            else:
                _suggest(jt["title"], jt.get("year"), jt.get("type"))
        if not choices:
            log.info("[s:%s] grok: asking (original query only) (%dms)",
                     sid, el())
            try:
                from app import ai as ai_mod
                ai_titles = await ai_mod.ai_extract_titles(user_id, raw,
                                                           sid=sid)
            except Exception as exc:  # noqa: BLE001
                log.debug("AI title extract failed: %s", exc)
                ai_titles = []
            # Verify every AI title against the DB — only titles with
            # real files survive. Year is applied for movies only,
            # never series.
            for t in ai_titles:
                t_parsed = _with_original_filters(parse_query(t["title"]),
                                                  orig_parsed)
                if t.get("type") == "movie" and t.get("year"):
                    t_parsed["year"] = t["year"]
                retry = await _hot_sweeps(user_id, t["title"], t_parsed,
                                          log_q=False)
                log.info("[s:%s] grok: tried %r (%s) -> %d files (%dms)",
                         sid, t["title"][:50], t.get("type"),
                         len(retry), el())
                if retry:
                    choices.append({"title": t["title"],
                                    "year": t.get("year"),
                                    "type": t.get("type"),
                                    "reason": t.get("reason"),
                                    "via": "ai",
                                    "hits": retry, "parsed": t_parsed})
                else:
                    _suggest(t["title"], t.get("year"), t.get("type"))
        if len(choices) == 1:
            c = choices[0]
            merged, score = c["hits"], _best_score(c["hits"])
            parsed = c["parsed"]
            parsed["title"] = c["title"]
            title = c["title"]
            corrected = c["title"]
            corrected_via = c["via"]
            log.info("[s:%s] %s: single title %r -> using it (%dms)",
                     sid, c["via"], title[:50], el())
        elif len(choices) > 1:
            log.info("[s:%s] %s: %d titles -> asking user (%dms)", sid,
                     choices[0]["via"], len(choices), el())
            return {"status": "choose", "files": [], "best": None,
                    "best_reasons": [], "title": title, "parsed": parsed,
                    "confidence": 0.0, "corrected": None,
                    "corrected_via": None, "sid": sid,
                    "choices": [{"title": c["title"], "year": c["year"],
                                 "type": c["type"], "reason": c["reason"]}
                                for c in choices],
                    "suggestions": [], "raw": raw}
        elif suggestions:
            # Titles were found (JustWatch/AI) but none have files —
            # show them as tappable buttons so the user can pick one
            # to request, instead of a dead "try a different spelling".
            log.info("[s:%s] suggest: %d titles, no files (%dms)", sid,
                     len(suggestions), el())
            return {"status": "suggest", "files": [], "best": None,
                    "best_reasons": [], "title": title, "parsed": parsed,
                    "confidence": 0.0, "corrected": None,
                    "corrected_via": None, "sid": sid, "choices": [],
                    "suggestions": suggestions[:5], "raw": raw}

    items = list(merged.values())
    if not items:
        log.info("[s:%s] \u25c0 status=no_results title=%r (%dms)",
                 sid, title[:60], el())
        return {"status": "no_results", "files": [], "best": None,
                "best_reasons": [], "title": title, "parsed": parsed,
                "confidence": 0.0, "corrected": corrected,
                "corrected_via": corrected_via, "sid": sid,
                "choices": [], "suggestions": [], "raw": raw}

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
            "corrected_via": corrected_via, "sid": sid,
            "choices": [], "raw": raw}
