"""Force-subscribe: users must join required channels before using the bot."""
from __future__ import annotations

import logging

from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings

log = logging.getLogger(__name__)

_LEFT = (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)


async def _group_force_sub(chat_id: int | None) -> list[str]:
    """Extra force-sub channels configured for a connected group."""
    if chat_id is None:
        return []
    try:
        from sqlalchemy import select

        from app.db import get_session_factory
        from app.models import Group

        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            g = (await s.execute(
                select(Group).where(Group.id == chat_id)
            )).scalar_one_or_none()
            if g and g.settings:
                return list(g.settings.get("force_sub") or [])
    except Exception as exc:
        log.debug("group forcesub lookup failed: %s", exc)
    return []


async def missing_channels(client, user_id: int,
                           chat_id: int | None = None) -> list[str]:
    """Return the required channels the user has NOT joined."""
    refs = list(settings.force_sub_channels)
    for ref in await _group_force_sub(chat_id):
        if ref not in refs:
            refs.append(ref)
    missing = []
    for ref in refs:
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


async def ensure_joined(client, user_id: int,
                      chat_id: int | None = None) -> InlineKeyboardMarkup | None:
    """None if the user joined everything, else a join keyboard."""
    missing = await missing_channels(client, user_id, chat_id)
    return join_kb(missing) if missing else None
