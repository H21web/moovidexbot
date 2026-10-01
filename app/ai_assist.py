"""v9.1 AI assistant — AI only where search truly fails.

Two AI touchpoints, nothing on the hot path:

1. **No results** — :func:`assist_no_results` runs the recovery chain:
   AI title correction -> DB retry -> spell suggestions -> give up
   honestly. This is the ONLY search-time AI besides the verdict.
2. **Verdict** — :func:`ai_verdict` writes the one-line best-pick note.
   AI first; a deterministic local line as fallback so the 💡 verdict
   ALWAYS shows (it was starving when v9's router/parse/judge burned
   the quota).

The old "uncertain" AI judge was removed in v9.1 (speed + quota).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


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


def _local_verdict(best: dict, title: str) -> str:
    """Deterministic verdict so the 💡 line ALWAYS renders."""
    dl = best.get("downloads") or 0
    if dl:
        return f"Most downloaded pick — {dl} downloads"
    bits = [x for x in (best.get("quality"), best.get("language")) if x]
    if bits:
        return f"Best {' '.join(bits)} match for \u201c{title}\u201d"
    return f"Top match for \u201c{title}\u201d"


async def ai_verdict(user_id: int | None, best: dict,
                     title: str) -> str:
    """One-line best-pick note. AI first, local fallback — never empty."""
    try:
        from app.ai_search import _ai_verdict
        note = await _ai_verdict(user_id or 0, best, title)
        if note:
            return note
    except Exception as exc:  # noqa: BLE001
        log.debug("v9 verdict failed: %s", exc)
    note = _local_verdict(best, title or "")
    log.info("v9 verdict: local fallback %r", note[:60])
    return note
