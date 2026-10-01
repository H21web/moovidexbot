

"""v8 results UI: best pick + file list with download links + filters."""
from __future__ import annotations

from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from app.bot.ui import esc, fmt_size

# ---------------------------------------------------------------- v8 results UI
# Best pick on top (Play/Download buttons), rest as a text list with
# download LINKS (not buttons), pagination, and Language / Quality /
# Season / Episode selector buttons. Replaces the old button-style
# per-file filter list.

V8_PAGE_SIZE = 8
V8_FILTERS: tuple[tuple[str, str], ...] = (
    ("language", "🌐 Language"),
    ("quality", "🎞 Quality"),
    ("season", "📺 Season"),
    ("episode", "🔢 Episode"),
)


def file_season_episode(file_name: str | None) -> tuple[int | None, int | None]:
    """Season/episode parsed from a filename (best effort)."""
    from app.textutil import (
        EPISODE_WORD_RE,
        SEASON_BARE_RE,
        SEASON_WORD_RE,
        SE_EP_RE,
        X_EP_RE,
    )
    text = file_name or ""
    m = SE_EP_RE.search(text) or X_EP_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    season = episode = None
    m = SEASON_WORD_RE.search(text)
    if m:
        season = int(m.group(1))
    else:
        m = SEASON_BARE_RE.search(text)
        if m:
            season = int(m.group(1))
    m = EPISODE_WORD_RE.search(text)
    if m:
        episode = int(m.group(1))
    return season, episode


def v8_file_kb(file_db_id: int, user_id: int):
    """Tech VJ style file buttons: Play (web app) + Download (direct link)."""
    from app.web.tokens import dl_url, watch_url
    rows = []
    w = watch_url(file_db_id, user_id)
    d = dl_url(file_db_id, user_id)
    row = []
    if w:
        row.append(InlineKeyboardButton("▶ Play", web_app=WebAppInfo(url=w)))
    if d:
        row.append(InlineKeyboardButton("⬇ Download", url=d))
    if row:
        rows.append(row)
    if not rows:
        # No WEB_URL configured — fall back to in-Telegram delivery.
        rows.append([InlineKeyboardButton("⬇ Download",
                                         callback_data=f"dl:{file_db_id}")])
    return InlineKeyboardMarkup(rows)


def _v8_dl_link(file_db_id: int, user_id: int) -> str | None:
    from app.web.tokens import dl_url
    return dl_url(file_db_id, user_id)


def _v8_file_line(idx: int, f: dict, user_id: int) -> str:
    name = (f.get("file_name") or "file").strip()
    short = esc(name if len(name) <= 48 else name[:45] + "…")
    meta = " · ".join(x for x in (
        f.get("quality"), f.get("language"), fmt_size(f.get("file_size"))) if x)
    s, e = file_season_episode(name)
    if s or e:
        se = "".join(x for x in (
            f" S{s:02d}" if s else "", f" E{e:02d}" if e else ""))
        meta = (se.strip() + (" · " + meta if meta else "")).strip(" ·")
    line = f"{idx}. <b>{short}</b>"
    if meta:
        line += f"\n   <i>{esc(meta)}</i>"
    link = _v8_dl_link(f["id"], user_id)
    if link:
        line += f' — <a href="{link}">⬇ download</a>'
    return line


def v8_results_text(meta: dict | None, best: dict, files: list[dict],
                    page: int, pages: int, total: int,
                    filters: dict, user_id: int,
                    ai_note: str | None = None) -> str:
    parts: list[str] = []
    if meta:
        head = f"🎬 <b>{esc(meta.get('title'))}</b>"
        if meta.get("year"):
            head += f" ({meta['year']})"
        if meta.get("rating"):
            head += f" · ⭐ {meta['rating']}"
        parts.append(head)
        if meta.get("genres"):
            parts.append(f"<i>{esc(', '.join(meta['genres']))}</i>")
        if meta.get("plot"):
            plot = meta["plot"]
            parts.append(f"<i>{esc(plot[:170] + '…' if len(plot) > 170 else plot)}</i>")
        parts.append("")
    bname = esc((best.get("file_name") or "")[:70])
    bmeta = " · ".join(x for x in (
        best.get("quality"), best.get("language"),
        fmt_size(best.get("file_size"))) if x)
    parts.append(f"⭐ <b>Best pick</b>\n📁 {bname}")
    if bmeta:
        parts.append(f"<i>{esc(bmeta)}</i>")
    if ai_note:
        parts.append(f"💡 <i>{esc(ai_note)}</i>")
    parts.append("")
    flt = " · ".join(
        f"{dict(V8_FILTERS).get(k, k)}: {v}" for k, v in filters.items() if v)
    head = f"📋 <b>All files ({total})</b>"
    if flt:
        head += f"\n🔎 <i>{esc(flt)}</i>"
    parts.append(head)
    start = page * V8_PAGE_SIZE
    for i, f in enumerate(files, start=start + 1):
        parts.append(_v8_file_line(i, f, user_id))
    text = "\n".join(parts)
    # Telegram hard limit: 4096 chars.
    if len(text) > 4000:
        text = text[:3990] + "…"
    return text


def v8_results_kb(token: str, best_id: int, user_id: int,
                  page: int, pages: int, filters: dict):
    rows: list[list] = []
    # Best-pick Play / Download buttons (Tech VJ style).
    from app.web.tokens import dl_url, watch_url
    w = watch_url(best_id, user_id)
    d = dl_url(best_id, user_id)
    brow: list = []
    if w:
        brow.append(InlineKeyboardButton("▶ Play", web_app=WebAppInfo(url=w)))
    if d:
        brow.append(InlineKeyboardButton("⬇ Download", url=d))
    if brow:
        rows.append(brow)
    # Pagination.
    if pages > 1:
        prow: list = []
        if page > 0:
            prow.append(InlineKeyboardButton(
                "◀ Prev", callback_data=f"v8:{token}:{page - 1}"))
        prow.append(InlineKeyboardButton(
            f"{page + 1}/{pages}", callback_data=f"v8:{token}:{page}"))
        if page < pages - 1:
            prow.append(InlineKeyboardButton(
                "Next ▶", callback_data=f"v8:{token}:{page + 1}"))
        rows.append(prow)
    # Filter selectors — one row per two kinds, ✓ when active.
    frow: list = []
    for kind, label in V8_FILTERS:
        mark = " ✓" if filters.get(kind) else ""
        frow.append(InlineKeyboardButton(
            f"{label}{mark}", callback_data=f"rf:{token}:{kind}"))
        if len(frow) == 2:
            rows.append(frow)
            frow = []
    if frow:
        rows.append(frow)
    return InlineKeyboardMarkup(rows)


def v8_filter_options_kb(token: str, kind: str, options: list,
                         filters: dict):
    label = dict(V8_FILTERS).get(kind, kind)
    rows: list[list] = []
    row: list = []
    for i, opt in enumerate(options[:20]):
        if kind == "season":
            text = f"S{int(opt):02d}"
        elif kind == "episode":
            text = f"E{int(opt):02d}"
        else:
            text = str(opt)
        cur = filters.get(kind)
        mark = " ✓" if str(cur) == str(opt) else ""
        row.append(InlineKeyboardButton(
            f"{text}{mark}", callback_data=f"rf:{token}:{kind}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if filters.get(kind):
        rows.append([InlineKeyboardButton(
            "❌ Clear", callback_data=f"rf:{token}:{kind}:x")])
    rows.append([InlineKeyboardButton("◀ Back",
                                      callback_data=f"rf:{token}:back")])
    return InlineKeyboardMarkup(rows), label


def v8_filter_options(files: list[dict]) -> dict[str, list]:
    """Distinct filter values present in a file list."""
    langs: list[str] = []
    quals: list[str] = []
    seasons: list[int] = []
    episodes: list[int] = []
    for f in files:
        lang = (f.get("language") or "").strip()
        if lang and lang not in langs:
            langs.append(lang)
        q = (f.get("quality") or "").strip()
        if q and q not in quals:
            quals.append(q)
        s, e = file_season_episode(f.get("file_name"))
        if s and s not in seasons:
            seasons.append(s)
        if e and e not in episodes:
            episodes.append(e)
    quals.sort(key=lambda x: ({"480p": 0, "720p": 1, "1080p": 2,
                               "2160p": 3, "4320p": 4}.get(x.lower(), 9), x))
    return {
        "language": langs,
        "quality": quals,
        "season": sorted(seasons),
        "episode": sorted(episodes),
    }


def apply_v8_filters(files: list[dict], filters: dict) -> list[dict]:
    out = []
    for f in files:
        if filters.get("language"):
            if (f.get("language") or "").strip().lower() != \
                    str(filters["language"]).strip().lower():
                continue
        if filters.get("quality"):
            if (f.get("quality") or "").strip().lower() != \
                    str(filters["quality"]).strip().lower():
                continue
        s, e = (None, None)
        if filters.get("season") or filters.get("episode"):
            s, e = file_season_episode(f.get("file_name"))
        if filters.get("season") and s != int(filters["season"]):
            continue
        if filters.get("episode") and e != int(filters["episode"]):
            continue
        out.append(f)
    return out
