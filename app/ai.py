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
import time

import httpx

from app.config import settings

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Per-key cooldowns: {api_key: unix_ts_until}. Set when Groq answers
# rate_limit_exceeded for that key; the key is skipped until then.
_KEY_COOLDOWN: dict[str, float] = {}
# Keys rejected with 401 (invalid/revoked) — never retried in-process.
_DEAD_KEYS: set[str] = set()


def _groq_keys() -> list[str]:
    """All configured Groq keys: GROQ_API_KEY first, then GROQ_API_KEYS."""
    keys: list[str] = []
    primary = (getattr(settings, "GROQ_API_KEY", "") or "").strip()
    if primary:
        keys.append(primary)
    extra = (getattr(settings, "GROQ_API_KEYS", "") or "").strip()
    for k in extra.split(","):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    return keys


def _live_keys() -> list[str]:
    """Keys usable right now: not dead (401) and not in cooldown."""
    now = time.time()
    return [k for k in _groq_keys()
            if k not in _DEAD_KEYS and _KEY_COOLDOWN.get(k, 0) <= now]


def _parse_retry_after(body: str) -> float:
    """Parse Groq's 'Please try again in 4m43.824s' hint; default 300s."""
    m = re.search(r"try again in (\d+)m([\d.]+)s", body)
    if m:
        return float(m.group(1)) * 60 + float(m.group(2))
    m = re.search(r"try again in ([\d.]+)s", body)
    if m:
        return float(m.group(1))
    return 300.0


# --- repeated-search cache ------------------------------------------------
# v10.8.9: the same zero-result query often repeats (user retries, or
# several users ask the same thing). Cache Grok's answer 6h so repeats
# cost zero tokens and zero quota.
_TITLES_TTL = 60 * 60 * 6
_titles_cache: dict[str, tuple[float, list[dict]]] = {}


def _titles_cache_get(q: str) -> list[dict] | None:
    hit = _titles_cache.get((q or "").strip().lower())
    if hit and time.time() - hit[0] < _TITLES_TTL:
        return hit[1]
    return None


def _titles_cache_put(q: str, titles: list[dict]) -> None:
    key = (q or "").strip().lower()
    if not key:
        return
    _titles_cache[key] = (time.time(), titles)
    if len(_titles_cache) > 500:
        for k in sorted(_titles_cache, key=lambda k: _titles_cache[k][0])[:100]:
            _titles_cache.pop(k, None)

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
- You have a browser search tool. Use it when the query is ambiguous,
  names an actor/director, or asks about a new/recent/unknown title.

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
    return bool(_groq_keys())


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
async def groq_complete(messages: list[dict],
                        max_tokens: int = 300,
                        tools: list[dict] | None = None,
                        tool_choice: str | None = None,
                        reasoning_effort: str | None = None) -> str | None:
    """One Groq chat completion; returns the text or ``None``.

    v10.3.1: retry once without ``response_format`` when Groq answers
    ``json_validate_failed`` (gpt-oss-20b quirk).
    v10.8.2: ``tools=[{"type": "browser_search"}]`` gives gpt-oss-20b
    Groq's built-in web search (server-side, no extra setup). Note:
    browser search is NOT compatible with ``response_format`` — never
    combine them.
    v10.8.3: prompt + user query go as ONE user message (better output
    from gpt-oss-20b than split system/user).
    v10.8.4: fallback cascade — if the full payload (tools +
    reasoning) is rejected with 400, retry without reasoning_effort,
    then plain. The real Groq error body is logged at warning level so
    the cause is visible in Render logs.
    v10.8.5: tool_choice="auto" (NOT "required") — gpt-oss-20b answers
    simple queries from knowledge without calling the tool, and
    "required" 400s with tool_use_failed in that case. With "auto"
    the model browses when the prompt tells it to (ambiguous /
    actor / new titles) and skips it otherwise — also saves tokens.
    Rate-limit (TPD) responses now set a short cooldown so we don't
    hammer Groq while the daily token budget is exhausted.
    v10.8.6: multi-key rotation — GROQ_API_KEYS (comma-separated) are
    tried in order when a key is rate-limited; each key has its own
    cooldown. Keys from different Groq accounts get separate daily
    token budgets (Groq TPD is per-organization).
    v10.8.7: 401 handling — a key rejected as invalid/revoked is
    marked dead and the next key is tried immediately; a clear
    message is logged when every key is dead.
    """
    keys = _live_keys()
    if not keys:
        if _DEAD_KEYS:
            log.error("groq: all %d key(s) rejected (401 invalid) — "
                      "check GROQ_API_KEY/GROQ_API_KEYS on Render",
                      len(_DEAD_KEYS))
        elif _groq_keys():
            log.debug("groq: all %d key(s) in rate-limit cooldown, "
                      "skipping", len(_groq_keys()))
        return None
    base: dict = {
        "model": settings.AI_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    full = dict(base)
    if tools:
        full["tools"] = tools
        full["tool_choice"] = tool_choice or "auto"
    if reasoning_effort:
        full["reasoning_effort"] = reasoning_effort
    attempts: list[tuple[str, dict]] = [("full", full)]
    if reasoning_effort and tools:
        no_reason = dict(base)
        no_reason["tools"] = tools
        no_reason["tool_choice"] = tool_choice or "auto"
        attempts.append(("no-reasoning", no_reason))
    elif reasoning_effort:
        attempts.append(("no-reasoning", dict(base)))
    if tools:
        attempts.append(("plain", dict(base)))

    def _content(resp) -> str | None:
        try:
            return (resp.json()["choices"][0]["message"]["content"]
                    or "").strip()
        except Exception:  # noqa: BLE001
            return None

    def _key_tag(key: str) -> str:
        return f"key…{key[-4:]}" if len(key) > 4 else "key"

    for name, payload in attempts:
        for key in _live_keys():
            headers = {"Authorization": f"Bearer {key}"}
            tag = _key_tag(key)
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
                status = None
                try:
                    status = exc.response.status_code
                except Exception:  # noqa: BLE001
                    pass
                if status == 401 or "invalid_api_key" in body:
                    _DEAD_KEYS.add(key)
                    log.warning("groq %s rejected (401 invalid key) — "
                                "marked dead, trying next key; check the "
                                "key on Render", tag)
                    continue  # next key, same payload
                if "rate_limit_exceeded" in body or "rate_limit" in body:
                    wait = _parse_retry_after(body)
                    _KEY_COOLDOWN[key] = time.time() + wait + 5
                    log.warning("groq %s rate-limited — cooling down "
                                "~%ds, trying next key", tag, int(wait))
                    continue  # next key, same payload
                log.warning("groq 400 (%s, %s): %s", name, tag,
                            body[:400])
                break  # payload problem — try the simpler payload
            except Exception as exc:  # noqa: BLE001
                log.warning("groq failed (%s): %s", tag, exc)
                return None
            content = _content(r)
            if content:
                if name != "full":
                    log.info("groq ok via fallback '%s' (%s)", name, tag)
                return content
            log.warning("groq empty content (%s, %s)", name, tag)
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
    cached = _titles_cache_get(q)
    if cached is not None:
        log.info("[s:%s] grok: cache hit for %r (%d titles)", sid or "-",
                 q[:50], len(cached))
        return cached
    if await quota_remaining(user_id) <= 0:
        log.debug("ai_extract_titles: quota exhausted for %s", user_id)
        return []
    # v10.8.3: prompt + query as ONE user message.
    content = f"{TITLE_LIST_SYSTEM}\n\nUser search:\n\"{q[:200]}\""
    messages = [{"role": "user", "content": content}]
    # v10.8.5: tool_choice="auto" — "required" 400s (tool_use_failed)
    # when gpt-oss-20b answers from knowledge without browsing.
    raw = await groq_complete(messages, max_tokens=800,
                              tools=[{"type": "browser_search"}],
                              tool_choice="auto",
                              reasoning_effort="low")
    if raw is None:
        log.info("[s:%s] grok: no response (API failed)", sid or "-")
        return []
    log.info("[s:%s] grok raw response: %r", sid or "-", raw[:800])
    titles = _parse_title_list(raw, q)
    if not titles:
        log.info("[s:%s] grok: no title identified for %r", sid or "-",
                 q[:60])
        _titles_cache_put(q, [])
        return []
    await quota_use(user_id)
    _titles_cache_put(q, titles)
    # v10.8.10: AI usage goes to the activity log (dashboard + log channel).
    try:
        from app.analytics import log_event
        asyncio.create_task(log_event("ai", user_id=user_id,
                                      detail=q[:120]))
    except Exception:
        pass
    log.info("[s:%s] grok titles %r -> %r", sid or "-", q[:60],
             [(t["title"][:40], t["type"], t["year"]) for t in titles])
    return titles
