"""Keyboards + message formatting."""
from __future__ import annotations

import html

from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from app.config import settings

# ---------------------------------------------------------------- formatting

def fmt_size(num: int | None) -> str:
    if not num:
        return "—"
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(sec: int | None) -> str:
    if not sec:
        return ""
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def movie_card(group: dict, poster_url: str | None = None) -> str:
    """HTML caption for a movie card."""
    files = group.get("files", [])
    langs = sorted({f.get("language") for f in files if f.get("language")})
    quals = sorted({f.get("quality") for f in files if f.get("quality")},
                   key=lambda q: (q or ""))
    year = group.get("year")
    title = esc(group.get("display") or "Unknown")
    lines = [f"🎬 <b>{title}</b>" + (f" ({year})" if year else "")]
    if langs:
        lines.append(f"🗣 {' · '.join(langs)}")
    if quals:
        lines.append(f"📺 {' · '.join(quals)}")
    lines.append(f"📁 {len(files)} file(s)")
    return "\n".join(lines)


def file_caption(f: dict) -> str:
    name = esc(f.get("file_name") or "File")
    bits = [f"📄 <b>{name}</b>"]
    q = f.get("quality")
    lang = f.get("language")
    if q or lang:
        bits.append(f"🎞 {q or '—'} · 🗣 {lang or '—'}")
    bits.append(f"💾 {fmt_size(f.get('file_size'))}")
    dur = fmt_duration(f.get("duration"))
    if dur:
        bits.append(f"⏱ {dur}")
    return "\n".join(bits)

# ---------------------------------------------------------------- keyboards

def results_kb(token: str, page: int, total_pages: int,
               groups: list[dict], page_start: int = 0) -> InlineKeyboardMarkup:
    rows = []
    for i, g in enumerate(groups):
        title = (g.get("display") or "Unknown")[:32]
        year = g.get("year")
        label = f"🎬 {title}" + (f" ({year})" if year else "")
        rows.append([InlineKeyboardButton(label[:60],
                                         callback_data=f"mv:{token}:{page_start + i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev",
                                       callback_data=f"pg:{token}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{max(total_pages,1)}",
                                   callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️",
                                       callback_data=f"pg:{token}:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def movie_kb(token: str, midx: int, group: dict,
             page: int) -> InlineKeyboardMarkup:
    rows = []
    for i, f in enumerate(group.get("files", [])):
        q = f.get("quality") or "?"
        lang = f.get("language") or ""
        size = fmt_size(f.get("file_size"))
        label = f"📥 {q} {lang} · {size}".strip()[:60]
        rows.append([InlineKeyboardButton(
            label, callback_data=f"dl:{f['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Back to results",
                                     callback_data=f"bk:{token}:{page}")])
    return InlineKeyboardMarkup(rows)


def file_kb(file_db_id: int, watch_url: str | None) -> InlineKeyboardMarkup:
    rows = []
    if watch_url:
        rows.append([InlineKeyboardButton(
            "🎬 Watch / Download",
            web_app=WebAppInfo(url=watch_url))])
    return InlineKeyboardMarkup(rows) if rows else None


def spell_kb(suggestions: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🔍 {s[:50]}", callback_data=f"sp:{s[:50]}")]
            for s in suggestions[:3]]
    return InlineKeyboardMarkup(rows)


def index_stop_kb(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 Stop indexing",
                             callback_data=f"ixstop:{job_id}")
    ]])


def start_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔍 Search movies",
                             switch_inline_query_current_chat="")],
        [InlineKeyboardButton("📊 Trending", callback_data="trending"),
         InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    if settings.REQUEST_CHANNEL:
        rows.append([InlineKeyboardButton("🎞 Request a movie",
                                         callback_data="request")])
    return InlineKeyboardMarkup(rows)
