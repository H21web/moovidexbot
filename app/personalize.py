"""Per-user preference profiles: learn from downloads, re-rank searches.

No AI, no external calls — pure counters + arithmetic (microseconds).

How it works:
- Every successful file download fires ``record_download()`` (fire-and-forget
  from the delivery path). It extracts signals from the file
  (quality/language/size bucket/codec, genre via TMDB when available) and
  bumps per-user counters in ``user_prefs``.
- ``rerank()`` multiplies each search hit's base score by
  ``1 + matched preference weight`` — but only once the user has >= 10
  downloads (cold start) and only if they didn't disable it in /settings.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from sqlalchemy import select

from app.config import settings
from app.db import get_session_factory
from app.models import UserPref
from app.textutil import (
    canon_quality,
    clean_title,
    detect_quality_language,
    extract_year,
)

log = logging.getLogger(__name__)

# Cold start: personalize only after this many downloads.
MIN_DOWNLOADS = 10

# Per-category influence on the final boost (tuned conservatively so
# relevance still dominates; preference only re-orders close matches).
CAT_WEIGHT = {
    "quality": 0.60,
    "language": 0.50,
    "genre": 0.40,
    "size": 0.30,
    "codec": 0.30,
}
MAX_BOOST = 2.0

_CODEC_RE = re.compile(r"\b(x264|x265|hevc|10bit|av1)\b", re.IGNORECASE)

# --- tiny L1 cache: user_id -> (timestamp, prefs dict) -----------------------
_prefs_cache: dict[int, tuple[float, dict]] = {}
_PREFS_TTL = 60


def size_bucket(size: int | None) -> str | None:
    """S < 500MB, M 500MB–1.5GB, L > 1.5GB."""
    if not size:
        return None
    if size < 500 * 1024 * 1024:
        return "S"
    if size < 1536 * 1024 * 1024:
        return "M"
    return "L"


def extract_signals(f: dict) -> dict[str, list[str]]:
    """Signals learnable from one file dict (file_name/file_size/...)."""
    name = f.get("file_name") or ""
    quality, language = detect_quality_language(name)
    sig: dict[str, list[str]] = {}
    if quality:
        sig["quality"] = [canon_quality(quality) or quality]
    if language:
        sig["language"] = [language]
    sb = size_bucket(f.get("file_size"))
    if sb:
        sig["size"] = [sb]
    m = _CODEC_RE.search(name)
    if m:
        c = m.group(1).lower()
        sig["codec"] = ["hevc" if c in ("x265", "hevc", "10bit") else c]
    return sig


def _blank_prefs() -> dict:
    return {"enabled": True, "downloads": 0, "counters": {}}


async def get_prefs(user_id: int) -> dict:
    """Return the user's pref dict (L1-cached 60s). Never raises."""
    now = time.time()
    hit = _prefs_cache.get(user_id)
    if hit and now - hit[0] < _PREFS_TTL:
        return hit[1]
    prefs = _blank_prefs()
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            row = await session.get(UserPref, user_id)
            if row:
                prefs = {
                    "enabled": bool(row.enabled),
                    "downloads": row.downloads or 0,
                    "counters": dict(row.counters or {}),
                }
    except Exception as exc:  # noqa: BLE001 - personalization never breaks search
        log.debug("get_prefs failed: %s", exc)
    _prefs_cache[user_id] = (now, prefs)
    return prefs


def _bump(counters: dict, cat: str, value: str) -> None:
    cat_d = counters.setdefault(cat, {})
    cat_d[value] = cat_d.get(value, 0) + 1


async def record_download(user_id: int, f: dict) -> None:
    """Learn from one download. Fire-and-forget; never raises."""
    try:
        signals = extract_signals(f)
        genres: list[str] = []
        # Genre via TMDB (30-day PG cache — usually a cheap cache hit).
        try:
            from app.tmdb import get_movie

            meta = await get_movie(clean_title(f.get("file_name")),
                                   extract_year(f.get("file_name")))
            if meta and meta.get("genres"):
                genres = list(meta["genres"])[:3]
        except Exception as exc:  # noqa: BLE001 - genre is best-effort
            log.debug("pref genre lookup failed: %s", exc)

        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            row = await session.get(UserPref, user_id)
            if row is None:
                row = UserPref(user_id=user_id, enabled=True, downloads=0,
                               counters={})
                session.add(row)
            counters = dict(row.counters or {})
            for cat, vals in signals.items():
                for v in vals:
                    _bump(counters, cat, v)
            for g in genres:
                _bump(counters, "genre", g)
            row.counters = counters
            row.downloads = (row.downloads or 0) + 1
            await session.commit()
        _prefs_cache.pop(user_id, None)
    except Exception as exc:  # noqa: BLE001
        log.debug("record_download failed: %s", exc)


def _boost_for(item: dict, counters: dict) -> float:
    """Preference boost for one search hit (0.0 .. MAX_BOOST)."""
    sig = extract_signals(item)
    boost = 0.0
    for cat, vals in sig.items():
        cat_c = counters.get(cat) or {}
        total = sum(cat_c.values()) or 1
        for v in vals:
            c = cat_c.get(v, 0)
            if c:
                boost += (c / total) * CAT_WEIGHT.get(cat, 0.2)
    # Genre boost: match item's TMDB genres against learned genre counters.
    # (Done lazily here is too slow — genre boost applies at format time
    # via quality/language/size only. Kept simple by design.)
    return min(boost, MAX_BOOST)


async def rerank(items: list[dict], user_id: int | None) -> list[dict]:
    """Re-rank search hits by user taste. Returns the (possibly new) list."""
    if not items or not user_id:
        return items
    prefs = await get_prefs(user_id)
    if not prefs["enabled"] or prefs["downloads"] < MIN_DOWNLOADS:
        return items
    counters = prefs["counters"]
    if not counters:
        return items
    # P1#9: copy before mutating — items may come from a shared cache,
    # and compounding boost on cache hits would skew scores permanently.
    out = [dict(it) for it in items]
    for it in out:
        base = it.get("score") or 0.0
        it["score"] = base * (1.0 + _boost_for(it, counters))
        it["personalized"] = True
    out.sort(key=lambda i: i.get("score", 0), reverse=True)
    return out


def quality_order(prefs: dict) -> list[str]:
    """Quality labels ordered by learned preference (most-loved first)."""
    counts = (prefs.get("counters") or {}).get("quality") or {}
    ordered = sorted(counts, key=lambda q: counts[q], reverse=True)
    # Append any canonical qualities not seen yet, best-first.
    for q in ("1080p", "720p", "480p", "2160p", "4320p"):
        if q not in ordered:
            ordered.append(q)
    return ordered


async def set_enabled(user_id: int, enabled: bool) -> None:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        row = await session.get(UserPref, user_id)
        if row is None:
            row = UserPref(user_id=user_id, enabled=enabled, downloads=0,
                           counters={})
            session.add(row)
        else:
            row.enabled = enabled
        await session.commit()
    _prefs_cache.pop(user_id, None)


async def reset(user_id: int) -> None:
    """Forget everything learned (keeps the toggle state)."""
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        row = await session.get(UserPref, user_id)
        if row:
            row.counters = {}
            row.downloads = 0
            await session.commit()
    _prefs_cache.pop(user_id, None)


def fire_record_download(user_id: int, f: dict) -> None:
    """Non-blocking hook for the delivery path."""
    asyncio.create_task(record_download(user_id, f))


# --- best pick: the user's keywords and taste choose -------------------------
# v10.2: the best pick is no longer "first after sort" — every candidate is
# scored on relevance + the user's explicit keywords (language/quality in
# THIS query) + learned taste (language/quality from past downloads) +
# popularity (downloads). Reasons are returned so the card can say WHY.
import math as _math

# v10.2: weights are scaled so the USER's intent decides the pick —
# explicit query language/quality first, learned taste second, raw
# relevance and download-count only as tie-break signals.
_PICK_W = {
    "relevance": 1.0,    # base relevance score from the search ranker
    "lang_query": 6.0,   # language the user typed in this query
    "qual_query": 3.0,   # quality the user typed in this query
    "lang_pref": 2.0,    # language the user usually downloads
    "qual_pref": 1.0,    # quality the user usually downloads
    "downloads": 0.5,    # log-scaled popularity (one signal among many)
    "keywords": 1.5,     # per extra query word found in the filename
}


def _top_of(counters: dict, cat: str) -> str | None:
    cat_c = counters.get(cat) or {}
    if not cat_c:
        return None
    return max(cat_c, key=lambda k: cat_c[k])


async def choose_best(files: list[dict], parsed: dict,
                      user_id: int | None) -> tuple[dict | None, list[str]]:
    """Pick the best file the way the user would.

    Returns ``(best, reasons)``. Deterministic, quota-free, never raises.
    """
    if not files:
        return None, []
    prefs: dict = {}
    if user_id:
        try:
            prefs = await get_prefs(user_id)
        except Exception:  # noqa: BLE001
            prefs = {}
    counters = (prefs or {}).get("counters") or {}
    q_lang = (parsed.get("language") or "").lower() or None
    q_qual = (parsed.get("quality") or "").lower() or None
    pref_lang = (_top_of(counters, "language") or "").lower() or None
    pref_qual = (_top_of(counters, "quality") or "").lower() or None
    qwords = [w for w in re.split(r"\s+", (parsed.get("query") or "").lower())
              if len(w) >= 3]

    def _score(qlang: str | None, qqual: str | None):
        """Score every file; also report whether any file matched the
        explicit language/quality asks."""
        out = []
        lang_hit = qual_hit = False
        for f in files:
            name = (f.get("file_name") or "").lower()
            f_lang = (f.get("language") or "").lower()
            f_qual = (f.get("quality") or "").lower()
            score = _PICK_W["relevance"] * float(f.get("score") or 0.0)
            reasons: list[str] = []
            if qlang and f_lang == qlang:
                score += _PICK_W["lang_query"]
                reasons.append(f_lang)
                lang_hit = True
            elif pref_lang and f_lang == pref_lang and not qlang:
                score += _PICK_W["lang_pref"]
                reasons.append(f"{f_lang} (your usual)")
            if qqual and f_qual == qqual:
                score += _PICK_W["qual_query"]
                reasons.append(f_qual)
                qual_hit = True
            elif pref_qual and f_qual == pref_qual and not qqual:
                score += _PICK_W["qual_pref"]
                reasons.append(f"{f_qual} (your usual)")
            dls = f.get("downloads") or 0
            if dls > 0:
                score += _PICK_W["downloads"] * _math.log10(1 + dls)
                if dls >= 5:
                    reasons.append(f"⬇ {dls}")
            hits = sum(1 for w in qwords if w in name)
            if hits:
                score += _PICK_W["keywords"] * hits
            # Bigger file wins ties (usually the better encode).
            out.append((score, f.get("file_size") or 0, reasons, f))
        return out, lang_hit, qual_hit

    # If the user asked for a language/quality that NO file has, drop that
    # ask and fall back to learned taste instead of raw relevance.
    scored, lang_hit, qual_hit = _score(q_lang, q_qual)
    if (q_lang and not lang_hit) or (q_qual and not qual_hit):
        scored, _, _ = _score(
            None if (q_lang and not lang_hit) else q_lang,
            None if (q_qual and not qual_hit) else q_qual)

    best: dict | None = None
    best_score = float("-inf")
    best_reasons: list[str] = []
    for score, tiebreak, reasons, f in scored:
        if (score, tiebreak) > (best_score, (best or {}).get("file_size") or 0):
            best_score, best, best_reasons = score, f, reasons
    return best, best_reasons[:3]
