"""Group management from PM.

* In a group: ``/connect`` (by a group admin) links the group to the bot.
* In PM (bot admin): ``/groups`` lists linked groups; tapping one opens a
  panel to manage that group's settings — auto-delete timer, force-sub
  channels, welcome text — or disconnect it.

Group settings live in ``groups.settings`` (JSONB) and override the global
runtime defaults.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app import state
from app.bot import ui
from app.bot.handlers.common import admin_only, track_user
from app.config import settings
from app.db import get_session_factory
from app.models import Group, User
from app import runtime as rt

log = logging.getLogger(__name__)

# v10.14.2: pending group-setting replies are DB-backed
# (state.gpending_*) — restart-proof. The old in-memory _pending dict
# silently ate settings on restart / second instance; it is gone.
_ACTION_LABELS = {
    "welcome": "Welcome message",
    "startmsg": "Start message",
    "caption_tpl": "File caption",
    "start_btn": "Start button",
}
# settings key(s) cleared by "off", and text length limits.
_ACTION_KEYS = {
    "welcome": ("welcome",),
    "startmsg": ("start_message",),
    "caption_tpl": ("caption_tpl",),
    "start_btn": ("start_btn_text", "start_btn_url"),
}
_ACTION_LIMITS = {
    "welcome": 1000,
    "startmsg": 1500,
    "caption_tpl": 500,
}
_ACTION_SAVE_KEY = {
    "welcome": "welcome",
    "startmsg": "start_message",
    "caption_tpl": "caption_tpl",
}

AD_CHOICES = [("Off", 0), ("5 min", 300), ("15 min", 900),
              ("30 min", 1800), ("1 hour", 3600),
              ("🌐 Global default", -1)]


# ---------- v10.14: member tiers ----------
# BASIC <500 · T500 >=500 · T1000 >=1000 · T3000 >=3000 members.
TIER_500 = 500
TIER_1000 = 1000
TIER_3000 = 3000

_member_cache: dict[int, tuple[int, float]] = {}
_MEMBER_TTL = 3600.0  # 1 hour


async def group_member_count(client: Client, chat_id: int) -> int:
    """Member count with a 1-hour in-memory cache (tier gating)."""
    now = time.monotonic()
    hit = _member_cache.get(chat_id)
    if hit and now - hit[1] < _MEMBER_TTL:
        return hit[0]
    try:
        n = await client.get_chat_members_count(chat_id)
    except Exception:
        n = 0
    _member_cache[chat_id] = (n, now)
    return n


async def group_tier_ok(client: Client, chat_id: int, needed: int) -> bool:
    """True when the group has >= ``needed`` members."""
    if needed <= 0:
        return True
    return await group_member_count(client, chat_id) >= needed


async def is_owner_group(chat_id: int) -> bool:
    """True when this group is the bot owner's designated main group."""
    try:
        og = await rt.aget_setting("OWNER_GROUP_ID")
        return int(og or 0) == int(chat_id)
    except (TypeError, ValueError):
        return False


async def group_ai_allowed(client: Client, chat_id: int | None) -> bool:
    """Groq AI spell help is allowed in PM, the owner group,
    or groups with 3000+ members. Everyone else gets local spell only.

    v10.14.1: the per-group "AI mode" toggle (default ON) can turn it
    off even where the tier would allow it.
    """
    if chat_id is None or not str(chat_id).startswith("-"):
        return True
    try:
        g = await _get_group(int(chat_id))
        if g is not None and (g.settings or {}).get("ai_mode") is False:
            return False
    except Exception:
        pass
    if await is_owner_group(chat_id):
        return True
    return await group_tier_ok(client, chat_id, TIER_3000)


async def group_unlocked(client: Client, chat_id: int, need: int) -> bool:
    """v10.14: tier feature unlocked? Owner group bypasses all tiers."""
    if await is_owner_group(chat_id):
        return True
    return await group_tier_ok(client, chat_id, need)


async def _tier_btn(client: Client, gid: int, need: int, label: str,
                    ok_cb: str) -> InlineKeyboardButton:
    """v10.14: tier-gated panel button — shows 🔒 when locked."""
    if await group_unlocked(client, gid, need):
        return InlineKeyboardButton(label, callback_data=ok_cb)
    short = label.split(":")[0]
    return InlineKeyboardButton(f"🔒 {short}",
                                callback_data=f"grp:locked:{need}:{gid}")


_bot_username: str | None = None


async def _bot_username(client: Client) -> str | None:
    """Cached bot username for group start links."""
    global _bot_username
    if _bot_username:
        return _bot_username
    try:
        me = await client.get_me()
        _bot_username = me.username or None
    except Exception:
        pass
    return _bot_username


# ---------- helpers ----------

async def _get_group(gid: int) -> Group | None:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        return (await s.execute(
            select(Group).where(Group.id == gid))).scalar_one_or_none()


async def _save_group(gid: int, title: str | None,
                      mutate=None, connected_by: int | None = None) -> Group:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        g = (await s.execute(
            select(Group).where(Group.id == gid))).scalar_one_or_none()
        if g is None:
            g = Group(id=gid, title=title,
                      settings={"connected_by": connected_by})
            s.add(g)
        else:
            if title:
                g.title = title
            if connected_by and not g.settings.get("connected_by"):
                g.settings = {**g.settings, "connected_by": connected_by}
        if mutate:
            g.settings = mutate(dict(g.settings or {}))
        await s.commit()
        return g


async def effective_autodelete(chat_id: int) -> int:
    """Per-group auto-delete seconds, falling back to the global default."""
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            g = (await s.execute(
                select(Group).where(Group.id == chat_id))).scalar_one_or_none()
            if g is not None:
                raw = (g.settings or {}).get("autodelete_seconds")
                if raw is not None:
                    # Explicit group value (including 0 = Off) wins.
                    return int(raw)
    except Exception as exc:
        log.debug("effective_autodelete failed: %s", exc)
    return int(await rt.aget_setting("AUTO_DELETE_SECONDS") or 0)


async def _panel_kb(client: Client, g: Group) -> InlineKeyboardMarkup:
    # v10.14: absent autodelete key = inherit the global default (🌐).
    s = g.settings or {}
    gid = g.id
    raw_ad = s.get("autodelete_seconds")
    if raw_ad is None:
        ad_label = "🌐 Global"
    else:
        ad = int(raw_ad)
        ad_label = next((l for l, s_ in AD_CHOICES if s_ == ad),
                        f"{ad // 60}m")
    welcome = s.get("welcome")
    imdb = s.get("imdb_enabled", True)
    poster = s.get("poster", True)
    rows = [
        [await _tier_btn(client, gid, TIER_500,
                         f"🗑 Auto-delete: {ad_label}",
                         f"grpadmenu:{gid}")],
        [InlineKeyboardButton(
            f"👋 Welcome: {'set' if welcome else 'off'}",
            callback_data=f"grpwelcome:{gid}")],
        [InlineKeyboardButton(
            f"🎬 IMDB info: {'ON' if imdb else 'OFF'}",
            callback_data=f"grpimdb:{gid}")],
        [InlineKeyboardButton(
            f"🖼 Poster: {'ON' if poster else 'OFF'}",
            callback_data=f"grp:poster:{gid}")],
        [InlineKeyboardButton(
            f"💬 Start msg: {'set' if s.get('start_message') else 'off'}",
            callback_data=f"grpsmsg:{gid}")],
        [await _tier_btn(client, gid, TIER_500,
                         f"📝 Caption: {'set' if s.get('caption_tpl') else 'off'}",
                         f"grpcap:{gid}")],
        [await _tier_btn(client, gid, TIER_1000,
                         f"🔘 Start button: {'set' if s.get('start_btn_text') else 'off'}",
                         f"grpsbtn:{gid}")],
        [await _tier_btn(client, gid, TIER_3000,
                         f"🤖 AI mode: {'ON' if s.get('ai_mode', True) else 'OFF'}",
                         f"grp:ai:{gid}")],
        [InlineKeyboardButton("🔌 Disconnect",
                              callback_data=f"grpdel:{gid}")],
        [InlineKeyboardButton("⬅️ All groups", callback_data="grplist")],
    ]
    return InlineKeyboardMarkup(rows)


async def _panel_text(client: Client, g: Group) -> str:
    s = g.settings or {}
    raw_ad = s.get("autodelete_seconds")
    ad_txt = "🌐 global" if raw_ad is None else (
        "off" if not int(raw_ad) else f"{int(raw_ad) // 60} min")
    imdb = s.get("imdb_enabled", True)
    n = await group_member_count(client, g.id)
    tier = ("BASIC" if n < TIER_500 else "500+" if n < TIER_1000
            else "1000+" if n < TIER_3000 else "3000+")
    if await is_owner_group(g.id):
        tier += " 👑"
    un = await _bot_username(client)
    start_link = (f"\n🔗 Start link: <code>https://t.me/{un}"
                  f"?start=grp_{g.id}</code>" if un else "")
    # v10.14.1: show the EFFECTIVE AI status, not just the toggle —
    # a small group shows 🔒 instead of a misleading "ON".
    ai_toggle = bool(s.get("ai_mode", True))
    if not ai_toggle:
        ai_txt = "OFF"
    elif await is_owner_group(g.id) or await group_tier_ok(
            client, g.id, TIER_3000):
        ai_txt = "ON"
    else:
        ai_txt = "🔒 needs 3000+"
    return (
        f"👪 <b>{ui.esc(g.title or str(g.id))}</b>\n<code>{g.id}</code>\n"
        f"👥 {n:,} members · tier <b>{tier}</b>{start_link}\n\n"
        f"🗑 Auto-delete: <b>{ad_txt}</b>\n"
        f"👋 Welcome: <b>{'set' if s.get('welcome') else 'off'}</b>\n"
        f"🎬 IMDB info: <b>{'ON' if imdb else 'OFF'}</b>\n"
        f"🖼 Poster: <b>{'ON' if s.get('poster', True) else 'OFF'}</b>\n"
        f"💬 Start msg: <b>{'set' if s.get('start_message') else 'off'}</b>\n"
        f"📝 Caption tpl: <b>{'set' if s.get('caption_tpl') else 'off'}</b>\n"
        f"🤖 AI mode: <b>{ai_txt}</b>\n\n"
        f"<i>🛡 Mod: /gban /gunban /gwarn · 📣 /gbroadcast · "
        f"1000+ group admins: /ubroadcast</i>"
    )


def _can_manage(uid: int | None, g: Group | None) -> bool:
    """Bot admin, or the group admin who connected this group."""
    if not uid or not g:
        return False
    if settings.is_admin(uid):
        return True
    return (g.settings or {}).get("connected_by") == uid


async def _can_manage_async(client: Client, uid: int | None,
                           g: Group | None) -> bool:
    """v10.14: _can_manage + a LIVE get_chat_member fallback.

    Fixes groups where the bot was added directly (no connected_by
    stored): the group's real admins can still control the bot.
    """
    if _can_manage(uid, g):
        return True
    if not uid or not g:
        return False
    try:
        m = await client.get_chat_member(g.id, uid)
        return m.status in (ChatMemberStatus.ADMINISTRATOR,
                            ChatMemberStatus.OWNER)
    except Exception:
        return False


async def _manageable_groups(uid: int) -> list[Group]:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        rows = (await s.execute(select(Group).order_by(Group.title))).all()
        groups = [r[0] for r in rows]
    if settings.is_admin(uid):
        return groups
    return [g for g in groups if _can_manage(uid, g)]


# ---------- /connect (in group) ----------

async def _group_start(client: Client, message: Message):
    """v10.10.3: /start inside a group — the private handler is PM-only."""
    await message.reply_text(
        "👋 <b>Moovidex</b> — your personal movie finder.\n\n"
        "🔍 Just type a movie or series name here to search.\n"
        "🛠 Group admins: run /connect to manage this group.",
        parse_mode=ParseMode.HTML)


async def _connect(client: Client, message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("Run /connect inside the group.")
        return
    uid = message.from_user.id if message.from_user else None
    ok = False
    if uid is None and message.sender_chat \
            and message.sender_chat.id == message.chat.id:
        # v10.14: anonymous admin — posting as the group implies rights.
        ok = True
    elif uid:
        try:
            m = await client.get_chat_member(message.chat.id, uid)
            ok = m.status in (ChatMemberStatus.ADMINISTRATOR,
                              ChatMemberStatus.OWNER)
        except Exception:
            ok = False
    if not ok:
        await message.reply_text(
            "⛔ Only group admins can connect." if uid
            else "⚠️ Couldn't verify admin status.")
        return
    await _save_group(message.chat.id, message.chat.title,
                      connected_by=uid)
    await message.reply_text(
        f"✅ <b>{message.chat.title}</b> connected.\n"
        "Manage it from my PM with /groups.",
        parse_mode=ParseMode.HTML)


async def _set_main_group(client: Client, message: Message):
    """v10.14: /setmaingroup — bot-owner-only, run inside the group.

    Marks the group as the owner's main group: every feature unlocked.
    """
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("Run /setmaingroup inside the group.")
        return
    uid = message.from_user.id if message.from_user else None
    if not uid or not settings.is_admin(uid):
        await message.reply_text("⛔ Bot owner only.")
        return
    await rt.set_setting("OWNER_GROUP_ID", str(message.chat.id))
    await _save_group(message.chat.id, message.chat.title)
    await message.reply_text(
        f"✅ Owner group set: <b>{ui.esc(message.chat.title or '')}</b>\n"
        "All features unlocked here.", parse_mode=ParseMode.HTML)


async def _locked_cb(client: Client, query):
    """v10.14: 🔒 tier buttons — explain the member requirement."""
    try:
        need = int((query.data or "").split(":")[2])
        gid = int((query.data or "").split(":")[3])
    except (ValueError, IndexError):
        await query.answer("🔒 Locked.", show_alert=True)
        return
    n = await group_member_count(client, gid)
    await query.answer(
        f"🔒 Needs {need}+ members (this group: {n}).", show_alert=True)


async def _require_group_admin(client: Client,
                             message: Message) -> int | None:
    """v10.14: gate for in-group mod commands.

    Returns the admin uid (0 = anonymous admin), or None after replying.
    """
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("Use this inside the group.")
        return None
    uid = message.from_user.id if message.from_user else None
    if uid is None and message.sender_chat \
            and message.sender_chat.id == message.chat.id:
        return 0  # anonymous admin
    if uid and settings.is_admin(uid):
        return uid
    try:
        m = await client.get_chat_member(message.chat.id, uid)
        if m.status in (ChatMemberStatus.ADMINISTRATOR,
                        ChatMemberStatus.OWNER):
            return uid
    except Exception:
        pass
    await message.reply_text("⛔ Only group admins can use this.")
    return None


def _ban_target(message: Message) -> int | None:
    """Target user for /gban|/gunban|/gwarn: reply, else first arg."""
    r = message.reply_to_message
    if r and r.from_user and not r.from_user.is_bot:
        return r.from_user.id
    parts = (message.text or "").split()
    if len(parts) > 1:
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


async def _is_gbanned(chat_id: int, uid: int) -> bool:
    """v10.14: is this user group-banned here?"""
    try:
        g = await _get_group(chat_id)
        return str(uid) in ((g.settings or {}).get("gbanned") or {})
    except Exception:
        return False


async def _gban(client: Client, message: Message):
    """/gban — group-admin-only. Usage: reply, or /gban <id> [reason]."""
    if await _require_group_admin(client, message) is None:
        return
    target = _ban_target(message)
    if not target:
        await message.reply_text(
            "Usage: reply to a user with <code>/gban</code>, or "
            "<code>/gban &lt;user_id&gt; [reason]</code>.",
            parse_mode=ParseMode.HTML)
        return
    parts = (message.text or "").split(None, 2)
    reason = parts[2] if len(parts) > 2 else ""

    def _mut(s):
        gb = dict(s.get("gbanned") or {})
        gb[str(target)] = reason
        s["gbanned"] = gb
        return s

    await _save_group(message.chat.id, None, mutate=_mut)
    await message.reply_text(
        f"⛔ Banned <code>{target}</code> in this group."
        + (f" Reason: {ui.esc(reason)}" if reason else ""),
        parse_mode=ParseMode.HTML)


async def _gunban(client: Client, message: Message):
    """/gunban — group-admin-only. Usage: reply, or /gunban <id>."""
    if await _require_group_admin(client, message) is None:
        return
    target = _ban_target(message)
    if not target:
        await message.reply_text(
            "Usage: reply to a user with <code>/gunban</code>, or "
            "<code>/gunban &lt;user_id&gt;</code>.",
            parse_mode=ParseMode.HTML)
        return

    def _mut(s):
        gb = dict(s.get("gbanned") or {})
        gb.pop(str(target), None)
        s["gbanned"] = gb
        gw = dict(s.get("gwarns") or {})
        gw.pop(str(target), None)
        s["gwarns"] = gw
        return s

    await _save_group(message.chat.id, None, mutate=_mut)
    await message.reply_text(f"✅ Unbanned <code>{target}</code>.",
                             parse_mode=ParseMode.HTML)


async def _gwarn(client: Client, message: Message):
    """/gwarn — group-admin-only. 3 warns = auto group-ban."""
    if await _require_group_admin(client, message) is None:
        return
    target = _ban_target(message)
    if not target:
        await message.reply_text(
            "Usage: reply to a user with <code>/gwarn</code>, or "
            "<code>/gwarn &lt;user_id&gt;</code>.",
            parse_mode=ParseMode.HTML)
        return
    warn_limit = 3
    try:
        warn_limit = int(await rt.aget_setting("WARN_LIMIT") or 3)
    except (TypeError, ValueError):
        pass

    def _mut(s):
        gw = dict(s.get("gwarns") or {})
        gw[str(target)] = int(gw.get(str(target)) or 0) + 1
        s["gwarns"] = gw
        return s

    g = await _save_group(message.chat.id, None, mutate=_mut)
    n = int((g.settings or {}).get("gwarns", {}).get(str(target), 0))
    if n >= warn_limit:
        def _ban(s):
            gb = dict(s.get("gbanned") or {})
            gb[str(target)] = f"auto-ban: {n} warns"
            s["gbanned"] = gb
            return s

        await _save_group(message.chat.id, None, mutate=_ban)
        await message.reply_text(
            f"⛔ <code>{target}</code> reached {n} warns — banned.",
            parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(
            f"⚠️ Warned <code>{target}</code> ({n}/{warn_limit}).",
            parse_mode=ParseMode.HTML)


async def _gbroadcast(client: Client, message: Message):
    """/gbroadcast — group-admin-only: send to THIS group only."""
    if await _require_group_admin(client, message) is None:
        return
    src = message.reply_to_message
    text = (message.text or "").partition(" ")[2].strip()
    if not src and not text:
        await message.reply_text(
            "Usage: reply to a message with <code>/gbroadcast</code>, or "
            "<code>/gbroadcast &lt;text&gt;</code>.",
            parse_mode=ParseMode.HTML)
        return
    try:
        if src:
            await src.copy(message.chat.id)
        else:
            await client.send_message(message.chat.id, text,
                                      parse_mode=ParseMode.HTML)
        await message.reply_text("📢 Sent to this group.")
    except Exception as exc:  # noqa: BLE001
        await message.reply_text(f"❌ Failed: {ui.esc(str(exc)[:120])}",
                                 parse_mode=ParseMode.HTML)


async def _ubroadcast(client: Client, message: Message):
    """v10.14: /ubroadcast — group-admin of a 1000+ group (or bot owner)
    broadcasts to ALL bot PM users. 1000+ tier."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("Use /ubroadcast inside the group.")
        return
    uid = message.from_user.id if message.from_user else None
    if not (uid and settings.is_admin(uid)):
        if await _require_group_admin(client, message) is None:
            return
        if not await group_unlocked(client, message.chat.id, TIER_1000):
            n = await group_member_count(client, message.chat.id)
            await message.reply_text(
                f"🔒 /ubroadcast needs 1000+ members (this group: {n}).")
            return
    src = message.reply_to_message
    text = (message.text or "").partition(" ")[2].strip()
    if not src and not text:
        await message.reply_text(
            "Usage: reply to a message with <code>/ubroadcast</code>, or "
            "<code>/ubroadcast &lt;text&gt;</code>.",
            parse_mode=ParseMode.HTML)
        return
    from app.bot.handlers.common import broadcast_to_users
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        ids = (await s.execute(
            select(User.id).where(User.is_banned.is_(False)))).scalars().all()
    status = await message.reply_text(
        f"📢 Broadcasting to {len(ids):,} users…")

    async def _prog(sent: int, failed: int, total: int) -> None:
        try:
            await status.edit_text(
                f"📢 {sent + failed:,}/{total:,}… ✅{sent} ❌{failed}")
        except Exception:
            pass

    sent, failed = await broadcast_to_users(
        client, list(ids), text=text or None, src_msg=src,
        progress_cb=_prog)
    await status.edit_text(f"📢 Done. ✅ {sent:,} sent, ❌ {failed:,} failed.")


async def _group_settings(client: Client, message: Message):
    """v10.14: /settings inside the group — opens the panel in-group."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("Run /settings inside the group.")
        return
    uid = message.from_user.id if message.from_user else None
    g = await _get_group(message.chat.id)
    if g is None:
        await _save_group(message.chat.id, message.chat.title)
        g = await _get_group(message.chat.id)
    anonymous = (uid is None and message.sender_chat
                 and message.sender_chat.id == message.chat.id)
    if not anonymous and not await _can_manage_async(client, uid, g):
        await message.reply_text("⛔ Only group admins.")
        return
    await message.reply_text(await _panel_text(client, g), reply_markup=await _panel_kb(client, g),
                             parse_mode=ParseMode.HTML)


async def _bot_added(client: Client, message: Message):
    me = await client.get_me()
    if not any(u.id == me.id for u in (message.new_chat_members or [])):
        return
    await _save_group(message.chat.id, message.chat.title)
    welcome = await rt.aget_setting("WELCOME_GROUP")
    if welcome:
        try:
            await message.reply_text(welcome, parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def _welcome_new_members(client: Client, message: Message):
    """v10.14.2 rewrite: send the group's custom welcome to new humans.

    Reads the group's settings fresh on every join event; never raises.
    Set via the group panel → 👋 Welcome ("off" clears it).
    ``{name}`` becomes the new member's first name.
    """
    try:
        g = await _get_group(message.chat.id)
    except Exception as exc:  # noqa: BLE001
        log.debug("welcome: group lookup failed: %s", exc)
        return
    tmpl = (g.settings or {}).get("welcome") if g else None
    if not tmpl:
        return
    for u in message.new_chat_members or []:
        if getattr(u, "is_bot", False):
            continue
        text = tmpl.replace("{name}", u.first_name or "friend")
        try:
            await message.reply_text(text, parse_mode=ParseMode.HTML)
            log.info("welcome sent in group %s to user %s",
                     message.chat.id, u.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("welcome send failed in %s: %s",
                        message.chat.id, exc)


# ---------- /groups (PM) ----------

async def _groups(client: Client, message: Message):
    """v10.10.1: any user sees the groups THEY connected; the bot admin
    sees everything."""
    uid = message.from_user.id if message.from_user else None
    groups = await _manageable_groups(uid) if uid else []
    if not groups:
        await message.reply_text(
            "No groups connected yet.\n"
            "Add me to your group as admin, run /connect there, "
            "then manage it from here 👇")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"👪 {(g.title or g.id)}"[:40],
                             callback_data=f"grp:{g.id}")]
        for g in groups[:50]])
    await message.reply_text("👪 <b>Connected groups</b> — tap to manage:",
                             reply_markup=kb, parse_mode=ParseMode.HTML)


async def _grp_open(client: Client, query):
    gid = int(query.data.split(":")[1])
    g = await _get_group(gid)
    if not g:
        await query.answer("Group not found.", show_alert=True)
        return
    await query.message.edit_text(await _panel_text(client, g), reply_markup=await _panel_kb(client, g),
                                  parse_mode=ParseMode.HTML)
    await query.answer()


async def _grp_list(client: Client, query):
    uid = query.from_user.id if query.from_user else None
    groups = await _manageable_groups(uid) if uid else []
    if not groups:
        await query.message.edit_text("No groups connected yet.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"👪 {(g.title or g.id)}"[:40],
                             callback_data=f"grp:{g.id}")]
        for g in groups[:50]])
    await query.message.edit_text("👪 <b>Connected groups</b> — tap to manage:",
                                  reply_markup=kb, parse_mode=ParseMode.HTML)
    await query.answer()


async def _ad_menu(client: Client, query):
    gid = int(query.data.split(":")[1])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"grpad:{gid}:{sec}")
         for label, sec in AD_CHOICES],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"grp:{gid}")],
    ])
    await query.message.edit_text(
        "🗑 <b>Auto-delete</b> — bot-sent results/files in this group "
        "are deleted after:", reply_markup=kb, parse_mode=ParseMode.HTML)
    await query.answer()


async def _ad_set(client: Client, query):
    _, gid, sec = query.data.split(":")
    gid, sec = int(gid), int(sec)
    if sec < 0:
        # v10.14: 🌐 Global default — delete the override, inherit global.
        def _drop(s):
            s.pop("autodelete_seconds", None)
            return s
        await _save_group(gid, None, mutate=_drop)
    else:
        await _save_group(gid, None,
                          mutate=lambda s: {**s, "autodelete_seconds": sec})
    g = await _get_group(gid)
    await query.message.edit_text(await _panel_text(client, g), reply_markup=await _panel_kb(client, g),
                                  parse_mode=ParseMode.HTML)
    await query.answer("Saved.")


async def _ask_reply(client: Client, query, action: str, prompt: str):
    """v10.14.2 rewrite: arm a group-setting reply prompt.

    The pending state is DB-backed (state.gpending_*) so the setting
    survives restarts and a briefly-duplicate instance — the old
    in-memory dict silently lost it in both cases.
    """
    gid = int(query.data.split(":")[1])
    uid = query.from_user.id
    await state.gpending_set(uid, {
        "action": action, "gid": gid,
        "panel_msg_id": query.message.id,
        "panel_chat_id": query.message.chat.id,
    })
    # Turn the panel itself into the prompt — no extra message.
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel",
                             callback_data=f"grp:cancel:{gid}")]])
    await query.message.edit_text(
        prompt + "\n<i>Reply here in PM. /cancel to abort.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb)
    await query.answer()


async def _grp_del(client: Client, query):
    gid = int(query.data.split(":")[1])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, disconnect",
                              callback_data=f"grpdelok:{gid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"grp:{gid}")],
    ])
    await query.message.edit_text("Disconnect this group? Its per-group "
                                  "settings will be lost.",
                                  reply_markup=kb)
    await query.answer()


async def _grp_del_ok(client: Client, query):
    gid = int(query.data.split(":")[1])
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        g = (await s.execute(
            select(Group).where(Group.id == gid))).scalar_one_or_none()
        if g:
            await s.delete(g)
            await s.commit()
    await query.answer("Disconnected.")
    await _grp_list(client, query)


async def _pending_reply(client: Client, message: Message):
    """v10.14.2 rewrite: catch PM replies for group-setting edits.

    Pending state is DB-backed (state.gpending_*) — never silently
    lost to a restart. Every outcome tells the admin exactly what
    happened, and every save/clear is logged (Render logs prove it).
    """
    from pyrogram import StopPropagation

    uid = message.from_user.id if message.from_user else None
    if not uid or not message.text:
        return
    pend = await state.gpending_get(uid)
    if not pend:
        return

    gid = pend["gid"]
    action = pend["action"]
    label = _ACTION_LABELS.get(action, "Setting")

    async def _done(note: str):
        """Clear the prompt, restore the panel, confirm to the admin."""
        await state.gpending_clear(uid)
        try:
            await message.delete()  # panel shows the new value
        except Exception:
            pass
        g = await _get_group(gid)
        if g:
            try:
                await client.edit_message_text(
                    pend.get("panel_chat_id") or message.chat.id,
                    pend.get("panel_msg_id"),
                    await _panel_text(client, g),
                    reply_markup=await _panel_kb(client, g),
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass
        # Explicit confirmation — the admin always knows it saved.
        try:
            await message.reply_text(note, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        raise StopPropagation

    text = message.text.strip()
    if text == "/cancel":
        await _done("❌ Cancelled.")

    if text.lower() == "off":
        drop = _ACTION_KEYS[action]
        await _save_group(
            gid, None,
            mutate=lambda s: {k: v for k, v in s.items() if k not in drop})
        log.info("group %s: %s cleared by %s", gid, action, uid)
        await _done(f"✅ <b>{label} cleared.</b>")

    if action == "start_btn":
        # "Button Text | https://…" under the start message.
        parts = [p.strip() for p in text.split("|", 1)]
        if not (len(parts) == 2 and parts[0] and parts[1]):
            await message.reply_text(
                "❌ Send as <code>Button Text | https://…</code>\n"
                "Try again, or /cancel to abort.",
                parse_mode=ParseMode.HTML)
            raise StopPropagation
        await _save_group(
            gid, None,
            mutate=lambda s: {**s, "start_btn_text": parts[0][:60],
                              "start_btn_url": parts[1][:300]})
    else:
        key = _ACTION_SAVE_KEY[action]
        limit = _ACTION_LIMITS[action]
        await _save_group(gid, None,
                          mutate=lambda s: {**s, key: text[:limit]})
    log.info("group %s: %s set by %s", gid, action, uid)
    await _done(f"✅ <b>{label} saved.</b>\n<i>{ui.esc(text[:120])}</i>")


def _admin_cb(func):
    async def wrapper(client: Client, query):
        uid = query.from_user.id if query.from_user else None
        if not settings.is_admin(uid):
            await query.answer("⛔ Admins only.", show_alert=True)
            return
        return await func(client, query)
    wrapper.__name__ = func.__name__
    return wrapper


def _mgr_cb(func):
    """v10.10.1: the bot admin, or the group admin who connected it.

    v10.14: falls back to a live get_chat_member check so real group
    admins can manage the bot even when it was added without /connect.
    """
    async def wrapper(client: Client, query):
        uid = query.from_user.id if query.from_user else None
        if settings.is_admin(uid):
            return await func(client, query)
        gid = None
        try:
            parts = (query.data or "").split(":")
            # 3-part form grp:<action>:<gid> — gid is last there.
            gid = int(parts[2] if len(parts) > 2 and parts[0] == "grp"
                      else parts[1])
        except (ValueError, IndexError):
            pass
        g = await _get_group(gid) if gid else None
        if not await _can_manage_async(client, uid, g):
            await query.answer("⛔ Only this group's admin can do that.",
                               show_alert=True)
            return
        return await func(client, query)
    wrapper.__name__ = func.__name__
    return wrapper


async def _grp_imdb(client: Client, query):
    """Toggle per-group IMDB info/posters."""
    gid = int(query.data.split(":")[1])
    g = await _get_group(gid)
    if not g:
        await query.answer("Group not found.", show_alert=True)
        return
    cur = bool((g.settings or {}).get("imdb_enabled", True))
    await _save_group(gid, None,
                      mutate=lambda s: {**s, "imdb_enabled": not cur})
    g = await _get_group(gid)
    await query.message.edit_text(await _panel_text(client, g), reply_markup=await _panel_kb(client, g),
                                  parse_mode=ParseMode.HTML)
    await query.answer(f"🎬 IMDB {'ON' if not cur else 'OFF'}")


async def _grp_poster(client: Client, query):
    """v10.14: toggle per-group TMDB poster (default ON)."""
    gid = int(query.data.split(":")[2])
    g = await _get_group(gid)
    if not g:
        await query.answer("Group not found.", show_alert=True)
        return
    cur = bool((g.settings or {}).get("poster", True))
    await _save_group(gid, None,
                      mutate=lambda s: {**s, "poster": not cur})
    g = await _get_group(gid)
    await query.message.edit_text(await _panel_text(client, g), reply_markup=await _panel_kb(client, g),
                                  parse_mode=ParseMode.HTML)
    await query.answer(f"🖼 Poster {'ON' if not cur else 'OFF'}")


async def _grp_ai(client: Client, query):
    """v10.14.1: per-group AI mode toggle (3000+ tier, default ON)."""
    gid = int(query.data.split(":")[2])
    g = await _get_group(gid)
    if not g:
        await query.answer("Group not found.", show_alert=True)
        return
    cur = bool((g.settings or {}).get("ai_mode", True))
    await _save_group(gid, None,
                      mutate=lambda s: {**s, "ai_mode": not cur})
    g = await _get_group(gid)
    await query.message.edit_text(await _panel_text(client, g), reply_markup=await _panel_kb(client, g),
                                  parse_mode=ParseMode.HTML)
    await query.answer(f"🤖 AI mode {'ON' if not cur else 'OFF'}")


async def _grp_cancel(client: Client, query):
    """v10.14.2: cancel a pending group-setting reply (❌ Cancel button).

    Clears the DB-backed prompt, restores the panel, and tells the
    admin — the reply-capture is always disarmed.
    """
    uid = query.from_user.id if query.from_user else None
    pend = await state.gpending_get(uid) if uid else None
    if uid:
        await state.gpending_clear(uid)
    await query.answer("Cancelled.")
    gid = pend.get("gid") if pend else None
    if gid is None:
        try:
            gid = int((query.data or "").split(":")[1])
        except (ValueError, IndexError):
            gid = None
    if gid:
        g = await _get_group(gid)
    else:
        g = None
    if g:
        try:
            chat_id = (pend.get("panel_chat_id")
                       if pend else query.message.chat.id)
            msg_id = (pend.get("panel_msg_id")
                      if pend else query.message.id)
            await client.edit_message_text(
                chat_id, msg_id,
                await _panel_text(client, g),
                reply_markup=await _panel_kb(client, g),
                parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    try:
        await query.message.edit_text("❌ Cancelled.")
    except Exception:
        pass


def register(bot: Client) -> None:
    bot.on_message(filters.group & filters.command("connect"))(_connect)
    bot.on_message(filters.group & filters.command("start"))(_group_start)
    # v10.14: group management.
    bot.on_message(filters.group & filters.command("setmaingroup"))(
        _set_main_group)
    bot.on_message(filters.group & filters.command("settings"))(
        _group_settings)
    bot.on_message(filters.group & filters.command("gban"))(_gban)
    bot.on_message(filters.group & filters.command("gunban"))(_gunban)
    bot.on_message(filters.group & filters.command("gwarn"))(_gwarn)
    bot.on_message(filters.group & filters.command("gbroadcast"))(
        _gbroadcast)
    bot.on_message(filters.group & filters.command("ubroadcast"))(
        _ubroadcast)
    bot.on_message(filters.group & filters.new_chat_members)(_bot_added)
    bot.on_message(filters.group & filters.new_chat_members)(
        _welcome_new_members)
    bot.on_message(filters.private & filters.command("groups"))(_groups)
    # pending replies must run before the search text handler
    bot.on_message(filters.private & filters.text,
                   group=-1)(_pending_reply)

    bot.on_callback_query(filters.regex(r"^grp:locked:\d+:-?\d+$"))(_locked_cb)
    bot.on_callback_query(filters.regex(r"^grp:-?\d+$"))(_mgr_cb(_grp_open))
    bot.on_callback_query(filters.regex(r"^grplist$"))(_grp_list)
    bot.on_callback_query(filters.regex(r"^grpadmenu:-?\d+$"))(_mgr_cb(_ad_menu))
    bot.on_callback_query(filters.regex(r"^grpad:-?\d+:-?\d+$"))(_mgr_cb(_ad_set))
    bot.on_callback_query(filters.regex(r"^grpwelcome:-?\d+$"))(
        _mgr_cb(lambda c, q: _ask_reply(
            c, q, "welcome",
            "👋 Send the welcome text for new members, or <code>off</code> to clear.")))
    bot.on_callback_query(filters.regex(r"^grpimdb:-?\d+$"))(_mgr_cb(_grp_imdb))
    # v10.14: tiered settings.
    bot.on_callback_query(filters.regex(r"^grp:poster:-?\d+$"))(
        _mgr_cb(_grp_poster))
    bot.on_callback_query(filters.regex(r"^grp:ai:-?\d+$"))(
        _mgr_cb(_grp_ai))
    bot.on_callback_query(filters.regex(r"^grp:cancel:-?\d+$"))(
        _mgr_cb(_grp_cancel))
    bot.on_callback_query(filters.regex(r"^grpsmsg:-?\d+$"))(
        _mgr_cb(lambda c, q: _ask_reply(
            c, q, "startmsg",
            "💬 Send the custom <b>/start</b> message for this group's "
            "start link, or <code>off</code> to clear.")))
    bot.on_callback_query(filters.regex(r"^grpcap:-?\d+$"))(
        _mgr_cb(lambda c, q: _ask_reply(
            c, q, "caption_tpl",
            "📝 Send the caption template — placeholders "
            "<code>{name}</code> <code>{quality}</code> <code>{lang}</code> "
            "<code>{size}</code> — or <code>off</code> to clear.")))
    bot.on_callback_query(filters.regex(r"^grpsbtn:-?\d+$"))(
        _mgr_cb(lambda c, q: _ask_reply(
            c, q, "start_btn",
            "🔘 Send <code>Button Text | https://…</code> for the button "
            "under the start message, or <code>off</code> to clear.")))
    bot.on_callback_query(filters.regex(r"^grpdel:-?\d+$"))(_mgr_cb(_grp_del))
    bot.on_callback_query(filters.regex(r"^grpdelok:-?\d+$"))(_mgr_cb(_grp_del_ok))
