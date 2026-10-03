"""Grok AI fallback for search — v10.6.

Per the user's flow diagram, AI exists for ONE purpose: correcting the
movie/series title when the Search API found no usable title ("Call Grok
AI with original user query only"). Model is ``openai/gpt-oss-20b``
(the live-confirmed working model), 50 uses/user/day.

No chat, no web answers, no similar-movies, no memory — those were
removed in v10.4 and stay removed.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re

import httpx

from app.config import settings

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

TITLE_SYSTEM = """You are a movie title correction module for a Telegram AutoFilter bot.

Your only task is to identify the likely intended movie title from a user search containing spelling mistakes, missing spaces, incorrect transliteration, or incomplete words.

Rules:
1. Return JSON only. Do not use Markdown.
2. Return only one likely corrected movie title, or null.
3. Never claim that the movie exists in the bot database.
4. Never provide file names, file IDs, Telegram links, download links, streaming links, or availability information.
5. Do not ask the user any questions.
6. Do not mention language, video quality, actor, director, genre, plot, release date, or explanation.
7. Remove technical filename words such as 480p, 720p, 1080p, 4k, WEB-DL, WEBRip, BluRay, x264, x265, MKV, MP4, and dubbed.
8. Correct only when reasonably confident.
9. If you cannot identify one likely title, return null.
10. Do not include words not related to the movie title.

Return exactly this JSON format:

{
  "corrected_title": null,
  "confidence": 0.0
}"""

# Minimum self-reported confidence before a Grok correction is used.
MIN_AI_CONFIDENCE = 0.6

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=20,
                                max_keepalive_connections=10),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001
            pass
        _client = None


def is_configured() -> bool:
    return bool(settings.GROQ_API_KEY)


# --- quota: 50 Grok uses / user / day ---------------------------------------
_quota: dict[int, list] = {}
_quota_lock = asyncio.Lock()


def _today() -> str:
    return datetime.date.today().isoformat()


async def quota_remaining(user_id: int | None) -> int:
    if user_id is None:
        return settings.AI_DAILY_QUOTA
    async with _quota_lock:
        day, used = _quota.get(user_id, (_today(), 0))
        if day != _today():
            return settings.AI_DAILY_QUOTA
        return max(0, settings.AI_DAILY_QUOTA - used)


async def quota_use(user_id: int | None) -> None:
    if user_id is None:
        return
    async with _quota_lock:
        day, used = _quota.get(user_id, (_today(), 0))
        if day != _today():
            day, used = _today(), 0
        _quota[user_id] = [day, used + 1]


# --- Groq call ---------------------------------------------------------------
async def groq_complete(system: str, prompt: str,
                        max_tokens: int = 300,
                        json_mode: bool = False) -> str | None:
    """One Groq chat completion; returns the text or ``None``.

    v10.3.1: retry once without ``response_format`` when Groq answers
    ``json_validate_failed`` (gpt-oss-20b quirk).
    """
    if not is_configured():
        return None
    payload = {
        "model": settings.AI_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {settings.GROQ_API_KEY}"}
    try:
        r = await _get_client().post(GROQ_URL, json=payload,
                                     headers=headers)
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = exc.response.text or ""
        except Exception:  # noqa: BLE001
            pass
        if json_mode and "json_validate_failed" in body:
            log.info("groq json_validate_failed — retrying without "
                     "response_format")
            try:
                payload.pop("response_format", None)
                r = await _get_client().post(GROQ_URL, json=payload,
                                             headers=headers)
                r.raise_for_status()
            except Exception as exc2:  # noqa: BLE001
                log.warning("groq retry failed: %s", exc2)
                return None
        else:
            log.warning("groq failed: %s", exc)
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("groq failed: %s", exc)
        return None
    try:
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:  # noqa: BLE001
        return None


# --- the one AI feature: title correction ------------------------------------
_EXPLAIN_RE = re.compile(
    r"\b(the movie is|this is|i think|probably|maybe|could be|it is|"
    r"refers to|looks like)\b", re.IGNORECASE)
_LEAD_IN_RE = re.compile(
    r"^(title|movie|series|answer|corrected title|result)\s*:\s*",
    re.IGNORECASE)
_TRAIL_YEAR_RE = re.compile(
    r"\s*(?:[\(\[]\s*)?(19\d{2}|20\d{2})(?:\s*[\)\]])?\s*$")


def clean_ai_title(raw: str | None, original_q: str) -> str | None:
    """Extract ONLY the movie/series title from Grok's raw reply.

    Grok sometimes adds quotes, a year, markdown, a lead-in ("Title:")
    or an explanation line — all of that is stripped so the DB search
    runs on the bare title only. Returns ``None`` when nothing usable
    remains.
    """
    if not raw:
        return None
    # first non-empty line only — drops any explanation after it
    line = ""
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln:
            line = ln
            break
    if not line:
        return None
    t = line.strip().strip("*_` \t")
    t = _LEAD_IN_RE.sub("", t).strip()
    t = _TRAIL_YEAR_RE.sub("", t).strip()
    t = t.strip("\"'\u201c\u201d\u2018\u2019").strip()
    if not t or len(t) > 100:
        return None
    if t.upper() == "NONE":
        return None
    if len(t.split()) > 12:
        return None  # a sentence, not a title
    if _EXPLAIN_RE.search(t):
        return None  # explanation, not a title
    if t.lower() == (original_q or "").strip().lower():
        return None  # no correction offered
    return t


def _parse_title_json(raw: str | None) -> tuple[str | None, float]:
    """Parse Grok's ``{"corrected_title", "confidence"}`` reply."""
    if not raw:
        return None, 0.0
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None, 0.0
    if not isinstance(data, dict):
        return None, 0.0
    title = data.get("corrected_title")
    if not title or not isinstance(title, str):
        return None, 0.0
    try:
        conf = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return title.strip(), conf


async def ai_extract_title(user_id: int | None, q: str) -> str | None:
    """Ask Grok for the intended title. Original query only — never any
    search-API response. ``None`` when unconfigured, out of quota,
    low confidence, or Grok can't tell."""
    q = (q or "").strip()
    if not q or not is_configured():
        return None
    if await quota_remaining(user_id) <= 0:
        log.debug("ai_extract_title: quota exhausted for %s", user_id)
        return None
    raw = await groq_complete(TITLE_SYSTEM, q[:200], max_tokens=120,
                              json_mode=True)
    title, conf = _parse_title_json(raw)
    if title is None or conf < MIN_AI_CONFIDENCE:
        log.debug("ai_extract_title: rejected (confidence %.2f)", conf)
        return None
    # Second safety net: the local cleaner strips any junk the model
    # left behind so the DB search runs on the bare title only.
    title = clean_ai_title(title, q)
    if not title:
        return None
    await quota_use(user_id)
    log.info("grok title %r -> %r (%.2f)", q[:60], title[:60], conf)
    return title
