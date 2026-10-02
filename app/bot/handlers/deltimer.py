"""v10.2 /deltimer — per-user file auto-delete timer.

Delivered files are auto-deleted after a timer so they don't pile up in
the user's chat. The default comes from the group/global setting; this
command lets each user pick their own timer (or turn it off), stored in
the ``bot_settings`` kv table as ``USERDEL_<uid>`` (no migration needed).
"""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.handlers.common import track_user
from app.config import settings
from app.db import get_session_factory
from app.models import BotSetting

log = logging.getLogger(__name__)

KEY_PREFIX = "USERDEL_"
OFF = 0

CHOICES = [
    ("⏱ 10 minutes", 600),
    ("⏱ 1 hour", 3600),
    ("⏱ 6 hours", 21600),
    ("⏱ 24 hours", 86400),
    ("🚫 Off", OFF),
]


def _key(uid: int) -> str:
    return f"{KEY_PREFIX}{uid}"


async def get_user_del_timer(uid: int) -> int | None:
    """The user's chosen delete timer (seconds), or None = use default."""
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as s:
            row = (await s.execute(
                select(BotSetting).where(BotSetting.key == _key(uid))
            )).scalar_one_or_none()
            if row is not None and row.value is not None:
                return int((row.value or {}).get("v", OFF))
    except Exception as exc:  # noqa: BLE001
        log.debug("get_user_del_timer failed: %s", exc)
    return None


async def set_user_del_timer(uid: int, seconds: int) -> None:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        row = (await s.execute(
            select(BotSetting).where(BotSetting.key == _key(uid))
        )).scalar_one_or_none()
        if row is None:
            s.add(BotSetting(key=_key(uid), value={"v": int(seconds)}))
        else:
            row.value = {"v": int(seconds)}
        await s.commit()


def _fmt(seconds: int | None) -> str:
    if seconds is None:
        return "default"
    if seconds <= 0:
        return "off 🚫"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"delt:{secs}")]
             for label, secs in CHOICES]
    return InlineKeyboardMarkup(rows)


async def _deltimer_cmd(client: Client, message: Message) -> None:
    user = await track_user(message)
    if user and user.is_banned:
        return
    uid = message.from_user.id
    try:
        current = await get_user_del_timer(uid)
    except Exception:  # noqa: BLE001
        current = None
    await message.reply_text(
        "🗑 <b>File auto-delete timer</b>\n\n"
        f"Current: <b>{_fmt(current)}</b>\n"
        "Files I send you will be deleted after this long.\n"
        "“Default” = the group/global setting.",
        reply_markup=_kb(), parse_mode=ParseMode.HTML)


async def _deltimer_cb(client: Client, query) -> None:
    uid = query.from_user.id
    try:
        secs = int((query.data or "").split(":")[1])
    except (ValueError, IndexError):
        await query.answer("Invalid choice.")
        return
    try:
        await set_user_del_timer(uid, secs)
    except Exception as exc:  # noqa: BLE001
        log.warning("set_user_del_timer failed: %s", exc)
        await query.answer("⚠️ Couldn't save — try again.", show_alert=True)
        return
    await query.answer(f"Timer set: {_fmt(secs)}")
    try:
        await query.message.edit_text(
            "🗑 <b>File auto-delete timer</b>\n\n"
            f"Current: <b>{_fmt(secs)}</b>\n"
            "Files I send you will be deleted after this long.",
            reply_markup=_kb(), parse_mode=ParseMode.HTML)
    except Exception:
        log.debug("deltimer edit failed", exc_info=True)


def register(bot: Client) -> None:
    bot.on_message(filters.private & filters.command("deltimer"))(
        _deltimer_cmd)
    bot.on_callback_query(filters.regex(r"^delt:\d+$"))(_deltimer_cb)
