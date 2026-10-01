"""v9 AI intent router.

Replaces the old prefix-based ``detect_intent`` ("what/when" words only).
Every non-command text message is classified by a cheap Groq call into:

- ``movie_search`` — user wants a movie/series file
- ``question``     — wants an answer / chat with the AI
- ``greeting``     — hi/hello/thanks/bye
- ``request``      — asking the bot to fetch/add something
- ``other``        — anything else (falls back to search)

Manglish-aware few-shot examples teach the model Malayalam chat patterns
("undo?", "aano?", "kaan anam"). When AI is off, over quota, or the call
fails, a keyword-rule fallback keeps the bot working — routing never
breaks search.

Results are cached per (user_id, text) for 10 minutes so repeated texts
cost nothing.
"""
from __future__ import annotations

import logging
import re
import time

log = logging.getLogger(__name__)

INTENT_SYSTEM = (
    "You classify a Telegram bot message into ONE word. Reply with ONLY "
    "the word, no other text.\n"
    "Classes:\n"
    "- movie_search: user wants a movie/series file "
    '("kgf", "kgf movie undo", "avatar 3 hindi 720p", "play kgf", '
    '"dheeram padam undo?", "money heist season 2")\n'
    "- question: user asks the bot something or wants to chat "
    '("bot work cheyyunno?", "ningal aara?", "best malayalam movie etha?", '
    '"thanks", no — thanks is greeting)\n'
    '- greeting: hi, hello, hey, thanks, thank you, bye ("hai", "nanni")\n'
    "- request: user asks the bot to get/add/find something for later "
    '("dheeram add cheyyamo?", "please upload kgf", "request kgf")\n'
    "- other: anything else\n"
    "Malayalam/Manglish counts: questions ending in undo/aano/alle/enth/evide "
    "about the BOT are question; about a MOVIE existing are movie_search."
)

_VALID = ("movie_search", "question", "greeting", "request", "other")

# --- keyword fallback (old detect_intent behavior, extended) ---------------
_QUESTION_START = re.compile(
    r"^(what|when|where|who|whom|whose|which|why|how|is|are|do|does|did|"
    r"can|could|will|would|should|may)\b", re.IGNORECASE)
_ML_QUESTION_START = re.compile(
    r"^(entha|enth|evide|eppol|epo|aara|aar|ethra|engine|engane)\b",
    re.IGNORECASE)
_ML_QUESTION_END = re.compile(
    r"(aano|alle|undo\?*|aakumo|aakum)\s*\??$", re.IGNORECASE)
_GREET_RE = re.compile(r"^(hi+|hello|hey|hai|thanks|thank you|nanni|bye)\b",
                       re.IGNORECASE)
_REQUEST_RE = re.compile(r"\b(request|add cheyy|upload|please.*(add|upload))\b",
                         re.IGNORECASE)


def _fallback_intent(text: str) -> str:
    """Keyword rules when AI routing is unavailable."""
    t = (text or "").strip()
    if not t:
        return "other"
    if _GREET_RE.match(t):
        return "greeting"
    if _REQUEST_RE.search(t):
        return "request"
    # Manglish "<title> ... undo/aano/alle?" is a movie search phrased as
    # a question — unless it's about the bot itself.
    if _ML_QUESTION_END.search(t):
        if re.search(r"\b(bot|nee|ningal|ninte)\b", t, re.IGNORECASE):
            return "question"
        return "movie_search"
    if "?" in t:
        return "question"
    if _QUESTION_START.match(t):
        return "question"
    if _ML_QUESTION_START.match(t):
        return "question"
    return "movie_search"


_cache: dict[tuple[int, str], tuple[float, str]] = {}
_TTL = 600


async def classify(user_id: int | None, text: str) -> str:
    """Classify a message. Never raises; always returns a valid intent."""
    t = (text or "").strip()
    if len(t) < 2:
        return "other"
    key = (user_id or 0, t.lower()[:120])
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]

    intent = _fallback_intent(t)
    try:
        from app import ai as ai_mod
        if ai_mod.is_configured() and await ai_mod.quota_remaining(
                user_id or 0) > 0:
            raw = await ai_mod.groq_complete(
                INTENT_SYSTEM,
                f"Classify: {t[:150]}",
                max_tokens=20,
                temperature=0.0,
            )
            word = (raw or "").strip().lower().strip(".,!?\"'")
            if word in _VALID:
                intent = word
                await ai_mod.quota_use(user_id or 0)
            else:
                log.debug("intent router: unexpected %r, fallback %s",
                          raw, intent)
    except Exception as exc:  # noqa: BLE001 - routing never breaks
        log.debug("intent router failed, fallback %s: %s", intent, exc)

    _cache[key] = (time.time(), intent)
    if len(_cache) > 2000:
        _cache.clear()
    return intent
