"""Admin commands: stats, broadcast, ban/unban, users, requests, settings."""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, PeerIdInvalid, UserIsBlocked
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, or_, select

from app.bot.handlers.common import admin_only, track_user
from app.bot import ui
from app import runtime as rt
from app.config import settings
from app.db import get_session_factory
from app.models import BackfillJob, File, Group, MovieRequest, User

log = logging.getLogger(__name__)


@admin_only
async def _admin(client: Client, message: Message):
    """v10.8.10: the /admin dashboard — fully button-driven.

    Every admin function lives behind callback buttons: stats, users,
    requests, broadcast, cleanup, settings, force-sub, db check.
    """
    text, kb = await _home_panel()
    await message.reply_text(text, reply_markup=kb,
                             parse_mode=ParseMode.HTML,
                             disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# v10.8.10 button dashboard
# ---------------------------------------------------------------------------

# uid -> (action, data) for multi-step admin flows (broadcast text,
# cleanup keyword, force-sub list, ...)
_pending: dict[int, tuple[str, dict]] = {}
# short token -> (mode, param) for cleanup confirm buttons (callback_data
# has a 64-byte limit, so long keywords ride a token instead)
_clean_tokens: dict[str, tuple[str, str]] = {}


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="adm:home")]])


async def _home_panel() -> tuple[str, InlineKeyboardMarkup]:
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as s:
            files = (await s.execute(
                select(func.count(File.id)))).scalar() or 0
            users = (await s.execute(
                select(func.count(User.id)))).scalar() or 0
            open_req = (await s.execute(
                select(func.count(MovieRequest.id)).where(
                    MovieRequest.status == "open"))).scalar() or 0
        db = "✅"
    except Exception:
        files = users = open_req = 0
        db = "❌"
    text = (f"👑 <b>Admin panel</b>  {db}\n\n"
            f"📦 <b>{files:,}</b> files · 👥 <b>{users:,}</b> users · "
            f"🎞 <b>{open_req}</b> open requests\n\n"
            "<i>Tap a section to manage it — no commands needed.</i>")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="adm:stats"),
         InlineKeyboardButton("👥 Users", callback_data="adm:users")],
        [InlineKeyboardButton(f"🎞 Requests ({open_req})",
                              callback_data="adm:reqs"),
         InlineKeyboardButton("📢 Broadcast", callback_data="adm:bcast")],
        [InlineKeyboardButton("🧹 Cleanup", callback_data="adm:clean"),
         InlineKeyboardButton("⚙️ Settings", callback_data="adm:set")],
        [InlineKeyboardButton("📢 Force-sub", callback_data="adm:fsub"),
         InlineKeyboardButton("🗄 DB check", callback_data="adm:dbcheck")],
    ])
    web = (settings.WEB_URL or "").rstrip("/")
    if web.startswith("https://"):
        kb.inline_keyboard.append([InlineKeyboardButton(
            "🌐 Web dashboard", url=f"{web}/admin")])
    return text, kb


async def _stats_panel() -> tuple[str, InlineKeyboardMarkup]:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        files = (await s.execute(select(func.count(File.id)))).scalar() or 0
        total_bytes = (await s.execute(
            select(func.coalesce(func.sum(File.file_size), 0)))).scalar() or 0
        users = (await s.execute(select(func.count(User.id)))).scalar() or 0
        groups = (await s.execute(select(func.count(Group.id)))).scalar() or 0
        banned = (await s.execute(
            select(func.count(User.id)).where(User.is_banned.is_(True))
        )).scalar() or 0
        open_req = (await s.execute(
            select(func.count(MovieRequest.id)).where(
                MovieRequest.status == "open"))).scalar() or 0
        jobs = (await s.execute(
            select(func.count(BackfillJob.id)).where(
                BackfillJob.status == "running"))).scalar() or 0
    text = ("📊 <b>Bot stats</b>\n\n"
            f"📦 Files: <b>{files:,}</b> ({_fmt_size(total_bytes)})\n"
            f"👥 Users: <b>{users:,}</b> (🚫 {banned} banned)\n"
            f"👪 Groups: <b>{groups:,}</b>\n"
            f"🎞 Open requests: <b>{open_req}</b>\n"
            f"📥 Running index jobs: <b>{jobs}</b>")
    return text, _back_kb()


def _fmt_size(n) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


async def _users_panel(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    per = 6
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        total = (await s.execute(select(func.count(User.id)))).scalar() or 0
        banned = (await s.execute(
            select(func.count(User.id)).where(User.is_banned.is_(True))
        )).scalar() or 0
        rows = (await s.execute(
            select(User).order_by(User.last_seen.desc().nullslast())
            .offset(page * per).limit(per))).scalars().all()
    lines = [f"👥 <b>Users:</b> {total:,} (🚫 {banned} banned)\n"]
    kb_rows = []
    for u in rows:
        name = ui.esc((u.first_name or "")[:18])
        tag = f"@{ui.esc(u.username)}" if u.username else ""
        flag = "🚫" if u.is_banned else "✅"
        lines.append(f"{flag} {name} {tag} <code>{u.id}</code>")
        toggle = ("adm:unban" if u.is_banned else "adm:ban")
        kb_rows.append([
            InlineKeyboardButton(
                f"{'✅ Unban' if u.is_banned else '🚫 Ban'} {u.id}",
                callback_data=f"{toggle}:{u.id}:{page}"),
            InlineKeyboardButton("⚠️0",
                                 callback_data=f"adm:warnreset:{u.id}:{page}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"adm:users:{page - 1}"))
    if (page + 1) * per < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"adm:users:{page + 1}"))
    nav.append(InlineKeyboardButton("⬅️ Back", callback_data="adm:home"))
    kb_rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


async def _set_ban(uid: int, ban: bool) -> str:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one_or_none()
        if not u:
            return "User not in database."
        u.is_banned = ban
        await s.commit()
    return f"{'🚫 Banned' if ban else '✅ Unbanned'} <code>{uid}</code>."


async def _reset_warns(uid: int) -> None:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one_or_none()
        if u:
            u.warns = 0
            await s.commit()


async def _reqs_panel() -> tuple[str, InlineKeyboardMarkup]:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        rows = (await s.execute(
            select(MovieRequest).where(MovieRequest.status == "open")
            .order_by(MovieRequest.created_at.desc()).limit(6)
        )).scalars().all()
        total = (await s.execute(
            select(func.count(MovieRequest.id)).where(
                MovieRequest.status == "open"))).scalar() or 0
    if not rows:
        return "🎞 <b>No open requests.</b>", _back_kb()
    lines = [f"🎞 <b>Open requests:</b> {total}\n"]
    kb_rows = []
    for r in rows:
        lines.append(f"<b>#{r.id}</b> <code>{r.user_id}</code>: "
                     f"{ui.esc(r.text[:80])}")
        kb_rows.append([
            InlineKeyboardButton(f"✅ #{r.id}",
                                 callback_data=f"reqdone:{r.id}"),
            InlineKeyboardButton(f"❌ #{r.id}",
                                 callback_data=f"reqrej:{r.id}"),
        ])
    kb_rows.append([InlineKeyboardButton("⬅️ Back",
                                         callback_data="adm:home")])
    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


# --- settings panel ---

_DEL_PRESETS = [("Off", 0), ("10m", 600), ("1h", 3600), ("24h", 86400),
                ("7d", 604800)]
_RPP_PRESETS = [5, 10, 15]
_WARN_PRESETS = [2, 3, 5]


def _fmt_dur(sec: int) -> str:
    for label, val in _DEL_PRESETS:
        if val == sec:
            return label
    if sec >= 86400:
        return f"{sec // 86400}d"
    if sec >= 3600:
        return f"{sec // 3600}h"
    return f"{sec}s"


async def _settings_panel() -> tuple[str, InlineKeyboardMarkup]:
    auto_del = int(await rt.aget_setting("AUTO_DELETE_SECONDS") or 0)
    protect = bool(await rt.aget_setting("PROTECT_CONTENT"))
    rpp = int(await rt.aget_setting("RESULTS_PER_PAGE") or 10)
    warn = int(await rt.aget_setting("WARN_LIMIT") or 3)
    jr = bool(await rt.aget_setting("FSUB_JOIN_REQUEST"))
    aa = bool(await rt.aget_setting("FSUB_AUTO_APPROVE"))
    rows = []
    brow = [InlineKeyboardButton(
        f"{'✅' if v == auto_del else ''}{label}",
        callback_data=f"adm:setdel:{v}") for label, v in _DEL_PRESETS]
    rows.append(brow)
    rows.append([
        InlineKeyboardButton(
            f"🛡 Protect content: {'ON' if protect else 'OFF'}",
            callback_data="adm:set:PROTECT_CONTENT")])
    rows.append([InlineKeyboardButton(
        f"{'✅' if v == rpp else ''}{v}/page",
        callback_data=f"adm:setrpp:{v}") for v in _RPP_PRESETS])
    rows.append([InlineKeyboardButton(
        f"{'✅' if v == warn else ''}{v} warns",
        callback_data=f"adm:setwarn:{v}") for v in _WARN_PRESETS])
    rows.append([
        InlineKeyboardButton(f"📩 Join-request links: {'ON' if jr else 'OFF'}",
                             callback_data="adm:set:FSUB_JOIN_REQUEST"),
        InlineKeyboardButton(f"✅ Auto-approve: {'ON' if aa else 'OFF'}",
                             callback_data="adm:set:FSUB_AUTO_APPROVE")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:home")])
    text = ("⚙️ <b>Settings</b>\n\n"
            f"🗑 <b>File delete time:</b> {_fmt_dur(auto_del)}\n"
            f"🛡 <b>Protect content:</b> {'ON' if protect else 'OFF'}\n"
            f"📄 <b>Results/page:</b> {rpp}\n"
            f"⚠️ <b>Warns before ban:</b> {warn}\n"
            f"📩 <b>Join-request links:</b> {'ON' if jr else 'OFF'}\n"
            f"✅ <b>Auto-approve:</b> {'ON' if aa else 'OFF'}\n\n"
            "<i>Advanced settings live in the 🌐 web dashboard.</i>")
    return text, InlineKeyboardMarkup(rows)


async def _fsub_panel() -> tuple[str, InlineKeyboardMarkup]:
    from app.bot import forcesub as _fs
    chans = await _fs.effective_channels()
    jr = bool(await rt.aget_setting("FSUB_JOIN_REQUEST"))
    aa = bool(await rt.aget_setting("FSUB_AUTO_APPROVE"))
    lines = ["📢 <b>Force-sub channels</b>\n"]
    lines += [f"• <code>{ui.esc(c)}</code>" for c in chans] or ["<i>none — off</i>"]
    lines.append(f"\n📩 Join-request links: <b>{'ON' if jr else 'OFF'}</b>")
    lines.append(f"✅ Auto-approve: <b>{'ON' if aa else 'OFF'}</b>")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set channels",
                              callback_data="adm:fsubset"),
         InlineKeyboardButton("🗑 Clear", callback_data="adm:fsubclear")],
        [InlineKeyboardButton("⬅️ Back", callback_data="adm:home")],
    ])
    return "\n".join(lines), kb


# --- cleanup panel ---

def _clean_token(mode: str, param: str) -> str:
    import secrets
    tok = secrets.token_hex(4)
    _clean_tokens[tok] = (mode, param)
    return tok


async def _clean_count(mode: str, param: str) -> tuple[int, str]:
    from sqlalchemy import delete as sa_delete  # noqa: F401 (parity)
    q = select(File)
    label = ""
    if mode == "keyword":
        like = f"%{param}%"
        q = q.where(or_(File.file_name.ilike(like),
                        File.caption.ilike(like)))
        label = f"keyword “{param}”"
    elif mode == "daterange":
        d_from = datetime.strptime(param.split()[0], "%Y-%m-%d")
        d_to = datetime.strptime(param.split()[1], "%Y-%m-%d")
        q = q.where(File.posted_at >= d_from, File.posted_at <= d_to)
        label = f"posted {param}"
    elif mode == "all":
        label = "ALL files"
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        n = (await s.execute(
            select(func.count()).select_from(q.subquery()))).scalar() or 0
    return n, label


async def _clean_exec(mode: str, param: str) -> int:
    from sqlalchemy import delete as sa_delete
    q = select(File)
    if mode == "keyword":
        like = f"%{param}%"
        q = q.where(or_(File.file_name.ilike(like),
                        File.caption.ilike(like)))
    elif mode == "daterange":
        d_from = datetime.strptime(param.split()[0], "%Y-%m-%d")
        d_to = datetime.strptime(param.split()[1], "%Y-%m-%d")
        q = q.where(File.posted_at >= d_from, File.posted_at <= d_to)
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        stmt = sa_delete(File)
        if q.whereclause is not None:
            stmt = stmt.where(q.whereclause)
        res = await s.execute(stmt)
        await s.commit()
        return res.rowcount or 0


def _clean_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 By keyword",
                              callback_data="adm:clean:keyword"),
         InlineKeyboardButton("📅 By date range",
                              callback_data="adm:clean:daterange")],
        [InlineKeyboardButton("💥 Delete ALL files",
                              callback_data="adm:clean:all")],
        [InlineKeyboardButton("⬅️ Back", callback_data="adm:home")],
    ])


# --- broadcast ---

async def _run_broadcast_panel(client: Client, msg: Message, text: str,
                               target: str) -> None:
    """Button-driven broadcast with live progress on the panel message."""
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        ids: list[int] = []
        if target in ("users", "both"):
            ids += (await s.execute(
                select(User.id).where(User.is_banned.is_(False)))).scalars().all()
        if target in ("groups", "both"):
            ids += (await s.execute(select(Group.id))).scalars().all()
    sent = failed = 0
    for uid in ids:
        try:
            await client.send_message(uid, text, parse_mode=ParseMode.HTML)
            sent += 1
        except (UserIsBlocked, PeerIdInvalid):
            failed += 1
        except FloodWait as exc:
            await asyncio.sleep(exc.value + 1)
        except Exception:
            failed += 1
        if (sent + failed) % 50 == 0:
            try:
                await msg.edit_text(
                    f"📢 <b>Broadcasting…</b> {sent + failed:,}/{len(ids):,} "
                    f"(✅{sent} ❌{failed})", parse_mode=ParseMode.HTML)
            except Exception:
                pass
    kb = _back_kb()
    try:
        await msg.edit_text(
            f"📢 <b>Broadcast done.</b>\n✅ {sent:,} sent · ❌ {failed:,} failed.",
            parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        pass


# --- callback router ---

async def _adm_cb(client: Client, query) -> None:
    uid = query.from_user.id if query.from_user else None
    if not settings.is_admin(uid):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    async def _edit(text: str, kb: InlineKeyboardMarkup) -> None:
        try:
            await query.message.edit_text(
                text, reply_markup=kb, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True)
        except Exception:
            pass

    if action == "home":
        _pending.pop(uid, None)
        text, kb = await _home_panel()
        await query.answer()
        await _edit(text, kb)
    elif action == "stats":
        await query.answer()
        text, kb = await _stats_panel()
        await _edit(text, kb)
    elif action == "users":
        await query.answer()
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        text, kb = await _users_panel(page)
        await _edit(text, kb)
    elif action in ("ban", "unban"):
        target = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        note = await _set_ban(target, action == "ban")
        await query.answer(note.replace("<code>", "").replace("</code>", ""),
                           show_alert=False)
        text, kb = await _users_panel(page)
        await _edit(text, kb)
    elif action == "warnreset":
        target = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        await _reset_warns(target)
        await query.answer("⚠️ Warns reset.")
        text, kb = await _users_panel(page)
        await _edit(text, kb)
    elif action == "reqs":
        await query.answer()
        text, kb = await _reqs_panel()
        await _edit(text, kb)
    elif action == "bcast":
        _pending[uid] = ("bcast_text", {})
        await query.answer()
        await _edit("📢 <b>Broadcast</b>\n\nSend me the broadcast text now "
                    "(HTML allowed).\n<i>Send /cancel to abort.</i>",
                    _back_kb())
    elif action == "bcastto":
        target = parts[2] if len(parts) > 2 else "users"
        pend = _pending.pop(uid, None)
        if not pend or pend[0] != "bcast_ready":
            await query.answer("⌛ Expired — start again.", show_alert=True)
            return
        text = pend[1]["text"]
        await query.answer("📢 Broadcasting…")
        await query.message.edit_text(
            f"📢 <b>Broadcasting…</b> 0/{'?'}\n<i>{ui.esc(target)}</i>",
            parse_mode=ParseMode.HTML)
        asyncio.create_task(_run_broadcast_panel(
            client, query.message, text, target))
    elif action == "clean":
        await query.answer()
        if len(parts) > 2:
            mode = parts[2]
            if mode == "all":
                n, label = await _clean_count("all", "")
                tok = _clean_token("all", "")
                await _edit(
                    f"🧹 <b>Delete ALL files?</b>\n\n<b>{n:,}</b> files match.",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            f"✅ Yes, delete {n:,}",
                            callback_data=f"adm:cleango:{tok}")],
                        [InlineKeyboardButton("⬅️ Back",
                                               callback_data="adm:clean")]]))
            else:
                _pending[uid] = (f"clean_{mode}", {})
                hint = ("Send the keyword now." if mode == "keyword"
                        else "Send the date range as "
                             "<code>YYYY-MM-DD YYYY-MM-DD</code>.")
                await _edit(f"🧹 <b>Cleanup — {mode}</b>\n\n{hint}\n"
                            "<i>Send /cancel to abort.</i>", _back_kb())
        else:
            await _edit("🧹 <b>Delete files</b>\n\nPick a mode:",
                        _clean_menu_kb())
    elif action == "cleango":
        tok = parts[2] if len(parts) > 2 else ""
        item = _clean_tokens.pop(tok, None)
        if not item:
            await query.answer("⌛ Expired — start again.", show_alert=True)
            return
        mode, param = item
        await query.answer("🧹 Deleting…")
        n = await _clean_exec(mode, param)
        await _edit(f"🧹 <b>Done.</b> Deleted <b>{n:,}</b> files.",
                    _back_kb())
    elif action == "set":
        await query.answer()
        if len(parts) > 2:
            key = parts[2]
            cur = bool(await rt.aget_setting(key))
            await rt.set_setting(key, "0" if cur else "1")
            await query.answer(f"{'OFF' if cur else 'ON'}")
        text, kb = await _settings_panel()
        await _edit(text, kb)
    elif action == "setdel":
        val = int(parts[2])
        await rt.set_setting("AUTO_DELETE_SECONDS", str(val))
        await query.answer(f"🗑 {_fmt_dur(val)}")
        text, kb = await _settings_panel()
        await _edit(text, kb)
    elif action == "setrpp":
        val = int(parts[2])
        await rt.set_setting("RESULTS_PER_PAGE", str(val))
        await query.answer(f"📄 {val}/page")
        text, kb = await _settings_panel()
        await _edit(text, kb)
    elif action == "setwarn":
        val = int(parts[2])
        await rt.set_setting("WARN_LIMIT", str(val))
        await query.answer(f"⚠️ {val} warns")
        text, kb = await _settings_panel()
        await _edit(text, kb)
    elif action == "fsub":
        await query.answer()
        text, kb = await _fsub_panel()
        await _edit(text, kb)
    elif action == "fsubset":
        _pending[uid] = ("fsub_set", {})
        await query.answer()
        await _edit("📢 <b>Force-sub channels</b>\n\nSend the new list — "
                    "comma separated <code>@usernames</code> or ids.\n"
                    "Send <code>off</code> to disable.\n"
                    "<i>Send /cancel to abort.</i>", _back_kb())
    elif action == "fsubclear":
        await rt.set_setting("FORCE_SUB_CHANNELS", "")
        await query.answer("🗑 Cleared.")
        text, kb = await _fsub_panel()
        await _edit(text, kb)
    elif action == "dbcheck":
        await query.answer()
        text = await _dbcheck_text()
        await _edit(text, _back_kb())
    else:
        await query.answer()


async def _dbcheck_text() -> str:
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
                sa_text("SELECT to_regclass('public.activity_logs')")
            )).scalar()
            lines.append("🗂 activity_logs table: "
                         + ("✅ exists" if exists else "❌ MISSING"))
    except Exception as exc:
        lines.append(f"🗂 activity_logs table: ❌ {exc}")
    return "\n".join(lines)


# --- pending text input (group=-1 so it runs before search) ---

async def _pending_input(client: Client, message: Message) -> None:
    uid = message.from_user.id if message.from_user else None
    if not uid or not settings.is_admin(uid):
        return
    pend = _pending.get(uid)
    if not pend:
        return
    action, data = pend
    text = (message.text or "").strip()
    if text.startswith("/"):
        if text != "/cancel":
            return  # other commands pass through; pending state kept
        _pending.pop(uid, None)
        t, kb = await _home_panel()
        await message.reply_text(t, reply_markup=kb,
                                 parse_mode=ParseMode.HTML,
                                 disable_web_page_preview=True)
        message.stop_propagation()
        return
    if action == "bcast_text":
        if not text:
            return
        _pending[uid] = ("bcast_ready", {"text": text})
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Users",
                                  callback_data="adm:bcastto:users"),
             InlineKeyboardButton("👪 Groups",
                                  callback_data="adm:bcastto:groups")],
            [InlineKeyboardButton("👥+👪 Both",
                                  callback_data="adm:bcastto:both")],
            [InlineKeyboardButton("⬅️ Back", callback_data="adm:home")],
        ])
        await message.reply_text(
            f"📢 <b>Broadcast preview:</b>\n\n{text[:1500]}\n\n"
            "Who should receive it?", reply_markup=kb,
            parse_mode=ParseMode.HTML)
        message.stop_propagation()
        return
    if action == "clean_keyword":
        if not text:
            return
        _pending.pop(uid, None)
        n, label = await _clean_count("keyword", text)
        tok = _clean_token("keyword", text)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Yes, delete {n:,}",
                                  callback_data=f"adm:cleango:{tok}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="adm:clean")]])
        await message.reply_text(
            f"🧹 <b>{n:,}</b> files match {ui.esc(label)}.\nDelete them?",
            reply_markup=kb, parse_mode=ParseMode.HTML)
        message.stop_propagation()
        return
    if action == "clean_daterange":
        _pending.pop(uid, None)
        try:
            d_from, d_to = text.split()[:2]
            datetime.strptime(d_from, "%Y-%m-%d")
            datetime.strptime(d_to, "%Y-%m-%d")
        except (ValueError, IndexError):
            await message.reply_text(
                "❌ Use <code>YYYY-MM-DD YYYY-MM-DD</code>.",
                parse_mode=ParseMode.HTML)
            message.stop_propagation()
            return
        param = f"{d_from} {d_to}"
        n, label = await _clean_count("daterange", param)
        tok = _clean_token("daterange", param)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Yes, delete {n:,}",
                                  callback_data=f"adm:cleango:{tok}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="adm:clean")]])
        await message.reply_text(
            f"🧹 <b>{n:,}</b> files match {ui.esc(label)}.\nDelete them?",
            reply_markup=kb, parse_mode=ParseMode.HTML)
        message.stop_propagation()
        return
    if action == "fsub_set":
        _pending.pop(uid, None)
        if text.lower() == "off":
            await rt.set_setting("FORCE_SUB_CHANNELS", "")
        else:
            await rt.set_setting("FORCE_SUB_CHANNELS", text)
        t, kb = await _fsub_panel()
        await message.reply_text(t, reply_markup=kb,
                                 parse_mode=ParseMode.HTML,
                                 disable_web_page_preview=True)
        message.stop_propagation()
        return


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
        lines.append(f"• {ui.esc(name)} (@{ui.esc(u.username) or '—'}) "
                     f"<code>{u.id}</code>")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


_DEBUG_STARTED_AT = _time.time()


def _debug_server_lines() -> list:
    import os
    import platform
    import sys
    import time as _time
    lines = []
    up = int(_time.time() - _DEBUG_STARTED_AT)
    d, up = divmod(up, 86400)
    h, up = divmod(up, 3600)
    m, s = divmod(up, 60)
    uptime = (f"{d}d " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"
    lines.append(f"⏱ uptime: <b>{uptime}</b>")
    lines.append(f"🐍 python: <code>{platform.python_version()}</code> "
                 f"({platform.system()} {platform.machine()})")
    # RSS memory (Linux)
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    rss = int(ln.split()[1]) // 1024
                    lines.append(f"🧠 memory RSS: <b>{rss} MB</b>")
                    break
    except Exception:  # noqa: BLE001
        pass
    try:
        la1, la5, la15 = os.getloadavg()
        lines.append(f"📊 load avg: <code>{la1:.2f} {la5:.2f} {la15:.2f}</code>")
    except Exception:  # noqa: BLE001
        pass
    # host platform detection (values never shown, only which matched)
    import os as _os
    host = "unknown"
    if _os.environ.get("RENDER"):
        host = "Render"
    elif _os.environ.get("VOROA") or _os.environ.get("VOROA_APP_NAME"):
        host = "Voroa"
    lines.append(f"🏠 host: <b>{host}</b>")
    return lines


async def _debug_db_lines() -> list:
    import time as _time
    from sqlalchemy import text as sa_text
    from app.db import get_engine

    lines = []
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            t0 = _time.perf_counter()
            await s.execute(sa_text("SELECT 1"))
            ping_ms = (_time.perf_counter() - t0) * 1000
        lines.append(f"📡 ping (SELECT 1): <b>{ping_ms:.0f} ms</b>")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"📡 ping: ❌ {exc}")
        return lines
    try:
        async with factory() as s:
            ver = (await s.execute(sa_text("SHOW server_version"))).scalar()
            lines.append(f"🐘 postgres: <code>{ver}</code>")
            size = (await s.execute(sa_text(
                "SELECT pg_size_pretty(pg_database_size("
                "current_database()))"))).scalar()
            n_files = (await s.execute(
                sa_text("SELECT count(*) FROM files"))).scalar()
            lines.append(f"💾 db size: <b>{size}</b> | "
                         f"files: <b>{n_files:,}</b>")
            idx = (await s.execute(sa_text(
                "SELECT count(*) FROM pg_indexes "
                "WHERE schemaname='public' AND tablename='files'"))).scalar()
            trgm = (await s.execute(sa_text(
                "SELECT count(*) FROM pg_indexes "
                "WHERE tablename='files' "
                "AND indexdef LIKE '%gin_trgm_ops%'"))).scalar()
            lines.append(f"🗂 files indexes: <b>{idx}</b> "
                         f"(trigram: <b>{trgm}</b>/3)")
            conns = (await s.execute(sa_text(
                "SELECT count(*), "
                "count(*) FILTER (WHERE state='active') "
                "FROM pg_stat_activity "
                "WHERE datname=current_database()"))).first()
            lines.append(f"🔌 db connections: <b>{conns[1]}</b> active / "
                         f"<b>{conns[0]}</b> total")
            # distinct client IPs talking to this DB (multi-instance check)
            ips = (await s.execute(sa_text(
                "SELECT count(DISTINCT client_addr) FROM pg_stat_activity "
                "WHERE datname=current_database() "
                "AND client_addr IS NOT NULL"))).scalar()
            lines.append(f"🌐 distinct client IPs: <b>{ips}</b>"
                         + (" ⚠️ >1 = another instance may be connected!"
                            if (ips or 0) > 1 else ""))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"🗄 db info: ❌ {exc}")
    # pool stats
    try:
        engine = get_engine(settings.DATABASE_URL)
        pool = engine.pool
        lines.append(f"🏊 pool: <b>{pool.checkedout()}</b> checked out / "
                     f"<b>{pool.size()}</b> size")
    except Exception:  # noqa: BLE001
        pass
    return lines


def _debug_config_lines() -> list:
    import os as _os
    lines = []
    # only SET/missing — never values
    checks = [
        ("BOT_TOKEN", bool(settings.BOT_TOKEN)),
        ("TG_API_ID/HASH", bool(settings.TG_API_ID and settings.TG_API_HASH)),
        ("DATABASE_URL", bool(settings.DATABASE_URL)),
        ("WEB_SECRET", bool(settings.WEB_SECRET and
                             settings.WEB_SECRET != "change-me")),
        ("OPENSUBTITLES_API_KEY", bool(_os.environ.get("OPENSUBTITLES_API_KEY"))),
        ("GROQ_API_KEYS", bool(settings.GROQ_API_KEYS)),
    ]
    bad = [n for n, ok in checks if not ok]
    lines.append("🔑 env: " + ("✅ all set" if not bad
                               else f"❌ missing: {', '.join(bad)}"))
    try:
        from urllib.parse import urlparse
        host = urlparse(settings.DATABASE_URL).hostname or "?"
        # show host only (no user/pass/port)
        lines.append(f"🗄 db host: <code>{host}</code>")
    except Exception:  # noqa: BLE001
        pass
    if settings.WEB_URL:
        lines.append(f"🌍 web url: <code>{settings.WEB_URL}</code>")
    return lines


@admin_only
async def _debug(client: Client, message: Message):
    """Full debug dump: server, DB, latency, config (admin only)."""
    import time as _time
    parts = ["🖥 <b>SERVER</b>"] + _debug_server_lines()
    # telegram latency
    try:
        t0 = _time.perf_counter()
        me = await client.get_me()
        tg_ms = (_time.perf_counter() - t0) * 1000
        parts.append(f"✈️ telegram api: <b>{tg_ms:.0f} ms</b> "
                     f"(@{me.username})")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"✈️ telegram api: ❌ {exc}")
    parts.append("")
    parts.append("🗄 <b>DATABASE</b>")
    parts += await _debug_db_lines()
    parts.append("")
    parts.append("⚙️ <b>CONFIG</b>")
    parts += _debug_config_lines()
    # verdict
    parts.append("")
    await message.reply_text("\n".join(parts), parse_mode=ParseMode.HTML)


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
        if not u:
            # Never seen this user — don't invent a row for them.
            await message.reply_text(
                f"⚠️ User <code>{uid}</code> not in database — nothing to ban.")
            return
        u.is_banned = True
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
    try:
        limit = int(await rt.aget_setting("WARN_LIMIT") or 3)
    except (TypeError, ValueError):
        log.warning("bad WARN_LIMIT setting — defaulting to 3")
        limit = 3
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
            uid, f"⚠️ <b>Warning {warns}/{limit}</b>\n"
            f"Reason: {ui.esc(reason)}\n"
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


async def _settings(client: Client, message: Message):
    # No @admin_only here on purpose: the function itself routes —
    # admins get the dashboard link, regular users get personal settings.
    uid = message.from_user.id if message.from_user else None
    if not settings.is_admin(uid):
        # v6: regular users get their personal AI/taste settings.
        from app import personalize
        prefs = await personalize.get_prefs(uid)
        await message.reply_text(
            ui.user_settings_text(prefs["enabled"], prefs["downloads"]),
            reply_markup=ui.user_settings_kb(prefs["enabled"]),
            parse_mode=ParseMode.HTML)
        return
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
    bot.on_message(filters.private & filters.command("admin"))(_admin)
    bot.on_message(filters.private & filters.command("stats"))(_stats)
    bot.on_message(filters.private & filters.command("users"))(_users)
    bot.on_message(filters.private & filters.command("ban"))(_ban)
    bot.on_message(filters.private & filters.command("unban"))(_unban)
    bot.on_message(filters.private & filters.command("warn"))(_warn)
    bot.on_message(filters.private & filters.command("broadcast"))(_broadcast)
    bot.on_message(filters.private & filters.command("requests"))(_requests)
    bot.on_message(filters.private & filters.command("settings"))(_settings)
    bot.on_message(filters.private & filters.command("dbcheck"))(_dbcheck)
    bot.on_message(filters.private & filters.command("debug"))(_debug)
    bot.on_callback_query(filters.regex(r"^req(done|rej):"))(_req_action)
    # v10.8.10: button dashboard.
    bot.on_callback_query(filters.regex(r"^adm:"))(_adm_cb)
    # v10.8.10: pending admin text input — group=-1 so it runs BEFORE
    # the search text handler and can swallow the message.
    bot.on_message(filters.private & filters.text, group=-1)(_pending_input)
