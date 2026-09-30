"""/request — users ask for missing movies."""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from app.bot import forcesub
from app.bot.handlers.common import track_user
from app.config import settings
from app.db import get_session_factory
from app.models import MovieRequest

log = logging.getLogger(__name__)


async def _request(client: Client, message: Message):
    user = await track_user(message)
    if user and user.is_banned:
        return
    kb = await forcesub.ensure_joined(client, message.from_user.id)
    if kb:
        await message.reply_text("📢 <b>Join our channels first</b>",
                                 reply_markup=kb)
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        # interactive: ask for the name
        await message.reply_text(
            "🎞 <b>What movie should I add?</b>\n"
            "Reply with <code>/request Movie Name 2024</code>")
        return
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        req = MovieRequest(user_id=message.from_user.id, text=text[:500])
        s.add(req)
        await s.commit()
        rid = req.id
    await message.reply_text(
        f"✅ <b>Request #{rid} noted!</b>\nWe'll add it soon. 🎬")
    # notify request channel / admins
    target = settings.REQUEST_CHANNEL
    if target:
        try:
            await client.send_message(
                target,
                f"🎞 <b>New request #{rid}</b>\n"
                f"From: {message.from_user.mention} "
                f"(<code>{message.from_user.id}</code>)\n{text[:400]}")
        except Exception as exc:
            log.debug("request notify failed: %s", exc)


async def _request_cb(client: Client, query):
    await query.answer()
    await query.message.reply_text(
        "🎞 Send <code>/request Movie Name 2024</code> to ask for a movie.")


def register(bot: Client) -> None:
    bot.on_message(filters.private & filters.command("request"))(_request)
    bot.on_callback_query(filters.regex(r"^request$"))(_request_cb)
