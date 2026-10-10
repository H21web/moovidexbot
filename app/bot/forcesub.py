"""Force-subscribe: users must join required channels before using the bot.

v10.8.10: request-to-join mode (like Tech VJ bot) — join buttons open a
join *request* link; the bot auto-approves requests so the user lands in
the channel with one tap. Falls back to plain invite links when the bot
can't create request links (not admin), and the "I've joined" retry tries
approving any pending request before re-checking membership.
"""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import runtime as rt
from app.config import settings

log = logging.getLogger(__name__)

_LEFT = (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)

# Cache of join-request invite links: creating one per check would
# spam the channel's invite list. Links stay valid until revoked.
_jr_link_cache: dict[str, str] = {}


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


async def effective_channels() -> list[str]:
    """Force-sub channels with the DB override winning over the env var."""
    try:
        raw = await rt.aget_setting("FORCE_SUB_CHANNELS")
    except Exception:
        raw = None
    if raw is None:
        raw = settings.FORCE_SUB_CHANNELS
    return [x.strip() for x in str(raw).replace(";", ",").split(",")
            if x.strip()]


async def missing_channels(client, user_id: int,
                           chat_id: int | None = None) -> list[str]:
    """Return the required channels the user has NOT joined."""
    # v10.14.1: force-sub is global-only (per-group override removed).
    refs = await effective_channels()
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


async def _invite_url(client, ref: int | str, raw: str) -> tuple[str, bool]:
    """v10.2.1: build a *working* join URL for a force-sub channel.

    Returns ``(url, is_join_request)``.

    v10.8.10: when FSUB_JOIN_REQUEST is on, prefer a join-*request* link
    (bot must be admin with invite rights). Automatic fallback chain:
    request link -> plain invite link -> t.me/c/ link.
    """
    join_request_mode = False
    try:
        join_request_mode = bool(await rt.aget_setting("FSUB_JOIN_REQUEST"))
    except Exception:
        pass
    if join_request_mode:
        cached = _jr_link_cache.get(raw)
        if cached:
            return cached, True
        try:
            link = await client.create_chat_invite_link(
                ref, creates_join_request=True)
            url = getattr(link, "invite_link", None) or str(link)
            if url:
                _jr_link_cache[raw] = url
                return url, True
        except Exception:
            log.debug("forcesub join-request link failed for %s "
                      "(bot needs admin + invite rights)", raw,
                      exc_info=True)
    try:
        chat = await client.get_chat(ref)
        if getattr(chat, "username", None):
            return f"https://t.me/{chat.username}", False
    except Exception:
        log.debug("forcesub get_chat failed for %s", raw, exc_info=True)
    try:
        link = await client.export_chat_invite_link(ref)
        if link:
            return link, False
    except Exception:
        log.debug("forcesub export invite failed for %s (bot needs admin)",
                  raw, exc_info=True)
    if isinstance(ref, int):
        return f"https://t.me/c/{str(ref).removeprefix('-100')}", False
    return f"https://t.me/{str(raw).lstrip('@')}", False


async def join_kb(client, channels: list[str],
                  extra_url: str | None = None) -> InlineKeyboardMarkup:
    """v10.9.0: no channel ids in button text — just "Join Channel".
    No "I've joined" button either: the bot auto-detects the join
    (chat_member update + a poll watcher) and continues by itself.

    v10.14: ``extra_url`` (a group's custom join channel) is appended
    as its own button under the force-sub buttons.
    """
    rows = []
    for ch in channels:
        url, is_jr = await _invite_url(client, _norm_ref(ch), ch)
        label = ("📩 Request to Join Channel" if is_jr
                 else "📢 Join Channel")
        rows.append([InlineKeyboardButton(label, url=url)])
    if extra_url:
        url = (extra_url if extra_url.lower().startswith("http")
               else f"https://t.me/{extra_url.lstrip('@')}")
        rows.append([InlineKeyboardButton("🔗 Join Group Channel", url=url)])
    return InlineKeyboardMarkup(rows)


def join_prompt_text() -> str:
    """v10.9.0: cleaner join prompt."""
    return (
        "👋 <b>One quick step!</b>\n\n"
        "Join our channel below to use the bot — "
        "it takes 2 seconds.\n\n"
        "✅ <i>I'll detect it automatically and continue.</i>"
    )


async def ensure_joined(client, user_id: int,
                      chat_id: int | None = None) -> InlineKeyboardMarkup | None:
    """None if the user joined everything, else a join keyboard."""
    missing = await missing_channels(client, user_id, chat_id)
    if not missing:
        return None
    # v10.14.1: no per-group join channel anymore — global force-sub only.
    return await join_kb(client, missing, None)


async def approve_pending(client, user_id: int,
                          channels: list[str]) -> int:
    """Try approving the user's pending join requests (retry fallback).

    Returns how many were approved. A user tapping "I've joined" while
    their request is still pending gets approved on the spot instead of
    an error — no approve = error is swallowed per channel.
    """
    approved = 0
    for ch in channels:
        try:
            await client.approve_chat_join_request(_norm_ref(ch), user_id)
            approved += 1
            log.info("forcesub: approved pending join request of %d for %s",
                     user_id, ch)
        except Exception:
            pass
    return approved


async def _on_join_request(client: Client, request) -> None:
    """Auto-approve channel join requests (Tech VJ style)."""
    try:
        auto = bool(await rt.aget_setting("FSUB_AUTO_APPROVE"))
    except Exception:
        auto = True
    if not auto:
        return
    try:
        await client.approve_chat_join_request(request.chat.id,
                                              request.from_user.id)
        log.info("forcesub: auto-approved join request of %d for chat %d",
                 request.from_user.id, request.chat.id)
    except Exception as exc:  # noqa: BLE001
        log.debug("forcesub auto-approve failed: %s", exc)


def register(bot: Client) -> None:
    bot.on_chat_join_request()(  # type: ignore[arg-type]
        _on_join_request)
