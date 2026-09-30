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
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app import state
from app.bot.handlers.common import admin_only, track_user
from app.config import settings
from app.db import get_session_factory
from app.models import Group
from app import runtime as rt

log = logging.getLogger(__name__)

# uid -> {"action": "fsub"|"welcome", "gid": int} while awaiting a reply
_pending: dict[int, dict] = {}

AD_CHOICES = [("Off", 0), ("5 min", 300), ("15 min", 900),
              ("30 min", 1800), ("1 hour", 3600)]


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


def _panel_kb(g: Group) -> InlineKeyboardMarkup:
    ad = int((g.settings or {}).get("autodelete_seconds") or 0)
    ad_label = next((l for l, s in AD_CHOICES if s == ad), f"{ad // 60}m")
    fsub = (g.settings or {}).get("force_sub") or []
    welcome = (g.settings or {}).get("welcome")
    rows = [
        [InlineKeyboardButton(f"🗑 Auto-delete: {ad_label}",
                              callback_data=f"grpadmenu:{g.id}")],
        [InlineKeyboardButton(
            f"📢 Force-sub: {', '.join(fsub) if fsub else 'off'}",
            callback_data=f"grpfsub:{g.id}")],
        [InlineKeyboardButton(
            f"👋 Welcome: {'set' if welcome else 'off'}",
            callback_data=f"grpwelcome:{g.id}")],
        [InlineKeyboardButton("🔌 Disconnect",
                              callback_data=f"grpdel:{g.id}")],
        [InlineKeyboardButton("⬅️ All groups", callback_data="grplist")],
    ]
    return InlineKeyboardMarkup(rows)


def _panel_text(g: Group) -> str:
    s = g.settings or {}
    ad = int(s.get("autodelete_seconds") or 0)
    ad_txt = "off" if not ad else f"{ad // 60} min"
    fsub = s.get("force_sub") or []
    return (
        f"👪 <b>{g.title or g.id}</b>\n<code>{g.id}</code>\n\n"
        f"🗑 Auto-delete: <b>{ad_txt}</b>\n"
        f"📢 Force-sub: <b>{', '.join(fsub) if fsub else 'off'}</b>\n"
        f"👋 Welcome: <b>{'set' if s.get('welcome') else 'off'}</b>"
    )


# ---------- /connect (in group) ----------

async def _connect(client: Client, message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply_text("Run /connect inside the group.")
        return
    uid = message.from_user.id if message.from_user else None
    try:
        m = await client.get_chat_member(message.chat.id, uid)
        if m.status not in (ChatMemberStatus.ADMINISTRATOR,
                             ChatMemberStatus.OWNER):
            await message.reply_text("⛔ Only group admins can connect.")
            return
    except Exception:
        await message.reply_text("⚠️ Couldn't verify admin status.")
        return
    await _save_group(message.chat.id, message.chat.title,
                      connected_by=uid)
    await message.reply_text(
        f"✅ <b>{message.chat.title}</b> connected.\n"
        "Manage it from my PM with /groups.",
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


# ---------- /groups (PM, bot admin) ----------

@admin_only
async def _groups(client: Client, message: Message):
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        rows = (await s.execute(select(Group).order_by(Group.title))).all()
        groups = [r[0] for r in rows]
    if not groups:
        await message.reply_text(
            "No groups connected yet.\nAdd me to a group and run /connect there.")
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
    await query.message.edit_text(_panel_text(g), reply_markup=_panel_kb(g),
                                  parse_mode=ParseMode.HTML)
    await query.answer()


async def _grp_list(client: Client, query):
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        rows = (await s.execute(select(Group).order_by(Group.title))).all()
        groups = [r[0] for r in rows]
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
    await _save_group(gid, None,
                      mutate=lambda s: {**s, "autodelete_seconds": sec})
    g = await _get_group(gid)
    await query.message.edit_text(_panel_text(g), reply_markup=_panel_kb(g),
                                  parse_mode=ParseMode.HTML)
    await query.answer("Saved.")


async def _ask_reply(client: Client, query, action: str, prompt: str):
    gid = int(query.data.split(":")[1])
    uid = query.from_user.id
    _pending[uid] = {"action": action, "gid": gid}
    await query.message.reply_text(prompt + "\n<i>Reply here in PM. /cancel to abort.</i>",
                                   parse_mode=ParseMode.HTML)
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
    """Catch PM replies for group-setting edits."""
    uid = message.from_user.id if message.from_user else None
    pend = _pending.get(uid)
    if not pend or not message.text:
        return
    if message.text.strip() == "/cancel":
        _pending.pop(uid, None)
        await message.reply_text("Cancelled.")
        from pyrogram import StopPropagation
        raise StopPropagation
    text = message.text.strip()
    gid = pend["gid"]
    if pend["action"] == "fsub":
        if text.lower() == "off":
            chans = []
        else:
            chans = [c.strip() for c in text.replace(";", ",").split(",")
                     if c.strip()]
        await _save_group(gid, None,
                          mutate=lambda s: {**s, "force_sub": chans})
        await message.reply_text(
            f"📢 Force-sub: {', '.join(chans) if chans else 'off'}")
    elif pend["action"] == "welcome":
        if text.lower() == "off":
            await _save_group(gid, None,
                              mutate=lambda s: {k: v for k, v in s.items()
                                                if k != "welcome"})
            await message.reply_text("👋 Welcome cleared.")
        else:
            await _save_group(gid, None,
                              mutate=lambda s: {**s, "welcome": text[:1000]})
            await message.reply_text("👋 Welcome text saved.")
    _pending.pop(uid, None)
    from pyrogram import StopPropagation
    raise StopPropagation


def _admin_cb(func):
    async def wrapper(client: Client, query):
        uid = query.from_user.id if query.from_user else None
        if not settings.is_admin(uid):
            await query.answer("⛔ Admins only.", show_alert=True)
            return
        return await func(client, query)
    wrapper.__name__ = func.__name__
    return wrapper


def register(bot: Client) -> None:
    bot.on_message(filters.group & filters.command("connect"))(_connect)
    bot.on_message(filters.group & filters.new_chat_members)(_bot_added)
    bot.on_message(filters.private & filters.command("groups"))(_groups)
    # pending replies must run before the search text handler
    bot.on_message(filters.private & filters.text,
                   group=-1)(_pending_reply)

    bot.on_callback_query(filters.regex(r"^grp:\d+$"))(_admin_cb(_grp_open))
    bot.on_callback_query(filters.regex(r"^grplist$"))(_admin_cb(_grp_list))
    bot.on_callback_query(filters.regex(r"^grpadmenu:\d+$"))(_admin_cb(_ad_menu))
    bot.on_callback_query(filters.regex(r"^grpad:\d+:\d+$"))(_admin_cb(_ad_set))
    bot.on_callback_query(filters.regex(r"^grpfsub:\d+$"))(
        _admin_cb(lambda c, q: _ask_reply(
            c, q, "fsub",
            "📢 Send the force-sub channels (comma separated @usernames), or <code>off</code> to clear.")))
    bot.on_callback_query(filters.regex(r"^grpwelcome:\d+$"))(
        _admin_cb(lambda c, q: _ask_reply(
            c, q, "welcome",
            "👋 Send the welcome text for new members, or <code>off</code> to clear.")))
    bot.on_callback_query(filters.regex(r"^grpdel:\d+$"))(_admin_cb(_grp_del))
    bot.on_callback_query(filters.regex(r"^grpdelok:\d+$"))(_admin_cb(_grp_del_ok))
