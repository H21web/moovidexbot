"""/start, /help, trending, and static callbacks."""
from __future__ import annotations

import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message
from sqlalchemy import func, select

from app import state
from app.analytics import log_event
from app.bot import forcesub, ui
from app.bot.handlers.common import is_banned, track_user
from app import runtime as rt
from app.config import settings
from app.db import get_session_factory
from app.models import File, Group, User
from app.search import get_trending

log = logging.getLogger(__name__)

START_TEXT = (
    "🎬 <b>Moovidex</b>\n\n"
    "Your personal movie finder.\n"
    "Just send me any <b>movie or series name</b> 👇"
)

HELP_TEXT = (
    "❓ <b>How to use Moovidex</b>\n\n"
    "🔍 <b>Search</b>\n"
    "Just type a movie or series name, like <code>avengers</code>.\n"
    "Add filters if you want: <code>dune 1080p hindi 2024</code>\n\n"
    "⭐ <b>Best pick</b>\n"
    "The top result is picked for you — tap a quality button "
    "to get the file.\n\n"
    "📊 <b>Trending</b>\n"
    "See what everyone is searching this week.\n\n"
    "🎞 <b>Request</b>\n"
    "<code>/request Movie Name 2024</code> — we'll try to add it.\n\n"
    "👤 <b>My Account</b>\n"
    "Your stats, saved files and taste preferences.\n\n"
    "💡 <b>Tip:</b> the more you download, the smarter your "
    "results get."
)


async def _deliver_deeplink(client: Client, message: Message,
                          file_db_id: int):
    """Deliver one file in PM for a /start dl_<id> deep link."""
    from app.bot.handlers.callbacks import _get_file, _send_file

    f = await _get_file(file_db_id)
    if not f:
        await message.reply_text("❌ File not found (removed?).")
        return
    try:
        await _send_file(client, message.chat.id, f,
                         message.from_user.id)
    except Exception as exc:
        log.warning("deep-link deliver failed for file %d: %s",
                    file_db_id, exc)
        await message.reply_text(
            "❌ Couldn't send the file. Try again later.")


def _parse_dl_arg(text: str | None) -> int | None:
    parts = (text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("dl_"):
        try:
            return int(parts[1][3:])
        except ValueError:
            return None
    return None


async def _start(client: Client, message: Message):
    user = await track_user(message)
    if user and user.is_banned:
        await message.reply_text("⛔ You are banned from using this bot.")
        return
    asyncio.create_task(log_event("start", user_id=message.from_user.id,
                                  chat_id=message.chat.id))
    uid = message.from_user.id
    dl_id = _parse_dl_arg(message.text)
    kb = await forcesub.ensure_joined(client, uid,
                                      chat_id=message.chat.id)
    if kb:
        # Remember the file so it auto-delivers after joining.
        if dl_id:
            state.pending_dl[uid] = dl_id
        from app.bot.handlers.callbacks import send_join_prompt
        await send_join_prompt(client, message, uid, kb,
                               chat_id=message.chat.id)
        return
    if dl_id:
        state.pending_dl.pop(uid, None)
        await _deliver_deeplink(client, message, dl_id)
        return
    text = await rt.aget_setting("WELCOME_PM") or START_TEXT
    await message.reply_text(text, reply_markup=ui.start_kb(),
                             parse_mode=ParseMode.HTML)


async def _help(client: Client, message: Message):
    await track_user(message)
    await message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def _trending(client: Client, message: Message):
    await track_user(message)
    rows = await get_trending(days=7, limit=10)
    if not rows:
        await message.reply_text("📊 No trending searches yet — be the first!")
        return
    lines = ["📊 <b>Trending this week</b>\n"]
    for i, (q, n) in enumerate(rows, 1):
        lines.append(f"{i}. {ui.esc(q)} <i>({n})</i>")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _help_cb(client: Client, query):
    await query.answer()
    await query.message.edit_text(HELP_TEXT, reply_markup=ui.start_kb(),
                                  parse_mode=ParseMode.HTML)


async def _trending_cb(client: Client, query):
    await query.answer()
    rows = await get_trending(days=7, limit=10)
    if not rows:
        await query.message.edit_text("📊 No trending searches yet.")
        return
    lines = ["📊 <b>Trending this week</b>\n"]
    for i, (q, n) in enumerate(rows, 1):
        lines.append(f"{i}. {ui.esc(q)} <i>({n})</i>")
    await query.message.edit_text("\n".join(lines),
                                  reply_markup=ui.start_kb(),
                                  parse_mode=ParseMode.HTML)


async def _file_count(client: Client, query):
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        n = (await session.execute(select(func.count(File.id)))).scalar() or 0
    await query.answer(f"📦 {n:,} files indexed", show_alert=False)


# ---------------------------------------------------------------------------
# v10.9.0: My Account
# ---------------------------------------------------------------------------

async def _get_user_row(uid: int):
    """Fetch the User row directly (never via the bot's own message)."""
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        return (await s.execute(
            select(User).where(User.id == uid))).scalar_one_or_none()


async def _account_text(uid: int, user) -> str:
    """Build the My Account panel text — the USER's details."""
    from app import ai as ai_mod
    from app.models import EventLog, SearchLog

    name = ui.esc((user.first_name if user else "") or "—")
    username = f"@{ui.esc(user.username)}" if user and user.username else "—"
    since = (user.joined_at.strftime("%d %b %Y")
             if user and user.joined_at else "—")
    ai_left = await ai_mod.quota_remaining(uid)
    ai_total = settings.AI_DAILY_QUOTA
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        n_search = (await s.execute(
            select(func.count(SearchLog.id)).where(
                SearchLog.user_id == uid))).scalar() or 0
        n_dl = (await s.execute(
            select(func.count(EventLog.id)).where(
                EventLog.kind == "download",
                EventLog.user_id == uid))).scalar() or 0
        # v10.10.1: groups this user connected (group admin).
        my_groups = (await s.execute(select(Group))).scalars().all()
        my_groups = [g for g in my_groups
                     if (g.settings or {}).get("connected_by") == uid]
    text = (
        "👤 <b>My Account</b>\n\n"
        f"🙋 <b>{name}</b>\n"
        f"🔖 Username: {username}\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📅 Joined: {since}\n\n"
        f"🤖 AI searches: <b>{ai_left}/{ai_total}</b> left today\n"
        f"🔍 Total searches: <b>{n_search:,}</b>\n"
        f"📥 Total downloads: <b>{n_dl:,}</b>"
    )
    return text, my_groups


def _account_kb(has_groups: bool = False) -> InlineKeyboardMarkup:
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[
        InlineKeyboardButton("💾 Saved", callback_data="acc:saved"),
        InlineKeyboardButton("🎨 Preference", callback_data="acc:prefs"),
    ]]
    if has_groups:
        rows.append([InlineKeyboardButton("👪 My Groups",
                                          callback_data="acc:groups")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="acc:start")])
    return InlineKeyboardMarkup(rows)


async def _acc_cb(client: Client, query):
    uid = query.from_user.id if query.from_user else None
    if not uid:
        return
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "saved":
        await query.answer()
        from app.bot.handlers.saved import _render_saved
        await _render_saved(query.message, uid, page=0, edit=True,
                            back_cb="acc")
        return
    if action == "prefs":
        await query.answer()
        text, kb = await _prefs_panel(uid)
        try:
            await query.message.edit_text(text, reply_markup=kb,
                                          parse_mode=ParseMode.HTML,
                                          disable_web_page_preview=True)
        except Exception:
            pass
        return
    if action == "lang":
        await query.answer()
        kb = _pref_choice_kb("language")
        try:
            await query.message.edit_text(
                "🌐 <b>Preferred language</b>\n\nPick one, or Auto to "
                "keep learning from your downloads.",
                reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return
    if action == "qual":
        await query.answer()
        kb = _pref_choice_kb("quality")
        try:
            await query.message.edit_text(
                "🎞 <b>Preferred quality</b>\n\nPick one, or Auto to "
                "keep learning from your downloads.",
                reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return
    if action == "set":
        key = parts[2] if len(parts) > 2 else ""
        val = parts[3] if len(parts) > 3 else ""
        if key in ("language", "quality"):
            from app import personalize
            await personalize.set_manual_pref(
                uid, key, None if val == "auto" else val)
            await query.answer(f"✅ {key.title()}: "
                               f"{val if val != 'auto' else 'Auto'}")
        text, kb = await _prefs_panel(uid)
        try:
            await query.message.edit_text(text, reply_markup=kb,
                                          parse_mode=ParseMode.HTML,
                                          disable_web_page_preview=True)
        except Exception:
            pass
        return
    if action == "groups":
        # v10.10.1: groups this user connected -> manage from here.
        await query.answer()
        from app.bot.handlers.groups import _get_group  # noqa
        user = await _get_user_row(uid)
        _, my_groups = await _account_text(uid, user)
        if not my_groups:
            await query.answer("No groups connected yet.", show_alert=True)
            return
        from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                f"👪 {(g.title or g.id)}"[:40],
                callback_data=f"grp:{g.id}")]
             for g in my_groups[:20]]
            + [[InlineKeyboardButton("⬅️ Back", callback_data="acc")]])
        try:
            await query.message.edit_text(
                "👪 <b>My Groups</b> — tap to manage:",
                reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return
    if action == "start":
        # v10.10.1: back to the /start home.
        await query.answer()
        try:
            await query.message.edit_text(
                START_TEXT, reply_markup=ui.start_kb(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True)
        except Exception:
            pass
        return
    # "acc" / "acc:home" -> account home (the USER's details).
    await query.answer()
    user = await _get_user_row(uid)
    text, my_groups = await _account_text(uid, user)
    try:
        await query.message.edit_text(
            text, reply_markup=_account_kb(bool(my_groups)),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass


def _top3(counters: dict, cat: str) -> list[tuple[str, int]]:
    d = (counters or {}).get(cat) or {}
    return sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:3]


async def _prefs_panel(uid: int):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from app import personalize

    prefs = await personalize.get_prefs(uid)
    counters = prefs.get("counters") or {}
    manual = prefs.get("manual") or {}
    m_lang = manual.get("language")
    m_qual = manual.get("quality")

    def fmt(cat: str, mval):
        if mval:
            return f"{ui.esc(mval)} <i>(you set)</i>"
        top = _top3(counters, cat)
        if not top:
            return "<i>learning…</i>"
        return ", ".join(f"{ui.esc(k)} ({v})" for k, v in top)

    text = ("🎨 <b>Your Taste</b>\n\n"
            f"🌐 Language: {fmt('language', m_lang)}\n"
            f"🎞 Quality: {fmt('quality', m_qual)}\n\n"
            "<i>Results rank themselves by your taste as you download. "
            "Set a preference to lock it in.</i>")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Language", callback_data="acc:lang"),
         InlineKeyboardButton("🎞 Quality", callback_data="acc:qual")],
        [InlineKeyboardButton("⬅️ Back", callback_data="acc")],
    ])
    return text, kb


def _pref_choice_kb(kind: str):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    if kind == "language":
        opts = ["auto", "English", "Hindi", "Malayalam", "Tamil",
                "Telugu", "Kannada"]
    else:
        opts = ["auto", "480p", "720p", "1080p", "2160p"]
    rows = []
    row = []
    for o in opts:
        label = "🔄 Auto" if o == "auto" else o
        row.append(InlineKeyboardButton(
            label, callback_data=f"acc:set:{kind}:{o}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back",
                                      callback_data="acc:prefs")])
    return InlineKeyboardMarkup(rows)


def register(bot: Client) -> None:
    bot.on_message(filters.private & filters.command("start"))(_start)
    bot.on_message(filters.private & filters.command("help"))(_help)
    bot.on_message(filters.private & filters.command("trending"))(_trending)
    bot.on_callback_query(filters.regex(r"^help$"))(_help_cb)
    bot.on_callback_query(filters.regex(r"^trending$"))(_trending_cb)
    bot.on_callback_query(filters.regex(r"^noop$"))(
        lambda c, q: q.answer())
    bot.on_callback_query(filters.regex(r"^count$"))(_file_count)
    bot.on_callback_query(filters.regex(r"^acc($|:)"))(_acc_cb)
