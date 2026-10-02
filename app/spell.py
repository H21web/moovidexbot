"""Smart spell correction for search queries — no AI, no quota.

Two tools:

``correct_query`` — *auto*-fixes minor misspellings (``"avangerrs"`` ->
``"avengers"``) using word-level Levenshtein distance against a cached
vocabulary of words that actually appear in indexed titles
(``File.title_key``). Only fires on the recovery path, never on the hot
path.

``suggest`` — "Did you mean?" candidates via pg_trgm similarity against
``title_key`` (clean, tag-free titles — far less noisy than raw
file names).
"""
from __future__ import annotations

import logging
import re
import time

from sqlalchemy import func, select

from app.models import File
from app.textutil import clean_title

log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.35

# --- auto-correction ---------------------------------------------------------
_VOCAB: tuple[float, frozenset[str]] = (0.0, frozenset())
_VOCAB_TTL = 3600.0  # refresh hourly
_VOCAB_WORD_RE = re.compile(r"[a-z]{3,}")
# Words that must never be "corrected" (filters / noise handled elsewhere).
_SKIP_WORDS = frozenset({
    "full", "movie", "download", "hindi", "tamil", "telugu", "malayalam",
    "kannada", "english", "dual", "audio", "with", "subtitles", "season",
    "episode", "part", "vol", "volume",
})


def _levenshtein(a: str, b: str, cap: int) -> int:
    """Edit distance with early exit when it exceeds ``cap``."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    prev = list(range(la + 1))
    for j in range(1, lb + 1):
        cur = [j]
        bj = b[j - 1]
        row_min = j
        for i in range(1, la + 1):
            cost = 0 if a[i - 1] == bj else 1
            v = min(prev[i] + 1, cur[i - 1] + 1, prev[i - 1] + cost)
            cur.append(v)
            if v < row_min:
                row_min = v
        if row_min > cap:
            return cap + 1
        prev = cur
    return prev[la]


async def _title_vocab(session_factory) -> frozenset[str]:
    """All distinct words appearing in indexed title keys (cached 1h)."""
    global _VOCAB
    now = time.time()
    if now - _VOCAB[0] < _VOCAB_TTL and _VOCAB[1]:
        return _VOCAB[1]
    words: set[str] = set()
    try:
        async with session_factory() as session:
            rows = (await session.execute(
                select(File.title_key).where(File.title_key.isnot(None))
                # v10.3: bounded — most-downloaded titles first so the
                # vocab keeps its quality without a full-table scan.
                .order_by(File.downloads.desc()).limit(25000)
            )).scalars().all()
        for tk in rows:
            if not tk:
                continue
            for w in _VOCAB_WORD_RE.findall(tk.lower()):
                if w not in _SKIP_WORDS:
                    words.add(w)
    except Exception as exc:  # noqa: BLE001 - correction is best-effort
        log.debug("spell vocab build failed: %s", exc)
        return _VOCAB[1]
    log.info("spell vocab: %d words", len(words))
    _VOCAB = (now, frozenset(words))
    return _VOCAB[1]


def _max_dist(word: str) -> int:
    if len(word) <= 4:
        return 1
    if len(word) <= 7:
        return 2
    return 3


def _closest(word: str, vocab: frozenset[str]) -> str | None:
    """Closest vocab word within the allowed edit distance, else None."""
    cap = _max_dist(word)
    # Bucket by first letter + nearby length: ~100x fewer comparisons.
    best: str | None = None
    best_d = cap + 1
    prefix = word[:1]
    for cand in vocab:
        if not cand.startswith(prefix):
            continue
        if abs(len(cand) - len(word)) > cap:
            continue
        d = _levenshtein(word, cand, best_d - 1)
        if d < best_d:
            best_d = d
            best = cand
    return best if best_d <= cap else None


async def correct_query(raw_query: str, session_factory) -> str | None:
    """Auto-correct minor misspellings. Returns the fixed query or None.

    ``"avangerrs endgame"`` -> ``"avengers endgame"``. Returns ``None``
    when nothing needs fixing (or no confident fix exists).
    """
    q = (raw_query or "").strip()
    if len(q) < 4:
        return None
    vocab = await _title_vocab(session_factory)
    if not vocab:
        return None
    words = q.split()
    out: list[str] = []
    changed = False
    for w in words:
        stripped = re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$", "", w)
        lw = stripped.lower()
        if (
            len(lw) < 4
            or lw in vocab
            or lw in _SKIP_WORDS
            or lw.isdigit()
            or re.fullmatch(r"(19|20)\d{2}", lw)
        ):
            out.append(w)
            continue
        fix = _closest(lw, vocab)
        if fix:
            out.append(fix if w.islower() else fix.capitalize())
            changed = True
        else:
            out.append(w)
    if not changed:
        return None
    fixed = " ".join(out)
    log.info("spell auto-correct %r -> %r", q[:60], fixed[:60])
    return fixed


# --- suggestions -------------------------------------------------------------
async def suggest(session, query: str, limit: int = 3) -> list[str]:
    """Return up to ``limit`` "Did you mean?" title suggestions."""
    q = (query or "").strip()
    if len(q) < 3:
        return []
    try:
        sim = func.similarity(File.title_key, q)
        stmt = (
            select(File.title_key, sim.label("sim"))
            .where(File.title_key.isnot(None))
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
