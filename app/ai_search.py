"""AI search helpers — only the live pieces.

Kept:
- :func:`_query_season_episode` — narrows v9 results to the query's
  season/episode (used by :mod:`app.search_v9`).
- :func:`ai_suggest_title` — AI guess at the intended title when a search
  found nothing (used by :mod:`app.ai_assist`).

Removed (P3, verified dead — no callers anywhere): the v7 ``ai_search``
pipeline, ``v8_search``, ``ai_parse_query``, ``_local_parse``,
``_local_intent``, ``best_pick_line``, the module-local ``fmt_size``,
``PARSE_SYSTEM``, ``_INTENT_RE`` and ``_MESSY_RE``.
"""
from __future__ import annotations

import logging

from app import ai

log = logging.getLogger(__name__)

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
