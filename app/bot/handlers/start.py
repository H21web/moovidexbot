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
from app.models import File
from app.search import get_trending

log = logging.getLogger(__name__)

START_TEXT = (
    "👋 <b>Welcome to Moovidex!</b>\n\n"
    "🎬 Send me any <b>movie / series name</b> and I'll find it for you.\n\n"
    "💡 <b>Tips</b>\n"
    "• <code>avengers 1080p hindi</code> — filters work inline\n"
    "• Tap a quality button to get the file instantly\n\n"
    "📥 Files stream in the web player or download directly."
)

HELP_TEXT = (
    "❓ <b>Help</b>\n\n"
    "🔍 <b>Search</b> — just type the movie name.\n"
    "   Filters: <code>1080p</code> <code>720p</code> <code>4k</code> "
    "<code>hindi</code> <code>malayalam</code> <code>tamil</code> "
    "<code>2024</code> <code>s01 e02</code>\n\n"
    "✨ <b>Smart for you</b> — results order themselves by your taste "
    "as you download. /settings to control it.\n"
    "🎞 <b>Request</b> — <code>/request Movie Name 2024</code>\n"
    "📊 <b>Trending</b> — /trending\n\n"
    "⚙️ <b>Admin</b>: /index /stats /broadcast /ban /unban /warn /requests /groups"
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
        # Remember the file so "try again" can deliver it after joining.
        if dl_id:
            state.pending_dl[uid] = dl_id
        await message.reply_text(
            "📢 <b>Please join our channels first</b>, then tap Try Again.",
            reply_markup=kb, parse_mode=ParseMode.HTML)
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


def register(bot: Client) -> None:
    bot.on_message(filters.private & filters.command("start"))(_start)
    bot.on_message(filters.private & filters.command("help"))(_help)
    bot.on_message(filters.private & filters.command("trending"))(_trending)
    bot.on_callback_query(filters.regex(r"^help$"))(_help_cb)
    bot.on_callback_query(filters.regex(r"^trending$"))(_trending_cb)
    bot.on_callback_query(filters.regex(r"^noop$"))(
        lambda c, q: q.answer())
    bot.on_callback_query(filters.regex(r"^count$"))(_file_count)
