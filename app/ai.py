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

TITLE_LIST_SYSTEM = """You are a movie-title correction module for a Telegram AutoFilter bot.

Identify the likely intended movie or series title from the user's search. Handle spelling mistakes, missing spaces, transliteration errors, incomplete words, and technical filename words.

Rules:
- Return JSON only. No Markdown.
- Return 1–5 likely titles, ordered from most to least likely.
- Include type: "movie" or "series".
- Include year when confident; otherwise null.
- If the query includes "similar", "related", "like", "season", "part", or a known franchise, return matching titles and related titles/seasons.
- Remove technical words: 480p, 720p, 1080p, 4k, WEB-DL, WEBRip, BluRay, x264, x265, MKV, MP4, dubbed, etc.
- Never claim availability, provide links, file IDs, or ask questions.
- Do not include language, quality, actor, director, genre, plot, or explanations.
- Return null only if no likely title can be identified.

Return exactly:
{"titles":[{"title":null,"year":null,"type":null,"reason":"direct|similar|season|franchise"}]}"""


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
_AI_SITE_SUFFIX_RE = re.compile(
    r"\s*[-–—|]\s*(imdb|wikipedia|rotten tomatoes|letterboxd)\s*$",
    re.IGNORECASE)


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
    t = line.strip()
    t = _LEAD_IN_RE.sub("", t).strip()
    t = _AI_SITE_SUFFIX_RE.sub("", t).strip()
    t = _TRAIL_YEAR_RE.sub("", t).strip()
    t = t.strip("*_` \t").strip("\"'\u201c\u201d\u2018\u2019").strip()
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


def _extract_json_text(raw: str | None) -> str:
    """Pull the JSON object out of a possibly chatty model reply.

    Strips markdown fences and trims everything outside the outermost
    ``{...}`` so ``json.loads`` sees only the object.
    """
    t = (raw or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t).strip()
    s, e = t.find("{"), t.rfind("}")
    if 0 <= s < e:
        t = t[s:e + 1]
    return t


def _parse_title_list(raw: str | None, q: str) -> list[dict]:
    """Parse Grok's ``{"titles": [{title, year, type, reason}]}`` reply.

    Returns cleaned title dicts, most-likely first. Empty list when
    Grok can't identify anything.
    """
    if not raw:
        return []
    try:
        data = json.loads(_extract_json_text(raw))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("titles")
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not title or not isinstance(title, str):
            continue
        # Local cleaner: second safety net so the DB search runs on
        # the bare title only.
        title = clean_ai_title(title, q)
        if not title:
            continue
        year = item.get("year")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None
        if year is not None and not 1900 <= year <= 2100:
            year = None
        typ = item.get("type")
        typ = typ if typ in ("movie", "series") else None
        reason = item.get("reason")
        reason = reason if isinstance(reason, str) else None
        out.append({"title": title, "year": year, "type": typ,
                    "reason": reason})
    return out


async def ai_extract_titles(user_id: int | None, q: str,
                            sid: str | None = None) -> list[dict]:
    """Ask Grok for the intended title(s). Original query only.

    Returns 0-5 cleaned ``{title, year, type, reason}`` dicts,
    most-likely first. Empty list when unconfigured, out of quota,
    or Grok can't tell.
    """
    q = (q or "").strip()
    if not q or not is_configured():
        return []
    if await quota_remaining(user_id) <= 0:
        log.debug("ai_extract_titles: quota exhausted for %s", user_id)
        return []
    raw = await groq_complete(TITLE_LIST_SYSTEM, q[:200], max_tokens=400,
                              json_mode=False)
    titles = _parse_title_list(raw, q)
    if not titles:
        log.info("[s:%s] grok: no title identified for %r", sid or "-",
                 q[:60])
        return []
    await quota_use(user_id)
    log.info("[s:%s] grok titles %r -> %r", sid or "-", q[:60],
             [(t["title"][:40], t["type"], t["year"]) for t in titles])
    return titles
