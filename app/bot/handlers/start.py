"""/start, /help, trending, and static callbacks."""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.types import Message
from sqlalchemy import func, select

from app import state
from app.bot import forcesub, ui
from app.bot.handlers.common import is_banned, track_user
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
    "• Use <b>@bot</b> in any chat for inline search\n"
    "• Tap a quality button to get the file instantly\n\n"
    "📥 Files stream in the web player or download directly."
)

HELP_TEXT = (
    "❓ <b>Help</b>\n\n"
    "🔍 <b>Search</b> — just type the movie name.\n"
    "   Filters: <code>1080p</code> <code>720p</code> <code>4k</code> "
    "<code>hindi</code> <code>malayalam</code> <code>tamil</code> "
    "<code>2024</code> <code>s01 e02</code>\n\n"
    "🎞 <b>Request</b> — <code>/request Movie Name 2024</code>\n"
    "📊 <b>Trending</b> — /trending\n\n"
    "⚙️ <b>Admin</b>: /index /stats /broadcast /ban /unban /requests"
)


async def _start(client: Client, message: Message):
    user = await track_user(message)
    if user and user.is_banned:
        await message.reply_text("⛔ You are banned from using this bot.")
        return
    kb = await forcesub.ensure_joined(client, message.from_user.id)
    if kb:
        await message.reply_text(
            "📢 <b>Please join our channels first</b>, then tap Try Again.",
            reply_markup=kb)
        return
    await message.reply_text(START_TEXT, reply_markup=ui.start_kb())


async def _help(client: Client, message: Message):
    await track_user(message)
    await message.reply_text(HELP_TEXT)


async def _trending(client: Client, message: Message):
    await track_user(message)
    rows = await get_trending(days=7, limit=10)
    if not rows:
        await message.reply_text("📊 No trending searches yet — be the first!")
        return
    lines = ["📊 <b>Trending this week</b>\n"]
    for i, (q, n) in enumerate(rows, 1):
        lines.append(f"{i}. {ui.esc(q)} <i>({n})</i>")
    await message.reply_text("\n".join(lines))


async def _help_cb(client: Client, query):
    await query.answer()
    await query.message.edit_text(HELP_TEXT, reply_markup=ui.start_kb())


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
                                  reply_markup=ui.start_kb())


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
