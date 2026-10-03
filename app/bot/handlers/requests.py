"""/request — users ask for missing movies."""
from __future__ import annotations

import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from app.analytics import log_event
from app.bot import forcesub
from app.bot.handlers.common import track_user
from app.config import settings
from app.db import get_session_factory
from app.models import MovieRequest

log = logging.getLogger(__name__)


async def submit_request(client: Client, user_id: int, chat_id: int,
                         text: str, mention: str | None = None) -> int:
    """Save a movie request; notify the channel; feed /mystats.

    Shared by /request, the Request Movie button and the did-you-mean
    "No" path. Returns the request id.
    """
    text = (text or "").strip()[:500]
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        req = MovieRequest(user_id=user_id, text=text)
        s.add(req)
        await s.commit()
        rid = req.id
    # v10.1: feed /mystats — requests were never logged, so the counter
    # was stuck at 0 forever.
    asyncio.create_task(log_event("request", user_id=user_id,
                                  chat_id=chat_id, detail=text[:120]))
    target = settings.REQUEST_CHANNEL
    if target:
        try:
            who = mention or f"<code>{user_id}</code>"
            await client.send_message(
                target,
                f"🎞 <b>New request #{rid}</b>\n"
                f"From: {who}\n{ui_esc(text[:400])}",
                parse_mode=ParseMode.HTML)
            log.info("request #%d posted to %s", rid, target)
        except Exception as exc:  # noqa: BLE001
            log.warning("request #%d notify to %s failed: %s",
                        rid, target, exc)
    return rid


async def _request(client: Client, message: Message):
    user = await track_user(message)
    if user and user.is_banned:
        return
    kb = await forcesub.ensure_joined(client, message.from_user.id,
                                      chat_id=message.chat.id)
    if kb:
        from app.bot.handlers.callbacks import send_join_prompt
        await send_join_prompt(client, message, message.from_user.id, kb,
                               chat_id=message.chat.id)
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        # interactive: ask for the name
        await message.reply_text(
            "🎞 <b>What movie should I add?</b>\n"
            "Reply with <code>/request Movie Name 2024</code>",
            parse_mode=ParseMode.HTML)
        return
    rid = await submit_request(client, message.from_user.id,
                               message.chat.id, text,
                               mention=message.from_user.mention)
    await message.reply_text(
        f"✅ <b>Request submitted!</b>\n\n"
        f"🎬 <b>{text[:80]}</b>\n"
        f"<i>We'll try to add it soon.</i>",
        parse_mode=ParseMode.HTML)


def ui_esc(s: str) -> str:
    from app.bot.ui import esc
    return esc(s)


async def _request_cb(client: Client, query):
    """🎞 Request Movie button: ``req:{token}`` — save the stashed
    original search as a movie request (flow diagram terminal)."""
    from app import state as state_mod
    from app.bot.handlers.common import is_banned
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    try:
        _, token = query.data.split(":", 1)
    except (ValueError, AttributeError):
        token = ""
    data = state_mod.req_tokens.pop(token, None) if token else None
    if not data or data.get("uid") != uid:
        await query.answer("⌛ Expired — search again.", show_alert=True)
        return
    q = data.get("q") or ""
    await query.answer("🎞 Submitting request…")
    log.info("[s:%s] request-movie button -> submitting %r", data.get("sid"),
             q[:60])
    try:
        rid = await submit_request(client, uid, query.message.chat.id, q)
        log.info("[s:%s] request-movie button -> saved as request #%d",
                 data.get("sid"), rid)
    except Exception:  # noqa: BLE001
        log.exception("request submit failed")
        await query.message.reply_text("❌ Could not save your request — "
                                       "try again later.")
        return
    try:
        await query.message.edit_text(
            f"✅ <b>Request submitted!</b>\n\n"
            f"🎬 <b>{ui_esc(q[:80])}</b>\n"
            f"<i>We'll try to add it soon.</i>",
            parse_mode=ParseMode.HTML)
    except Exception:  # noqa: BLE001
        pass


async def _request_group(client: Client, message: Message):
    """/request in a group: requests live in the bot's PM."""
    await message.reply_text("🎞 Please use /request in my PM 📩")


def register(bot: Client) -> None:
    bot.on_message(filters.private & filters.command("request"))(_request)
    bot.on_callback_query(filters.regex(r"^req:"))(_request_cb)
    bot.on_message(filters.group & filters.command("request"))(_request_group)
