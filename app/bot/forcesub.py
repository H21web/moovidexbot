"""Force-subscribe: users must join required channels before using the bot."""
from __future__ import annotations

import logging

from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings

log = logging.getLogger(__name__)

_LEFT = (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)


def _norm_ref(ref: str) -> int | str:
    """v10.2.1: numeric channel IDs (``-100…``) MUST be ints.

    Pyrogram resolves a *string* ref as a username — so a numeric ID kept
    as a string made every membership check fail, and the join prompt
    showed forever even after the user joined.
    """
    s = ref.strip()
    if s.lstrip("-").isdigit():
        try:
            return int(s)
        except ValueError:
            pass
    return s


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
            member = await client.get_chat_member(_norm_ref(ref), user_id)
            if member.status in _LEFT:
                missing.append(ref)
        except UserNotParticipant:
            missing.append(ref)
        except Exception as exc:
            # Fail closed: an unexpected error must not be treated as
            # "joined" — the user re-checks after joining.
            log.warning("forcesub check failed for %s: %s", ref, exc)
            missing.append(ref)
    return missing


async def _invite_url(client, ref: int | str, raw: str) -> str:
    """v10.2.1: build a *working* join URL for a force-sub channel.

    The old code made ``https://t.me/-1001680629032`` for numeric IDs —
    a dead link. Now: public username → clean t.me link; private channel
    → bot-exported invite link (needs admin); last resort → t.me/c/ link.
    """
    try:
        chat = await client.get_chat(ref)
        if getattr(chat, "username", None):
            return f"https://t.me/{chat.username}"
    except Exception:
        log.debug("forcesub get_chat failed for %s", raw, exc_info=True)
    try:
        link = await client.export_chat_invite_link(ref)
        if link:
            return link
    except Exception:
        log.debug("forcesub export invite failed for %s (bot needs admin)",
                  raw, exc_info=True)
    if isinstance(ref, int):
        return f"https://t.me/c/{str(ref).removeprefix('-100')}"
    return f"https://t.me/{str(raw).lstrip('@')}"


async def join_kb(client, channels: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        url = await _invite_url(client, _norm_ref(ch), ch)
        rows.append([InlineKeyboardButton(f"📢 Join {ch}", url=url)])
    rows.append([InlineKeyboardButton("✅ I've joined — continue",
                                     callback_data="fsub_retry")])
    return InlineKeyboardMarkup(rows)


async def ensure_joined(client, user_id: int,
                      chat_id: int | None = None) -> InlineKeyboardMarkup | None:
    """None if the user joined everything, else a join keyboard."""
    missing = await missing_channels(client, user_id, chat_id)
    return await join_kb(client, missing) if missing else None
