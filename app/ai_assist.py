"""v9.1 AI assistant — AI only where search truly fails.

Two AI touchpoints, nothing on the hot path:

1. **No results** — :func:`assist_no_results` runs the recovery chain:
   AI title correction -> DB retry -> spell suggestions -> give up
   honestly. This is the ONLY search-time AI besides the verdict.
2. **Verdict** — :func:`verdict_line` writes the one-line best-pick note
   with the old deterministic technique (downloads / quality /
   language). Zero AI, zero quota — it always shows.

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
