"""v9.1 intent router — keyword rules only, no AI.

v9 called Groq on EVERY message (1-3s latency + quota burn per message),
which starved the best-pick verdict of quota. v9.1 drops the AI call
entirely: routing is instant and free. AI is reserved for real failures
(no-result spell correction, enrich fallback, verdict).
"""
from __future__ import annotations

import re

_VALID = ("movie_search", "question", "greeting", "request", "other")

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
_REQUEST_RE = re.compile(r"\b(request|add cheyy\w*|upload|please.*(add|upload))\b",
                         re.IGNORECASE)
# v9.2: a bare "title?" is a search, not a question. Question words that
# keep the "?" -> question routing. "undo" is deliberately excluded:
# "<movie> undo?" asks whether the file exists -> movie_search.
_QUESTION_WORD_RE = re.compile(
    r"\b(what|when|where|who|whom|whose|which|why|how|is|are|was|were|"
    r"do|does|did|can|could|will|would|should|may|etha|entha|enth|evide|"
    r"eppol|epo|aara|aar|ethra|engane|engine|aano|alle|aakumo)\b",
    re.IGNORECASE)


def _keyword_intent(text: str) -> str:
    """Instant keyword rules — no network, no quota."""
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
        # v9.2: "kgf?" (no question word) is a movie search; a real
        # question ("best movie etha?", "bot work cheyyunno?") keeps
        # question routing.
        if _QUESTION_WORD_RE.search(t):
            return "question"
        # Manglish verb question suffix: "cheyyunno?", "varumo?"
        if re.search(r"(unno|umo)\?\s*$", t, re.IGNORECASE):
            return "question"
        return "movie_search"
    if _QUESTION_START.match(t):
        return "question"
    if _ML_QUESTION_START.match(t):
        return "question"
    return "movie_search"


async def classify(user_id: int | None, text: str) -> str:
    """Classify a message. Never raises; always returns a valid intent."""
    t = (text or "").strip()
    if len(t) < 2:
        return "other"
    intent = _keyword_intent(t)
    return intent if intent in _VALID else "movie_search"
