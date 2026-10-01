"""Admin commands: stats, broadcast, ban/unban, users, requests, settings."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, PeerIdInvalid, UserIsBlocked
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.bot.handlers.common import admin_only, track_user
from app import runtime as rt
from app.config import settings
from app.db import get_session_factory
from app.models import BackfillJob, File, Group, MovieRequest, User

log = logging.getLogger(__name__)


@admin_only
async def _stats(client: Client, message: Message):
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        files = (await s.execute(select(func.count(File.id)))).scalar() or 0
        users = (await s.execute(select(func.count(User.id)))).scalar() or 0
        groups = (await s.execute(select(func.count(Group.id)))).scalar() or 0
        open_req = (await s.execute(
            select(func.count(MovieRequest.id)).where(
                MovieRequest.status == "open"))).scalar() or 0
        jobs = (await s.execute(
            select(func.count(BackfillJob.id)).where(
                BackfillJob.status == "running"))).scalar() or 0
    await message.reply_text(
        "📊 <b>Bot stats</b>\n\n"
        f"📦 Files: <b>{files:,}</b>\n"
        f"👥 Users: <b>{users:,}</b>\n"
        f"👪 Groups: <b>{groups:,}</b>\n"
        f"🎞 Open requests: <b>{open_req}</b>\n"
        f"📥 Running index jobs: <b>{jobs}</b>",
        parse_mode=ParseMode.HTML)


@admin_only
async def _users(client: Client, message: Message):
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        total = (await s.execute(select(func.count(User.id)))).scalar() or 0
        banned = (await s.execute(
            select(func.count(User.id)).where(User.is_banned.is_(True))
        )).scalar() or 0
        recent = (await s.execute(
            select(User).order_by(User.joined_at.desc()).limit(5)
        )).scalars().all()
    lines = [f"👥 <b>Users:</b> {total:,} (🚫 {banned} banned)\n",
             "<b>Recent:</b>"]
    for u in recent:
        name = (u.first_name or "")[:20]
        lines.append(f"• {name} (@{u.username or '—'}) <code>{u.id}</code>")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def _ban(client: Client, message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.reply_text("Usage: <code>/ban &lt;user_id&gt;</code>")
        return
    uid = int(parts[1])
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one_or_none()
        if u:
            u.is_banned = True
        else:
            s.add(User(id=uid, is_banned=True))
        await s.commit()
    await message.reply_text(f"🚫 Banned <code>{uid}</code>.")


@admin_only
async def _unban(client: Client, message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.reply_text("Usage: <code>/unban &lt;user_id&gt;</code>")
        return
    uid = int(parts[1])
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one_or_none()
        if u:
            u.is_banned = False
            await s.commit()
    await message.reply_text(f"✅ Unbanned <code>{uid}</code>.")


@admin_only
async def _broadcast(client: Client, message: Message):
    # /broadcast [groups] <text> — or reply to a message with /broadcast [groups]
    args = (message.text or "").split()
    target = "users"
    if len(args) > 1 and args[1].lower() == "groups":
        target = "groups"
        text = message.text.partition("groups")[2].strip()
    else:
        text = message.text.partition(" ")[2].strip()
    src = message.reply_to_message
    if not src and not text:
        await message.reply_text(
            "Usage: reply to a message with <code>/broadcast</code>, "
            "or <code>/broadcast &lt;text&gt;</code>\n"
            "Add <code>groups</code> to target groups: "
            "<code>/broadcast groups &lt;text&gt;</code>")
        return
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        if target == "groups":
            ids = (await s.execute(select(Group.id))).scalars().all()
        else:
            ids = (await s.execute(
                select(User.id).where(User.is_banned.is_(False)))).scalars().all()
    status = await message.reply_text(
        f"📢 Broadcasting to {len(ids):,} {target}…")
    sent = failed = 0
    for uid in ids:
        try:
            if src:
                await src.copy(uid)
            else:
                await client.send_message(uid, text,
                                          parse_mode=ParseMode.HTML)
            sent += 1
        except (UserIsBlocked, PeerIdInvalid):
            failed += 1
        except FloodWait as exc:
            await asyncio.sleep(exc.value + 1)
        except Exception:
            failed += 1
        if (sent + failed) % 100 == 0:
            try:
                await status.edit_text(
                    f"📢 {sent + failed:,}/{len(ids):,}… ✅{sent} ❌{failed}")
            except Exception:
                pass
    await status.edit_text(f"📢 Done. ✅ {sent:,} sent, ❌ {failed:,} failed.")


@admin_only
async def _warn(client: Client, message: Message):
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.reply_text(
            "Usage: <code>/warn &lt;user_id&gt; [reason]</code>")
        return
    uid = int(parts[1])
    reason = " ".join(parts[2:]) or "no reason given"
    limit = int(await rt.aget_setting("WARN_LIMIT") or 3)
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one_or_none()
        if not u:
            u = User(id=uid, warns=0)
            s.add(u)
        u.warns = (u.warns or 0) + 1
        warns = u.warns
        auto = warns >= limit
        if auto:
            u.is_banned = True
        await s.commit()
    try:
        await client.send_message(
            uid, f"⚠️ <b>Warning {warns}/{limit}</b>\nReason: {reason}\n"
            + ("🚫 You have been <b>banned</b>." if auto else
               "Further violations may lead to a ban."),
            parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await message.reply_text(
        f"⚠️ Warned <code>{uid}</code> ({warns}/{limit})."
        + (" 🚫 Auto-banned." if auto else ""))


@admin_only
async def _requests(client: Client, message: Message):
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        rows = (await s.execute(
            select(MovieRequest).where(MovieRequest.status == "open")
            .order_by(MovieRequest.created_at.desc()).limit(10)
        )).scalars().all()
    if not rows:
        await message.reply_text("🎞 No open requests.")
        return
    for r in rows:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Done",
                                 callback_data=f"reqdone:{r.id}"),
            InlineKeyboardButton("❌ Reject",
                                 callback_data=f"reqrej:{r.id}"),
        ]])
        await message.reply_text(
            f"🎞 <b>#{r.id}</b> by <code>{r.user_id}</code>\n{r.text[:300]}",
            reply_markup=kb)


async def _req_action(client: Client, query):
    uid = query.from_user.id if query.from_user else None
    if not settings.is_admin(uid):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    action, rid = query.data.split(":")
    rid = int(rid)
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        r = (await s.execute(
            select(MovieRequest).where(MovieRequest.id == rid)
        )).scalar_one_or_none()
        if not r or r.status != "open":
            await query.answer("Already handled.", show_alert=True)
            return
        r.status = "done" if action == "reqdone" else "rejected"
        user_id = r.user_id
        await s.commit()
    await query.answer("Marked.")
    try:
        await query.message.edit_text(
            query.message.text + f"\n\n{'✅ Done' if action == 'reqdone' else '❌ Rejected'}")
    except Exception:
        pass
    if user_id:
        try:
            await client.send_message(
                user_id,
                f"🎞 Your request <i>{r.text[:100]}</i> was "
                f"<b>{'fulfilled ✅' if action == 'reqdone' else 'rejected ❌'}</b>.")
        except Exception:
            pass


@admin_only
async def _dbcheck(client: Client, message: Message):
    """Report migration state without needing Render shell access."""
    from sqlalchemy import text as sa_text

    lines = []
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            ver = (await s.execute(
                sa_text("SELECT version_num FROM alembic_version")
            )).scalar()
            lines.append(f"🔖 alembic version: <code>{ver}</code>")
    except Exception as exc:
        lines.append(f"🔖 alembic version: ❌ {exc}")
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            exists = (await s.execute(
                sa_text("SELECT to_regclass('public.index_sessions')")
            )).scalar()
            if exists:
                lines.append("🗂 index_sessions table: ✅ exists")
            else:
                lines.append("🗂 index_sessions table: ❌ MISSING — "
                             "the /index number bug will persist until "
                             "<code>alembic upgrade head</code> runs")
    except Exception as exc:
        lines.append(f"🗂 index_sessions table: ❌ {exc}")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def _settings(client: Client, message: Message):
    base = (settings.WEB_URL or "").rstrip("/")
    dash = f"{base}/admin" if base else "/admin"
    await message.reply_text(
        "⚙️ <b>Settings</b>\n\n"
        "All settings are now managed in the admin dashboard:\n"
        f"🔗 {dash}\n\n"
        "<i>Force-sub, auto-delete, results/page, protect content, TMDB, "
        "request/log channels, welcome texts, warn limit…</i>",
        parse_mode=ParseMode.HTML)


def register(bot: Client) -> None:
    bot.on_message(filters.private & filters.command("stats"))(_stats)
    bot.on_message(filters.private & filters.command("users"))(_users)
    bot.on_message(filters.private & filters.command("ban"))(_ban)
    bot.on_message(filters.private & filters.command("unban"))(_unban)
    bot.on_message(filters.private & filters.command("warn"))(_warn)
    bot.on_message(filters.private & filters.command("broadcast"))(_broadcast)
    bot.on_message(filters.private & filters.command("requests"))(_requests)
    bot.on_message(filters.private & filters.command("settings"))(_settings)
    bot.on_message(filters.private & filters.command("dbcheck"))(_dbcheck)
    bot.on_callback_query(filters.regex(r"^req(done|rej):"))(_req_action)
