"""v10.2 AI assistant — AI where it genuinely earns its keep, never on the
hot path.

Search-time AI (recovery only):

1. **No results** — :func:`assist_no_results` runs the recovery chain:
   local spell correction -> web-search title parse -> AI title
   extraction -> DB retry -> spell suggestions -> give up honestly.
2. **Verdict** — :func:`verdict_line` writes the one-line best-pick note
   with the deterministic technique (downloads / quality / language).
   Zero AI, zero quota — it always shows.

On-demand AI (user taps, 1 quota each):

3. **🍿 Similar movies** — :func:`ai_similar_titles` suggests titles
   like the one just found, from the model's own knowledge.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

_EXTRACT_SYSTEM = (
    "Extract the movie or TV series title from the user's search text. "
    "Ignore quality words (1080p, 720p, camrip), language words, and "
    "words like 'movie', 'download', 'full', 'watch', 'series', "
    "'season', 'episode'. Reply with ONLY the title, nothing else. "
    "If there is no movie or series title in the text, reply exactly: NONE."
)

_SIMILAR_SYSTEM = (
    "Suggest movies or TV series similar to the given title. Reply with "
    "ONLY a JSON array of 6 titles, e.g. "
    '["Title One", "Title Two"]. No other text, no numbering. '
    "If you cannot suggest any, reply exactly: NONE."
)


async def ai_extract_title(user_id: int | None, q: str) -> str | None:
    """3rd search logic: let AI pull the canonical title from the query.

    Quota-gated (1 use). Returns the title or ``None``. Never raises.
    """
    from app import ai as ai_mod
    if not ai_mod.is_configured() or not q or not q.strip():
        return None
    uid = user_id or 0
    try:
        if await ai_mod.quota_remaining(uid) <= 0:
            log.debug("ai_extract_title: no quota")
            return None
        raw = await ai_mod.groq_complete(
            _EXTRACT_SYSTEM, q.strip()[:200], max_tokens=60)
    except Exception as exc:  # noqa: BLE001
        log.debug("ai_extract_title failed: %s", exc)
        return None
    if not raw:
        return None
    title = raw.strip().strip("\"'").split("\n")[0].strip()
    if not title or title.upper() == "NONE" or len(title) > 120:
        return None
    await ai_mod.quota_use(uid)
    log.info("ai_extract_title %r -> %r", q[:60], title[:60])
    return title


async def ai_similar_titles(user_id: int | None, title: str) -> list[str]:
    """🍿 Similar titles for a movie/series, from AI knowledge.

    Quota-gated (1 use). Returns up to 6 titles, possibly []. Never raises.
    """
    from app import ai as ai_mod
    if not ai_mod.is_configured() or not title or not title.strip():
        return []
    uid = user_id or 0
    try:
        if await ai_mod.quota_remaining(uid) <= 0:
            return []
        raw = await ai_mod.groq_complete(
            _SIMILAR_SYSTEM, title.strip()[:120],
            max_tokens=200, json_mode=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("ai_similar_titles failed: %s", exc)
        return []
    if not raw or raw.strip().upper() == "NONE":
        return []
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        # Not JSON — salvage line-separated titles.
        data = [ln.strip(" -•\t\"'") for ln in raw.splitlines()
                if ln.strip(" -•\t\"'")]
    titles = [str(t).strip()[:120] for t in (data or [])
              if str(t).strip()]
    if not titles:
        return []
    await ai_mod.quota_use(uid)
    log.info("ai_similar_titles for %r: %d titles", title[:60], len(titles))
    return titles[:6]


async def assist_no_results(user_id: int | None, q: str) -> dict:
    """Recovery chain for empty searches.

    Returns ``{"action": "retry", "query": ...}``,
    ``{"action": "suggest", "suggestions": [...]}``, or
    ``{"action": "none"}``.
    """
    # 1) AI corrects the title -> caller retries the search once.
    try:
        from app.ai_search import ai_suggest_title
        fix = await ai_suggest_title(user_id or 0, q)
        if fix and fix.lower() != q.lower():
            log.info("v9 assist: no results for %r, AI suggests %r",
                     q[:60], fix[:60])
            return {"action": "retry", "query": fix}
    except Exception as exc:  # noqa: BLE001
        log.debug("v9 assist title correction failed: %s", exc)

    # 2) DB spell suggestions ("did you mean?").
    try:
        from app.config import settings
        from app.db import get_session_factory
        from app.spell import suggest
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            suggestions = await suggest(session, q)
        if suggestions:
            return {"action": "suggest", "suggestions": suggestions}
    except Exception as exc:  # noqa: BLE001
        log.debug("v9 assist spell suggest failed: %s", exc)

    return {"action": "none"}


def verdict_line(best: dict, title: str) -> str:
    """v9.3: one-line best-pick note. Local only — no AI, no quota.

    Always returns a non-empty line, so the 💡 verdict renders on
    every result.
    """
    dl = (best or {}).get("downloads") or 0
    if dl:
        return f"Most downloaded pick — {dl} downloads"
    bits = [x for x in ((best or {}).get("quality"),
                        (best or {}).get("language")) if x]
    if bits:
        return f"Best {' '.join(bits)} match for \u201c{title}\u201d"
    return f"Top match for \u201c{title}\u201d"
