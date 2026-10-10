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


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def movie_card(group: dict, poster_url: str | None = None,
               meta: dict | None = None,
               personalized: bool = False) -> str:
    """HTML caption for a movie card — the single template used everywhere.

    ``meta`` is an optional TMDB dict (title/year/rating/plot/genres);
    ``personalized`` adds the "ordered for you" note.
    """
    files = group.get("files", [])
    langs = sorted({f.get("language") for f in files if f.get("language")})
    quals = sorted({f.get("quality") for f in files if f.get("quality")},
                   key=lambda q: (q or ""))
    year = group.get("year") or (meta or {}).get("year")
    title = esc(group.get("display") or (meta or {}).get("title") or "Unknown")
    rating = (meta or {}).get("rating")
    genres = (meta or {}).get("genres") or []
    plot = (meta or {}).get("plot") or ""

    lines = [f"🎬 <b>{title}</b>" + (f" ({year})" if year else "")
             + (f"  ⭐ <b>{rating}</b>" if rating else "")]
    if genres:
        lines.append(f"🎭 <i>{esc(' · '.join(genres[:3]))}</i>")
    if langs:
        lines.append(f"🗣 {esc(' · '.join(langs))}")
    if quals:
        lines.append(f"📺 {esc(' · '.join(quals))}")
    if plot:
        short = plot[:180].rsplit(" ", 1)[0]
        lines.append(f"📝 <i>{esc(short)}…</i>")
    tail = f"📁 {len(files)} file(s)"
    if personalized:
        tail += "  ✨ <i>ordered for your taste</i>"
    lines.append(tail)
    return "\n".join(lines)


def credit_suffix() -> str:
    """v10.14: the optional channel credit line ("" when disabled).

    Sync cache read — no DB hit. Shared by file_caption and per-group
    custom captions.
    """
    from app import runtime as rt
    channel = (rt.get_setting("CREDIT_CHANNEL") or "").strip()
    if not channel:
        return ""
    if channel.startswith("@") and len(channel) > 1:
        link = (f'<a href="https://t.me/{esc(channel[1:])}">'
                f"{esc(channel)}</a>")
    elif channel.lower().startswith("http"):
        link = f'<a href="{esc(channel)}">{esc(channel)}</a>'
    else:
        link = esc(channel)
    line = (rt.get_setting("CREDIT_LINE")
            or "\n\n📢 <b>Join our channel:</b> {channel}")
    return line.replace("{channel}", link)


def file_caption(f: dict) -> str:
    name = esc(f.get("file_name") or "File")
    bits = [f"📄 <b>{name}</b>"]
    q = f.get("quality")
    lang = f.get("language")
    if q or lang:
        bits.append(f"🎞 {q or '—'} · 🗣 {lang or '—'}")
    bits.append(f"💾 {fmt_size(f.get('file_size'))}")
    return "\n".join(bits) + credit_suffix()


def render_caption_tpl(tpl: str, f) -> str:
    """v10.14: render a group's custom caption template.

    Placeholders: {name} {quality} {lang} {size}. Values are HTML
    escaped; the credit line is still appended.
    """
    text = tpl.replace("{name}", esc(f.file_name or "File"))
    text = text.replace("{quality}", esc(f.quality or "—"))
    text = text.replace("{lang}", esc(f.language or "—"))
    text = text.replace("{size}", esc(fmt_size(f.file_size)))
    return text + credit_suffix()

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
             page: int, qorder: list[str] | None = None,
             more: bool = False) -> InlineKeyboardMarkup:
    """Download buttons per file. ``qorder`` lists quality labels in the
    user's preferred order (most-loved first); ``more`` appends a
    "More results" row back to the paginated list."""
    rows = []
    files = list(group.get("files", []))
    if qorder:
        rank = {q.lower(): i for i, q in enumerate(qorder)}

        def _qk(f: dict) -> int:
            return rank.get((f.get("quality") or "").lower(), 10**6)

        files.sort(key=_qk)  # stable: learned taste first
    for i, f in enumerate(files):
        q = f.get("quality") or "?"
        lang = f.get("language") or ""
        size = fmt_size(f.get("file_size"))
        label = f"📥 {q} {lang} · {size}".strip()[:60]
        rows.append([InlineKeyboardButton(
            label, callback_data=f"dl:{f['id']}")])
    if more:
        rows.append([InlineKeyboardButton("🔍 More results",
                                         callback_data=f"bk:{token}:{page}")])
    rows.append([InlineKeyboardButton("⬅️ Back to results",
                                     callback_data=f"bk:{token}:{page}")])
    return InlineKeyboardMarkup(rows)


def ai_search_kb(query_token: str) -> InlineKeyboardMarkup:
    """On-demand AI search button (shown when normal search finds nothing)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🤖 AI Search", callback_data=f"aiq:{query_token}")
    ]])


def user_settings_kb(enabled: bool) -> InlineKeyboardMarkup:
    """Personalization toggle + reset for /settings (non-admin users)."""
    state = "ON ✅" if enabled else "OFF ❌"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✨ Personalized search: {state}",
                             callback_data="pset:toggle")],
        [InlineKeyboardButton("🗑 Reset my taste",
                             callback_data="pset:reset")],
    ])


def user_settings_text(enabled: bool, downloads: int) -> str:
    state = "<b>ON</b> ✅" if enabled else "<b>OFF</b> ❌"
    taste = (f"🧠 <b>{downloads}</b> downloads learned from"
             if downloads else "🧠 Not enough downloads yet")
    return (
        "⚙️ <b>My Settings</b>\n\n"
        f"✨ Personalized search: {state}\n"
        f"{taste}\n\n"
        "<i>Downloads teach me your taste (quality, language, size…) "
        "and future results are ordered for you.</i>"
    )


def index_stop_kb(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 Stop indexing",
                             callback_data=f"ixstop:{job_id}")
    ]])


def ix_setup_cancel_kb() -> InlineKeyboardMarkup:
    """Cancel button shown during interactive /index setup."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data="ixs:cancel"),
    ]])


def ix_setup_kb(pending: dict) -> InlineKeyboardMarkup:
    """Options panel for interactive /index setup."""
    opts = pending["opts"]

    def fmt(key: str, off: str = "off") -> str:
        v = opts.get(key) or 0
        return f"{v:,}" if v else off

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start indexing",
                              callback_data="ixs:start")],
        [InlineKeyboardButton(f"⏭ Skip: {fmt('skip')}",
                              callback_data="ixs:opt:skip"),
         InlineKeyboardButton(f"🔢 Limit: {fmt('limit')}",
                              callback_data="ixs:opt:limit")],
        [InlineKeyboardButton(f"⬇️ From msg: {fmt('from_id', '—')}",
                              callback_data="ixs:opt:from_id"),
         InlineKeyboardButton(f"⬆️ To msg: {fmt('to_id', '—')}",
                              callback_data="ixs:opt:to_id")],
        [InlineKeyboardButton("❌ Cancel", callback_data="ixs:cancel")],
    ])


def start_kb() -> InlineKeyboardMarkup:
    """v10.9.0: clean home — trending, help, my account (no inline)."""
    rows = [
        [InlineKeyboardButton("📊 Trending", callback_data="trending"),
         InlineKeyboardButton("❓ Help", callback_data="help")],
        [InlineKeyboardButton("👤 My Account", callback_data="acc")],
    ]
    if settings.REQUEST_CHANNEL:
        rows.append([InlineKeyboardButton("🎞 Request a movie",
                                         callback_data="request")])
    return InlineKeyboardMarkup(rows)
