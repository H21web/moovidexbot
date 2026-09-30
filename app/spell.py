"""Smart spell suggestions using pg_trgm — no external AI needed.

``word_similarity``-style matching against distinct indexed filenames turns
``"avngers"`` into a tappable ``"Did you mean Avengers?"`` button.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.models import File
from app.textutil import clean_title

log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.3


async def suggest(session, query: str, limit: int = 3) -> list[str]:
    """Return up to ``limit`` cleaned title suggestions for a query."""
    q = (query or "").strip()
    if len(q) < 3:
        return []
    try:
        sim = func.similarity(File.file_name, q)
        # NOTE: no SELECT DISTINCT here — Postgres requires ORDER BY
        # expressions to appear in the select list under DISTINCT, so we
        # order by the similarity label and dedupe in Python instead.
        stmt = (
            select(File.file_name, sim.label("sim"))
            .where(sim > SIMILARITY_THRESHOLD)
            .order_by(sim.desc())
            .limit(limit * 3)
        )
        rows = (await session.execute(stmt)).all()
    except Exception as exc:  # noqa: BLE001 - suggestions are best-effort
        log.warning("spell suggest failed: %s", exc)
        return []

    seen: set[str] = set()
    out: list[str] = []
    for raw, _sim in rows:
        title = clean_title(raw)
        key = title.lower()
        if title and key not in seen:
            seen.add(key)
            out.append(title)
        if len(out) >= limit:
            break
    return out
