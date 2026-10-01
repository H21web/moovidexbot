"""v9 AI assistant — AI steps in exactly where search is uncertain.

Two situations, two helpers:

1. **No results** — :func:`assist_no_results` runs the recovery chain:
   AI title correction -> DB spell suggestions -> give up honestly.
2. **Uncertain results** — :func:`assist_uncertain` shows the weak
   candidates to Groq and keeps only the ones that genuinely match the
   query, with a one-line note explaining the call.

Plus :func:`ai_verdict`, the one-line best-pick recommendation.

Every helper degrades silently when AI is off or over quota — the bot
never breaks because the AI couldn't answer.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

JUDGE_SYSTEM = (
    "You judge whether file names match a movie/series search query. "
    "Reply with ONLY JSON, no other text.\n"
    'Schema: {"keep": [1, 3], "note": "one short line"}\n'
    "Rules:\n"
    "- keep = the 1-based numbers of files that are REALLY the requested "
    "movie/series (right title; year/language may differ).\n"
    "- A file is a match even if the spelling is slightly off.\n"
    "- If NONE match, keep is [].\n"
    '- note: one short line like "kept 2 of 8 — rest were other movies".'
)


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


async def assist_uncertain(user_id: int | None, q: str,
                           candidates: list[dict]) -> dict:
    """AI judges weak candidates; keeps genuine matches.

    Returns ``{"action": "filtered", "files": [...], "note": ...}``,
    ``{"action": "none_match", "note": ...}``, or
    ``{"action": "as_is"}`` when AI can't help.
    """
    if not candidates:
        return {"action": "as_is"}
    try:
        from app import ai as ai_mod
        if not ai_mod.is_configured() or await ai_mod.quota_remaining(
                user_id or 0) <= 0:
            return {"action": "as_is"}
        shown = candidates[:10]
        listing = "\n".join(
            f"{i + 1}. {(c.get('file_name') or '')[:90]} "
            f"[{c.get('quality') or '?'}/{c.get('language') or '?'}]"
            for i, c in enumerate(shown))
        raw = await ai_mod.groq_complete(
            JUDGE_SYSTEM,
            f"Query: {q[:120]}\nFiles:\n{listing}",
            max_tokens=200,
            json_mode=True,
        )
        if not raw:
            return {"action": "as_is"}
        data = json.loads(raw)
        keep = data.get("keep") or []
        note = (data.get("note") or "").strip()
        await ai_mod.quota_use(user_id or 0)
        kept = [shown[i - 1] for i in keep
                if isinstance(i, int) and 1 <= i <= len(shown)]
        if kept:
            log.info("v9 assist: uncertain %r -> kept %d/%d",
                     q[:60], len(kept), len(shown))
            return {"action": "filtered", "files": kept, "note": note}
        return {"action": "none_match",
                "note": note or "None of the files look like a real match."}
    except Exception as exc:  # noqa: BLE001
        log.debug("v9 assist judge failed: %s", exc)
        return {"action": "as_is"}


async def ai_verdict(user_id: int | None, best: dict,
                     title: str) -> str | None:
    """One-line AI note on why this file is the best pick (optional)."""
    try:
        from app.ai_search import _ai_verdict
        return await _ai_verdict(user_id or 0, best, title)
    except Exception as exc:  # noqa: BLE001
        log.debug("v9 verdict failed: %s", exc)
        return None
