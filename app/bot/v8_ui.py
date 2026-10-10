

"""v8 results UI: best pick + file list with download links + filters."""
from __future__ import annotations

from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from app.bot.ui import esc, fmt_size

# ---------------------------------------------------------------- v8 results UI
# Best pick on top, rest as a text list with
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


def kind_icon(meta: dict | None = None,
              file_name: str | None = None) -> str:
    """🎬 for movies, 📺 for series (TMDB kind, else filename S/E tags)."""
    kind = (meta or {}).get("kind")
    if kind == "tv":
        return "📺"
    if kind == "movie":
        return "🎬"
    s, e = file_season_episode(file_name)
    return "📺" if (s or e) else "🎬"


def file_deep_link(bot_username: str | None, file_db_id: int) -> str | None:
    """t.me deep link that makes the bot deliver this exact file."""
    if not bot_username:
        return None
    return f"https://t.me/{bot_username}?start=dl_{file_db_id}"


# Canonical quality order for "best logic" sorting.
_QUALITY_RANK = {"480p": 1, "720p": 2, "1080p": 3, "2160p": 4, "4320p": 5}


def sort_best_first(files: list[dict]) -> list[dict]:
    """Order files with the same logic that picks the best pick.

    Score first (search relevance + personal taste), then quality,
    then size — so the list runs best → worst and ``files[0]`` is
    always the best pick.
    """
    def key(f: dict):
        q = (f.get("quality") or "").lower()
        return (-(f.get("score") or 0.0),
                -_QUALITY_RANK.get(q, 0),
                -(f.get("file_size") or 0))
    return sorted(files, key=key)


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
    # v10: watchlist — save for later.
    rows.append([InlineKeyboardButton("⭐ Save",
                                     callback_data=f"save:{file_db_id}")])
    # v10.11.7: subtitles open in the Telegram web app (language picker);
    # tapping a language sends the .srt and closes the app.
    from app.web.tokens import sub_pick_url
    sp = sub_pick_url(file_db_id, user_id)
    if sp:
        rows.append([InlineKeyboardButton("📝 Subtitles",
                                          web_app=WebAppInfo(url=sp))])
    return InlineKeyboardMarkup(rows)


def sort_series_newest_first(files: list[dict]) -> list[dict]:
    """Series results: newest season/episode first.

    Stable sort by (-season, -episode) over the existing best-first order,
    so files with no S/E tags keep their relative order at the end.
    Movies (no S/E tags anywhere) are returned unchanged.
    """
    se = [file_season_episode(f.get("file_name")) for f in files]
    if not any(s or e for s, e in se):
        return files
    return [f for _, f in sorted(
        zip(se, files),
        key=lambda p: (-(p[0][0] or 0), -(p[0][1] or 0)))]


def _v8_file_line(idx: int, f: dict, user_id: int,
                  bot_username: str | None = None) -> str:
    """One file as its own blockquote — cleaned name + detailed meta."""
    from app.textutil import clean_display_name
    raw = f.get("file_name") or "file"
    name = clean_display_name(raw)
    # Tapping the file name delivers the file (deep link -> dl_ handler).
    deep = file_deep_link(bot_username, f["id"])
    if deep:
        disp = f'<b><a href="{deep}">{esc(name)}</a></b>'
    else:
        disp = f"<b>{esc(name)}</b>"
    icon = kind_icon(file_name=name)
    # Season/episode parsed from the RAW name (cleaning may truncate).
    s, e = file_season_episode(raw)
    se = ""
    if s or e:
        se = f"S{s:02d}" if s else ""
        if e:
            se += f"E{e:02d}"
    dl = f.get("downloads") or 0
    meta = " · ".join(x for x in (
        se or None,
        f.get("quality"),
        f.get("language"),
        fmt_size(f.get("file_size")),
        f"⬇ {dl:,}" if dl else None,
    ) if x)
    inner = f"{icon} {disp}"
    if meta:
        inner += f"\n<i>{esc(meta)}</i>"
    return f"<blockquote>{inner}</blockquote>"


def v8_results_text(meta: dict | None, best: dict, files: list[dict],
                    page: int, pages: int, total: int,
                    filters: dict, user_id: int,
                    ai_note: str | None = None,
                    bot_username: str | None = None) -> str:
    parts: list[str] = []
    if meta:
        icon = kind_icon(meta)
        # v10.8.7: the title links to the backdrop/poster image —
        # Telegram fetches it and shows a large preview above the text.
        title_html = f"<b>{esc(meta.get('title'))}</b>"
        img = meta.get("backdrop_url") or meta.get("poster_url")
        if img:
            title_html = f'<a href="{esc(img)}">{title_html}</a>'
        head = f"{icon} {title_html}"
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
        # v10.14.2: OTT "where to watch" from JustWatch.
        # v10.14.3: hyperlinked, max 2 providers.
        ott = meta.get("ott") or []
        if ott:
            ott_url = meta.get("ott_url") or ""
            if ott_url:
                links = ", ".join(
                    f'<a href="{esc(ott_url)}">{esc(p)}</a>'
                    for p in ott[:2])
            else:
                links = esc(", ".join(ott[:2]))
            parts.append(f"📺 <i>Available on: {links}</i>")
        parts.append("")
    from app.textutil import clean_display_name
    # v10.14: series — newest season/episode first (idempotent: _build_v8
    # already sorted the full list; this keeps single-page renders sane).
    files = sort_series_newest_first(files)
    bname = clean_display_name(best.get("file_name") or "")
    bdeep = file_deep_link(bot_username, best["id"])
    bq: list[str] = []
    if bdeep:
        bq.append(f'📁 <a href="{bdeep}"><b>{esc(bname)}</b></a>')
    else:
        bq.append(f"📁 <b>{esc(bname)}</b>")
    bmeta = " · ".join(x for x in (
        best.get("quality"), best.get("language"),
        fmt_size(best.get("file_size"))) if x)
    if bmeta:
        bq.append(f"<i>{esc(bmeta)}</i>")
    if ai_note:
        bq.append(f"💡 <i>{esc(ai_note)}</i>")
    parts.append("<blockquote>⭐ <b>Best Pick</b>\n" + "\n".join(bq) +
                 "</blockquote>")
    parts.append("")
    # v8.1: no other files besides the best pick -> skip the list section.
    if files:
        flt = " · ".join(
            f"{dict(V8_FILTERS).get(k, k)}: {v}" for k, v in filters.items() if v)
        head = f"📋 <b>All files ({total})</b>"
        if flt:
            head += f"\n🔎 <i>{esc(flt)}</i>"
        parts.append(head)
        parts.append("")  # breathing room between heading and the list
        start = page * V8_PAGE_SIZE
        for i, f in enumerate(files, start=start + 1):
            parts.append(_v8_file_line(i, f, user_id, bot_username))
            parts.append("")  # v10.10.2: breathing room between files
    text = "\n".join(parts)
    # Telegram hard limit: 4096 chars. Never cut mid-HTML-tag (Telegram
    # rejects the edit) — cut back to the last complete ">" before the
    # limit instead.
    if len(text) > 4000:
        cut = text.rfind(">", 0, 3990)
        text = (text[:cut + 1] if cut != -1 else text[:3990]) + "…"
    return text


def v8_results_kb(token: str, user_id: int,
                  page: int, pages: int, filters: dict):
    rows: list[list] = []
    # v10.4: no Play / Download buttons on the result card — files are
    # delivered in Telegram when a quality button is tapped.
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
