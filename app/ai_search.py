"""v7 AI search: understand -> find -> recommend. Fast by design.

One small Groq call (llama-3.1-8b-instant, JSON) turns messy human text
("kgf movie undo", "play avatar 3") into a structured query. The DB search
runs in parallel with the parse; TMDB enriches only the top hits; the best
file is picked with the user's own taste (personalize). If Groq is off or
fails, everything silently falls back to the local keyword path — search
never breaks.
"""
from __future__ import annotations

import json
import logging
import re

from app import ai
from app.config import settings
from app.search import parse_query as local_parse_query
from app.search import search_files
from app import personalize

log = logging.getLogger(__name__)

PARSE_SYSTEM = (
    "You parse a movie-search request into JSON. Reply with ONLY JSON, no prose.\n"
    'Schema: {"title": str, "year": int|null, "language": str|null, '
    '"quality": str|null, "intent": "search"|"play"|"chat"}\n'
    "Rules:\n"
    '- title = the movie/series name only, cleaned ("kgf movie undo" -> "kgf").\n'
    '- intent "play" when the user wants to watch NOW '
    '("play kgf", "kgf kaananam", "start avatar").\n'
    '- intent "chat" for greetings, opinions, non-search talk.\n'
    '- otherwise intent "search".\n'
    '- language: normalize ("malayalam", "hindi", "tamil", "english"...).\n'
    '- quality: normalize ("480p", "720p", "1080p", "4k", "hdrip"...).\n'
    "- year: 4-digit or null."
)

_INTENT_RE = re.compile(r"\b(play|kaan|kaanunnu|kaananam|start|watch)\b", re.IGNORECASE)
# Queries messy enough to deserve a Groq parse (1 quota unit).
_MESSY_RE = re.compile(
    r"(undo|aano|alle|aakumo|aakum|entha|enth|evide|eppol|aara|aar\?)",
    re.IGNORECASE)


def _local_intent(q: str) -> str:
    """Cheap intent guess when Groq is unavailable."""
    if _INTENT_RE.search(q or ""):
        return "play"
    return "search"


def _local_parse(q: str) -> dict:
    parsed = dict(local_parse_query(q))
    parsed["title"] = parsed.get("query") or q
    parsed["intent"] = _local_intent(q)
    return parsed


async def ai_parse_query(q: str) -> dict:
    """Structured parse of the query via Groq (caller ensures quota)."""
    fallback = _local_parse(q)
    try:
        raw = await ai.groq_complete(
            PARSE_SYSTEM,
            f"Parse this request: {q[:200]}",
            max_tokens=220,
            json_mode=True,
            model=settings.AI_PARSE_MODEL,
        )
        if not raw:
            return fallback
        data = json.loads(raw)
        if not isinstance(data, dict) or not data.get("title"):
            return fallback
        out = dict(fallback)
        out["title"] = str(data["title"])[:120]
        out["year"] = data.get("year")
        out["language"] = data.get("language")
        out["quality"] = data.get("quality")
        if data.get("intent") in ("search", "play", "chat"):
            out["intent"] = data["intent"]
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("ai parse failed, local fallback: %s", exc)
        return fallback


async def ai_search(user_id: int, q: str) -> dict:
    """Full AI search pipeline.

    Returns {"status", "intent", "title", "files", "best", "parsed"}.
    status: ok | no_quota | no_results

    Groq is used (1 quota unit) only for messy queries; simple ones take
    the free local path.
    """
    use_ai = ai.is_configured()
    messy = "?" in q or len(q.split()) > 5 or bool(_MESSY_RE.search(q))
    if use_ai and messy:
        if await ai.quota_remaining(user_id) <= 0:
            return {"status": "no_quota", "intent": "search", "title": q,
                    "files": [], "best": None, "parsed": {}}
        parsed = await ai_parse_query(q)
        await ai.quota_use(user_id)
    else:
        parsed = _local_parse(q)

    title = (parsed.get("title") or q).strip()

    items_raw, _lp = await search_files(q, user_id=user_id, log_query=False)

    # Second parallel sweep with the cleaned title (if different).
    items_extra: list[dict] = []
    if title and title.lower() != q.strip().lower() and len(title) >= 2:
        items_extra, _ = await search_files(title, user_id=user_id,
                                            log_query=False)

    # Merge: keep the best score per file id.
    merged: dict[int, dict] = {}
    for it in list(items_raw) + list(items_extra):
        fid = it.get("id")
        if fid is None:
            continue
        prev = merged.get(fid)
        if prev is None or (it.get("score") or 0) > (prev.get("score") or 0):
            merged[fid] = it
    items = list(merged.values())

    if items:
        try:
            items = await personalize.rerank(items, user_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("personalize rerank failed: %s", exc)

    if not items:
        return {"status": "no_results", "intent": parsed.get("intent", "search"),
                "title": title, "files": [], "best": None, "parsed": parsed}
    return {"status": "ok", "intent": parsed.get("intent", "search"),
            "title": title, "files": items[:10], "best": items[0],
            "parsed": parsed}


def fmt_size(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return ""


def best_pick_line(best: dict) -> str:
    bits = [f"⭐ <b>Best pick:</b> {best.get('file_name', '')[:60]}"]
    meta = " · ".join(x for x in (
        best.get("quality"), best.get("language"),
        fmt_size(best.get("file_size"))) if x)
    if meta:
        bits.append(meta)
    return "\n".join(bits)


# ---------------------------------------------------------------- v8 rework
# Keyword-first search + enrichment. The local parser is the priority:
# language / year / season / episode / quality come out of the query
# itself ("s01 e02", "S1E3", "ep3", "season 2") and the DB search runs
# on keywords. Enrichment: search API -> IMDB id -> TMDB -> Groq fallback.
# AI only *enhances* (verdict line, no-result suggestion); the flow works
# fully with AI off.

VERDICT_SYSTEM = (
    "You write a ONE-LINE recommendation note (under 18 words, friendly). "
    "Say why this file is the best pick. Plain text, no markdown, no quotes."
)

SUGGEST_SYSTEM = (
    "You correct movie/series search queries. Reply with ONLY the most "
    "likely intended movie or series title, nothing else. No year, no quotes."
)


def _query_season_episode(parsed: dict, files: list[dict]) -> list[dict]:
    """Narrow files to the query's season/episode; no-op when absent.

    If the filter would empty the set, the full set is kept (boost
    instead of filter) so a slightly-off tag never yields zero results.
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


async def _ai_verdict(user_id: int, best: dict, title: str) -> str | None:
    """One-line AI note on why this file is the best pick (optional)."""
    if not ai.is_configured():
        return None
    try:
        if await ai.quota_remaining(user_id) <= 0:
            return None
        desc = (f"{title}: {(best.get('file_name') or '')[:80]}, "
                f"{best.get('quality') or '?'}, {best.get('language') or '?'}")
        note = await ai.groq_complete(VERDICT_SYSTEM,
                                      f"Best pick -> {desc}",
                                      max_tokens=60)
        if note:
            await ai.quota_use(user_id)
            return " ".join(note.strip().split())[:160] or None
    except Exception as exc:  # noqa: BLE001
        log.debug("ai verdict failed: %s", exc)
    return None


async def ai_suggest_title(user_id: int, q: str) -> str | None:
    """AI guess at the intended title when a search found nothing."""
    if not ai.is_configured():
        return None
    try:
        if await ai.quota_remaining(user_id) <= 0:
            return None
        raw = await ai.groq_complete(SUGGEST_SYSTEM,
                                     f"Query: {q[:150]}",
                                     max_tokens=40)
        if raw:
            await ai.quota_use(user_id)
            return raw.strip().strip("\"'")[:120] or None
    except Exception as exc:  # noqa: BLE001
        log.debug("ai suggest failed: %s", exc)
    return None


async def v8_search(user_id: int, q: str) -> dict:
    """v8 search pipeline.

    Returns ``{"status", "files", "best", "title", "meta", "parsed",
    "ai_note"}``; status is ``"ok"`` or ``"no_results"``. Works with
    zero AI configured.
    """
    from app import enrich as enrich_mod

    parsed = _local_parse(q)
    title = (parsed.get("title") or q).strip()
    # v8: no special "play query" card — "play kgf" is just a search for
    # kgf; the Play button rides on the file itself (Tech VJ style).
    if title.lower().startswith("play ") and len(title) > 5:
        title = title[5:].strip()
        parsed["title"] = title

    items_raw, _lp = await search_files(q, user_id=user_id, log_query=False)

    items_extra: list[dict] = []
    if title and title.lower() != q.strip().lower() and len(title) >= 2:
        items_extra, _ = await search_files(title, user_id=user_id,
                                            log_query=False)

    merged: dict[int, dict] = {}
    for it in list(items_raw) + list(items_extra):
        fid = it.get("id")
        if fid is None:
            continue
        prev = merged.get(fid)
        if prev is None or (it.get("score") or 0) > (prev.get("score") or 0):
            merged[fid] = it
    items = list(merged.values())

    if items:
        try:
            items = await personalize.rerank(items, user_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("personalize rerank failed: %s", exc)

    if not items:
        return {"status": "no_results", "intent": "search", "title": title,
                "files": [], "best": None, "meta": None,
                "parsed": parsed, "ai_note": None}

    # "Best logic" ordering: score (relevance + taste) -> quality -> size.
    # files[0] is always the best pick; the list runs best -> worst.
    from app.bot.v8_ui import sort_best_first
    items = sort_best_first(items)

    files = _query_season_episode(parsed, items)
    # v8.1: the most-downloaded file wins best pick (when any downloads
    # exist); otherwise the current best-logic order stands.
    top_dl = max(files, key=lambda f: f.get("downloads") or 0, default=None)
    if top_dl and (top_dl.get("downloads") or 0) > 0:
        files = [top_dl] + [f for f in files if f.get("id") != top_dl.get("id")]
    best = files[0]

    meta = await enrich_mod.enrich_title(title or q, parsed.get("year"),
                                         user_id)
    ai_note = await _ai_verdict(user_id, best, title or q)

    return {"status": "ok", "intent": "search", "title": title,
            "files": files, "best": best, "meta": meta,
            "parsed": parsed, "ai_note": ai_note}
