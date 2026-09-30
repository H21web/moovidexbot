"""Force-subscribe: users must join required channels before using the bot."""
from __future__ import annotations

import logging

from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings

log = logging.getLogger(__name__)

_LEFT = (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)


async def missing_channels(client, user_id: int) -> list[str]:
    """Return the required channels the user has NOT joined."""
    missing = []
    for ref in settings.force_sub_channels:
        try:
            member = await client.get_chat_member(ref, user_id)
            if member.status in _LEFT:
                missing.append(ref)
        except UserNotParticipant:
            missing.append(ref)
        except Exception as exc:
            log.debug("forcesub check failed for %s: %s", ref, exc)
    return missing


def join_kb(channels: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        url = ch if ch.startswith("http") else f"https://t.me/{ch.lstrip('@')}"
        rows.append([InlineKeyboardButton(f"📢 Join {ch}", url=url)])
    rows.append([InlineKeyboardButton("✅ I've joined — try again",
                                     callback_data="fsub_retry")])
    return InlineKeyboardMarkup(rows)


async def ensure_joined(client, user_id: int) -> InlineKeyboardMarkup | None:
    """None if the user joined everything, else a join keyboard."""
    missing = await missing_channels(client, user_id)
    return join_kb(missing) if missing else None
