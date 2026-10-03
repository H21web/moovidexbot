"""v10: watchlist (⭐ Save) + /saved + /mystats."""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.bot import ui
from app.bot.handlers.common import is_banned, track_user
from app.config import settings
from app.db import get_session_factory
from app.models import EventLog, File, SavedFile

log = logging.getLogger(__name__)

_PAGE_SIZE = 8


def _file_line(f: File) -> str:
    bits = [x for x in (f.quality, f.language) if x]
    size = ui.fmt_size(f.file_size) if f.file_size else ""
    tail = " • ".join([x for x in (" • ".join(bits), size) if x])
    name = ui.esc((f.file_name or "file")[:60])
    return f"🎬 {name}" + (f"\n   <i>{ui.esc(tail)}</i>" if tail else "")


async def _save_cb(client: Client, query) -> None:
    """save:{file_db_id} — add to the user's watchlist."""
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    try:
        fid = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        return
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as s:
            f = (await s.execute(
                select(File).where(File.id == fid))).scalar_one_or_none()
            if not f:
                await query.answer("❌ File not found.", show_alert=True)
                return
            await s.execute(
                pg_insert(SavedFile).values(user_id=uid, file_id=fid)
                .on_conflict_do_nothing(
                    constraint="uq_saved_user_file"))
            await s.commit()
    except Exception:  # noqa: BLE001
        log.warning("watchlist save failed for user %d", uid, exc_info=True)
        try:
            await query.answer("⚠️ Couldn't save — try again.")
        except Exception:  # noqa: BLE001
            pass
        return
    await query.answer("⭐ Saved to your list — /saved to view")


async def _unsave_cb(client: Client, query) -> None:
    """unsave:{file_db_id} — remove from watchlist, re-render the list."""
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    try:
        fid = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        return
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            await s.execute(
                delete(SavedFile).where(SavedFile.user_id == uid,
                                        SavedFile.file_id == fid))
            await s.commit()
    except Exception:  # noqa: BLE001
        log.warning("watchlist unsave failed for user %d", uid,
                    exc_info=True)
        try:
            await query.answer("⚠️ Couldn't remove — try again.")
        except Exception:  # noqa: BLE001
            pass
        return
    await query.answer("🗑 Removed")
    # Re-render the list in place.
    await _render_saved(query.message, uid, page=0, edit=True)


async def _saved_rows(uid: int):
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        rows = (await s.execute(
            select(SavedFile, File)
            .join(File, File.id == SavedFile.file_id)
            .where(SavedFile.user_id == uid)
            .order_by(SavedFile.created_at.desc())
            .limit(200))).all()
        return [(sv, f) for sv, f in rows]


async def _render_saved(message, uid: int, page: int = 0,
                        edit: bool = False,
                        back_cb: str | None = None) -> None:
    try:
        rows = await _saved_rows(uid)
    except Exception:  # noqa: BLE001
        # v10.1: never die silently — a DB hiccup (or a missing saved_files
        # table) used to make /saved do nothing at all.
        log.warning("watchlist load failed for user %d", uid, exc_info=True)
        try:
            await message.reply_text(
                "⚠️ <b>Couldn't load your watchlist.</b>\n"
                "Please try again in a bit.",
                parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001
            pass
        return
    if not rows:
        text = ("⭐ <b>Your watchlist is empty.</b>\n"
                "Tap ⭐ Save on any file to keep it here.")
        kb = (InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back", callback_data=back_cb)]])
            if back_cb else None)
    else:
        pages = max(1, (len(rows) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        chunk = rows[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]
        lines = []
        kb_rows = []
        for sv, f in chunk:
            lines.append(_file_line(f))
            kb_rows.append([
                InlineKeyboardButton("📥 Get",
                                     callback_data=f"dl:{f.id}"),
                InlineKeyboardButton("🗑", callback_data=f"unsave:{f.id}"),
            ])
        if pages > 1:
            nav = []
            pg_cb = (f"savedpg:{page - 1}:{back_cb}" if back_cb
                     else f"savedpg:{page - 1}")
            if page > 0:
                nav.append(InlineKeyboardButton(
                    "⬅️", callback_data=pg_cb))
            nav.append(InlineKeyboardButton(f"{page + 1}/{pages}",
                                            callback_data="noop"))
            pg_cb2 = (f"savedpg:{page + 1}:{back_cb}" if back_cb
                      else f"savedpg:{page + 1}")
            if page < pages - 1:
                nav.append(InlineKeyboardButton(
                    "➡️", callback_data=pg_cb2))
            kb_rows.append(nav)
        text = ("⭐ <b>Your watchlist</b> "
                f"({len(rows)} saved)\n\n" + "\n\n".join(lines))
        if back_cb:
            kb_rows.append([InlineKeyboardButton("⬅️ Back",
                                                 callback_data=back_cb)])
        kb = InlineKeyboardMarkup(kb_rows) if kb_rows else None
    try:
        if edit:
            await message.edit_text(text, reply_markup=kb,
                                    parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)
        else:
            await message.reply_text(text, reply_markup=kb,
                                     parse_mode=ParseMode.HTML,
                                     disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        log.debug("render_saved failed", exc_info=True)


async def _saved_cmd(client: Client, message) -> None:
    """v10.2: reply FIRST, then fill in — this command can never again
    look like "no response", even if the database is slow."""
    user = await track_user(message)
    if user and user.is_banned:
        return
    wait = await message.reply_text("⭐ <i>Loading your list…</i>",
                                    parse_mode=ParseMode.HTML)
    try:
        await _render_saved(wait, message.from_user.id, edit=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("saved command failed: %s", exc)
        try:
            await wait.edit_text("⚠️ Couldn't load your watchlist — "
                                 "try again in a bit.")
        except Exception:
            pass


async def _saved_pg(client: Client, query) -> None:
    parts = (query.data or "").split(":")
    try:
        page = int(parts[1])
    except (ValueError, IndexError):
        return
    back_cb = parts[2] if len(parts) > 2 else None
    await query.answer()
    await _render_saved(query.message, query.from_user.id, page=page,
                        edit=True, back_cb=back_cb)


async def _noop(client: Client, query) -> None:
    await query.answer()


async def _mystats(client: Client, message) -> None:
    """v10: per-user stats from event_logs.

    v10.2: reply FIRST, then fill in — never "no response".
    """
    user = await track_user(message)
    if user and user.is_banned:
        return
    uid = message.from_user.id
    wait = await message.reply_text("📊 <i>Crunching your stats…</i>",
                                    parse_mode=ParseMode.HTML)
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        try:
            async with factory() as s:
                rows = (await s.execute(
                    select(EventLog.kind, func.count())
                    .where(EventLog.user_id == uid)
                    .group_by(EventLog.kind))).all()
        except Exception:  # noqa: BLE001
            rows = []
        counts = {k: n for k, n in rows}
        text = (
            "📊 <b>Your stats</b>\n\n"
            f"🔍 Searches: <b>{counts.get('search', 0)}</b>\n"
            f"📥 Downloads: <b>{counts.get('download', 0)}</b>\n"
            f"🎞 Requests: <b>{counts.get('request', 0)}</b>"
        )
        await wait.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception as exc:  # noqa: BLE001
        log.warning("mystats failed: %s", exc)
        try:
            await wait.edit_text("⚠️ Couldn't load your stats — "
                                 "try again in a bit.")
        except Exception:
            pass


def register(bot: Client) -> None:
    bot.on_message(filters.private & filters.command("saved"))(_saved_cmd)
    bot.on_message(filters.private & filters.command("mystats"))(_mystats)
    bot.on_callback_query(filters.regex(r"^save:\d+$"))(_save_cb)
    bot.on_callback_query(filters.regex(r"^unsave:\d+$"))(_unsave_cb)
    bot.on_callback_query(filters.regex(r"^savedpg:\d+"))(_saved_pg)
    bot.on_callback_query(filters.regex(r"^noop$"))(_noop)
