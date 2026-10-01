"""Groq-powered AI: on-demand RAG search + entertainment chat (v6).

Hard rules:
- The AI NEVER invents file links. Every download button comes from a real
  DB row returned by ``search_files()`` — Groq only *arranges* results.
- Every Groq call has a 15s timeout and a graceful fallback message.
- Quota: per-user daily counter (``AI_DAILY_QUOTA``). Cache: a global
  question -> answer cache (30-day TTL) is checked BEFORE any Groq call.
- No Groq key configured -> every entry point degrades gracefully (None).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select

from app.config import settings
from app.db import get_session_factory
from app.models import AiCache, AiQuota, ChatMemory

log = logging.getLogger(__name__)

GROQ_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = 15.0
CACHE_TTL = timedelta(days=30)
MEMORY_KEEP = 20
MEMORY_TTL = timedelta(days=7)

_client: httpx.AsyncClient | None = None

# ------------------------------------------------------------------ setup

def is_configured() -> bool:
    return bool(settings.GROQ_API_KEY)


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url="https://api.openai.com",
            timeout=httpx.Timeout(TIMEOUT, connect=5.0),
            headers={
                "Authorization": f"Bearer {settings.GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
        )
    return _client


# ------------------------------------------------------------------ intent

_QUESTION_START = re.compile(
    r"^(who|what|when|where|why|how|which|whose|whom)\b", re.IGNORECASE)


def detect_intent(text: str) -> str:
    """'chat' for question-like messages, else 'search'. No AI call."""
    t = (text or "").strip()
    if not t:
        return "search"
    if "?" in t:
        return "chat"
    if _QUESTION_START.match(t):
        return "chat"
    return "search"


# ------------------------------------------------------------------ cache

# Short-lived server-side store for AI-button queries: callback data is
# limited to 64 bytes, so the full query text lives here keyed by token.
_ai_queries: dict[str, tuple[float, str]] = {}
_AIQ_TTL = 900


def store_query(text: str) -> str:
    import time as _time
    import uuid as _uuid
    now = _time.time()
    stale = [k for k, (ts, _) in _ai_queries.items() if now - ts > _AIQ_TTL]
    for k in stale:
        _ai_queries.pop(k, None)
    token = _uuid.uuid4().hex[:12]
    _ai_queries[token] = (now, (text or "")[:300])
    return token


def take_query(token: str) -> str | None:
    import time as _time
    item = _ai_queries.get(token)
    if not item:
        return None
    ts, text = item
    if _time.time() - ts > _AIQ_TTL:
        _ai_queries.pop(token, None)
        return None
    return text


def _qkey(kind: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return f"{kind}:" + hashlib.sha1(norm.encode()).hexdigest()


async def cache_get(kind: str, text: str) -> str | None:
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            row = await session.get(AiCache, _qkey(kind, text))
            if row and row.created_at:
                ts = row.created_at
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - ts < CACHE_TTL:
                    return row.answer
    except Exception as exc:  # noqa: BLE001
        log.debug("ai cache get failed: %s", exc)
    return None


async def cache_put(kind: str, text: str, answer: str) -> None:
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            key = _qkey(kind, text)
            row = await session.get(AiCache, key)
            if row:
                row.answer = answer
                row.created_at = datetime.now(timezone.utc)
            else:
                session.add(AiCache(qkey=key, answer=answer))
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("ai cache put failed: %s", exc)


# ------------------------------------------------------------------ quota

async def quota_remaining(user_id: int) -> int:
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            row = await session.get(AiQuota, (user_id, date.today()))
            used = row.count if row else 0
            return max(0, settings.AI_DAILY_QUOTA - used)
    except Exception as exc:  # noqa: BLE001
        log.debug("quota check failed: %s", exc)
        return 0


async def quota_use(user_id: int) -> None:
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            today = date.today()
            row = await session.get(AiQuota, (user_id, today))
            if row is None:
                session.add(AiQuota(user_id=user_id, day=today, count=1))
            else:
                row.count = (row.count or 0) + 1
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("quota use failed: %s", exc)


# ------------------------------------------------------------------ groq

async def groq_complete(system: str, user: str,
                        max_tokens: int = 512,
                        json_mode: bool = False) -> str | None:
    """One Groq chat call. Returns the text or None on any failure."""
    if not is_configured():
        return None
    payload: dict = {
        "model": settings.AI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        resp = await _get_client().post("/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as exc:  # noqa: BLE001 - AI must never break the bot
        log.warning("groq call failed: %s", exc)
        return None


def _parse_json(text: str | None) -> dict:
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


PARSE_SYSTEM = (
    "You are a movie search query parser for a Telegram movie bot. "
    "Extract structured filters from the user's message. "
    "Reply with ONLY a JSON object, no other text. Keys: "
    '"title" (movie/series/anime/documentary title string or null), '
    '"year" (integer or null), '
    '"genre" (one of Action, Adventure, Animation, Comedy, Crime, Documentary, '
    "Drama, Family, Fantasy, History, Horror, Music, Mystery, Romance, Sci-Fi, "
    'Thriller, War, Western — or null), '
    '"quality" (one of 480p, 720p, 1080p, 2160p — or null). '
    'Example: "oru nalla action movie 2023-le, 1080p" -> '
    '{"title": null, "year": 2023, "genre": "Action", "quality": "1080p"}.'
)

FORMAT_SYSTEM = (
    "You are Moovidex AI, a friendly entertainment buddy inside a Telegram "
    "movie bot. The user writes casually (Manglish-friendly is fine). "
    "You are given REAL search results from the bot's database as JSON — "
    "present them warmly and briefly with emoji. "
    "RULES: never invent movies, files, ratings or download links — only "
    "mention what is in the provided results. If results are empty, say so "
    "honestly and suggest /request. "
    "You ONLY help with entertainment (movies, series, anime, documentaries, "
    "actors, music). For anything else reply exactly: "
    "\"ithu ee bot-il cheyyan pattilla 😅 — njan movies/series/anime/"
    'documentary kaaryangalil mathrame sahayikku." '
    "Keep it short (under 80 words)."
)

CHAT_SYSTEM = (
    "You are Moovidex AI, a friendly entertainment buddy inside a Telegram "
    "movie bot. Chat naturally like a friend (Manglish-friendly is fine). "
    "You ONLY discuss entertainment: movies, series, anime, documentaries, "
    "actors, music, reviews, recommendations. "
    "For any other topic reply exactly: "
    "\"ithu ee bot-il cheyyan pattilla 😅 — njan movies/series/anime/"
    'documentary kaaryangalil mathrame sahayikku." '
    "Never invent download links or claim files exist — if the user wants a "
    "file, tell them to search the movie name in the bot. Keep replies "
    "short (under 100 words)."
)


async def parse_filters(text: str) -> dict:
    """NL -> {title?, year?, genre?, quality?} via Groq. {} on failure."""
    raw = await groq_complete(PARSE_SYSTEM, text[:500], max_tokens=256,
                              json_mode=True)
    f = _parse_json(raw)
    out: dict = {}
    if isinstance(f.get("title"), str) and f["title"].strip():
        out["title"] = f["title"].strip()[:120]
    if isinstance(f.get("year"), int) and 1900 <= f["year"] <= 2100:
        out["year"] = f["year"]
    if isinstance(f.get("genre"), str) and f["genre"].strip():
        out["genre"] = f["genre"].strip()[:32]
    if f.get("quality") in ("480p", "720p", "1080p", "2160p"):
        out["quality"] = f["quality"]
    return out


# ------------------------------------------------------------------ memory

async def remember(user_id: int, role: str, text: str) -> None:
    """Store one chat line; trim to last MEMORY_KEEP, 7-day TTL."""
    text = (text or "").strip()[:1000]
    if not text or role not in ("user", "assistant"):
        return
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            session.add(ChatMemory(user_id=user_id, role=role, text=text))
            await session.flush()
            cutoff = datetime.now(timezone.utc) - MEMORY_TTL
            await session.execute(
                delete(ChatMemory).where(
                    ChatMemory.user_id == user_id,
                    ChatMemory.created_at < cutoff))
            # Keep only the newest MEMORY_KEEP rows.
            old_ids = (
                await session.execute(
                    select(ChatMemory.id)
                    .where(ChatMemory.user_id == user_id)
                    .order_by(ChatMemory.id.desc())
                    .offset(MEMORY_KEEP))
            ).scalars().all()
            if old_ids:
                await session.execute(
                    delete(ChatMemory).where(ChatMemory.id.in_(old_ids)))
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("remember failed: %s", exc)


async def history(user_id: int, limit: int = 10) -> list[dict]:
    """Recent conversation as [{role, content}] (oldest first)."""
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as session:
            rows = (
                await session.execute(
                    select(ChatMemory)
                    .where(ChatMemory.user_id == user_id)
                    .order_by(ChatMemory.id.desc())
                    .limit(limit))
            ).scalars().all()
            return [{"role": r.role, "content": r.text}
                    for r in reversed(rows)]
    except Exception as exc:  # noqa: BLE001
        log.debug("history failed: %s", exc)
        return []


# ------------------------------------------------------------------ RAG search

async def ai_search(user_id: int, raw_query: str
                    ) -> tuple[str | None, list[dict], str]:
    """Groq RAG search.

    Returns ``(intro_text, groups, status)`` where status is one of
    "ok" | "no_quota" | "no_results" | "ai_off" | "failed".
    Groups are DB rows only — the AI never invents files.
    """
    from app import personalize
    from app.search import group_by_title, search_files
    from app.tmdb import discover

    if not is_configured():
        return None, [], "ai_off"
    cached = await cache_get("search", raw_query)
    if cached is not None:
        # Quota-free path: re-run the cheap DB search with the raw query
        # for fresh, clickable groups; reuse the cached friendly intro.
        groups = await _db_search(raw_query, {}, user_id)
        return cached, groups, "ok"
    if await quota_remaining(user_id) <= 0:
        return None, [], "no_quota"

    filt = await parse_filters(raw_query)
    groups = await _db_search(raw_query, filt, user_id)
    if not groups:
        return None, [], "no_results"

    await quota_use(user_id)
    prefs = await personalize.get_prefs(user_id)
    quality = (prefs.get("counters") or {}).get("quality") or {}
    top_q = max(quality, key=lambda k: quality[k]) if quality else None
    results_json = [
        {"title": g.get("display"), "year": g.get("year"),
         "qualities": sorted({f.get("quality") for f in g.get("files", [])
                              if f.get("quality")}),
         "files": len(g.get("files", []))}
        for g in groups[:5]
    ]
    user_line = (
        f"User asked: {raw_query[:300]}\n"
        + (f"User prefers {top_q} quality — mention it warmly.\n"
           if top_q else "")
        + f"REAL results JSON: {json.dumps(results_json, ensure_ascii=False)}"
    )
    intro = await groq_complete(FORMAT_SYSTEM, user_line, max_tokens=300)
    if not intro:
        intro = "🤖 <b>AI results</b> — ethokke kitti:"
    await cache_put("search", raw_query, intro)
    return intro, groups, "ok"


async def _db_search(raw_query: str, filt: dict, user_id: int) -> list[dict]:
    """Run the DB search from parsed filters + personalization."""
    from app import personalize
    from app.search import group_by_title, search_files
    from app.tmdb import discover

    groups: list[dict] = []
    title = filt.get("title")
    if title:
        q = title
        if filt.get("year"):
            q += f" {filt['year']}"
        if filt.get("quality"):
            q += f" {filt['quality']}"
        items, _ = await search_files(q, user_id=user_id, log_query=False)
        items = await personalize.rerank(items, user_id)
        groups = group_by_title(items)
    elif filt.get("genre"):
        # Genre/year browse: TMDB discover -> titles -> our DB.
        try:
            cands = await discover(genre=filt["genre"], year=filt.get("year"),
                                   limit=8)
        except Exception as exc:  # noqa: BLE001
            log.debug("discover failed: %s", exc)
            cands = []
        seen: set[str] = set()
        all_items: list[dict] = []
        for c in cands:
            items, _ = await search_files(c["title"], user_id=user_id,
                                          log_query=False)
            for it in items:
                if it["id"] not in seen:
                    seen.add(it["id"])
                    all_items.append(it)
            if len(all_items) >= 40:
                break
        all_items = await personalize.rerank(all_items, user_id)
        # Reuse the standard relevance order as a stable base.
        all_items.sort(key=lambda i: i.get("score", 0), reverse=True)
        groups = group_by_title(all_items)
    else:
        items, _ = await search_files(raw_query, user_id=user_id,
                                      log_query=False)
        items = await personalize.rerank(items, user_id)
        groups = group_by_title(items)
    return groups


# ------------------------------------------------------------------ chat

async def ai_chat(user_id: int, text: str) -> tuple[str | None, str]:
    """Entertainment chat with per-user memory.

    Returns ``(reply, status)``; status in "ok" | "no_quota" | "ai_off" |
    "failed" | "cached".
    """
    if not is_configured():
        return None, "ai_off"
    cached = await cache_get("chat", text)
    if cached is not None:
        await remember(user_id, "user", text)
        await remember(user_id, "assistant", cached)
        return cached, "cached"
    if await quota_remaining(user_id) <= 0:
        return None, "no_quota"

    hist = await history(user_id, limit=10)
    messages = [{"role": "system", "content": CHAT_SYSTEM}]
    messages.extend(hist)
    messages.append({"role": "user", "content": text[:1000]})

    payload = {
        "model": settings.AI_MODEL,
        "messages": messages,
        "max_tokens": 300,
        "temperature": 0.8,
    }
    reply: str | None = None
    try:
        resp = await _get_client().post("/v1/chat/completions", json=payload)
        resp.raise_for_status()
        reply = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("groq chat failed: %s", exc)
    if not reply:
        return None, "failed"
    await quota_use(user_id)
    await remember(user_id, "user", text)
    await remember(user_id, "assistant", reply)
    await cache_put("chat", text, reply)
    return reply, "ok"


# ------------------------------------------------------------------ web RAG

WEBSEARCH_URL = settings.WEBSEARCH_API_URL.rstrip("/")

WEBQA_SYSTEM = (
    "You answer using ONLY the web search results below. "
    "Cite sources like [1], [2]. If the results don't contain the answer, "
    "say so honestly \u2014 never invent. Keep it short (under 100 words). "
    "Manglish-friendly tone is fine."
)


async def ai_web_answer(user_id: int, query: str) -> tuple[str | None, str]:
    """Live web Q&A: search API results -> Groq answer.

    Returns ``(answer, status)``; status in "ok" | "no_quota" | "no_results"
    | "ai_off" | "failed". Uses one quota unit only when an answer is made.
    """
    if not is_configured():
        return None, "ai_off"
    if await quota_remaining(user_id) <= 0:
        return None, "no_quota"
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=5.0)) as c:
            r = await c.get(f"{WEBSEARCH_URL}/search",
                            params={"q": query[:300], "num": 5})
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("websearch api failed: %s", exc)
        return None, "failed"
    if not results:
        return None, "no_results"
    ctx = "\n".join(
        f"[{i + 1}] {(x.get('title') or '').strip()}"
        + (f": {(x.get('snippet') or '').strip()}" if x.get("snippet") else "")
        + f" ({x.get('url') or ''})"
        for i, x in enumerate(results[:5]))
    ans = await groq_complete(
        WEBQA_SYSTEM,
        f"Question: {query[:300]}\n\nWeb search results:\n{ctx}",
        max_tokens=300)
    if not ans:
        return None, "failed"
    await quota_use(user_id)
    return ans, "ok"


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
