"""Admin dashboard — /admin.

Password-protected web UI (ADMIN_PASSWORD env) for everything an admin
needs without touching Telegram commands:

* homepage — daily / weekly / monthly analytics
* users — ban, unban, warn, PM message
* broadcast — to users, groups, or both
* files — search, view details, delete
* requests — approve / not available / already uploaded / notify user
* cleanup — delete files: all, by keyword, by date range
* settings — every manageable option
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from datetime import datetime, timezone
from html import escape as _esc

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import URLSafeSerializer
from sqlalchemy import func, or_, select

from app import analytics
from app.bot import app as bot_app
from app.config import settings
from app.db import get_session_factory
from app.models import (ActivityLog, EventLog, File, Group, MovieRequest,
                        SearchLog, User)
from app import runtime as rt

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

COOKIE = "mx_admin"


def _signer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.WEB_SECRET or "change-me", salt="mx-admin")


def _authed(request: Request) -> bool:
    if not settings.ADMIN_PASSWORD:
        return False
    token = request.cookies.get(COOKIE)
    if not token:
        return False
    try:
        return _signer().loads(token) == "ok"
    except Exception:
        return False


def _need_auth(request: Request):
    if not _authed(request):
        raise HTTPException(307, headers={"Location": "/admin/login"})


def esc(x) -> str:
    return _esc(str(x if x is not None else ""))


# ---------- layout ----------

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f1420;color:#e8ecf4;font-family:system-ui,-apple-system,sans-serif;font-size:14px}
a{color:#7ab8ff;text-decoration:none}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
nav{background:#161d2e;border-bottom:1px solid #2a3550;padding:10px 16px;display:flex;gap:14px;flex-wrap:wrap;align-items:center;position:sticky;top:0;z-index:10}
nav a{padding:6px 10px;border-radius:8px;color:#c6d2e8}
nav a.on,nav a:hover{background:#24304d;color:#fff}
nav .brand{font-weight:700;color:#fff;margin-right:auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}
.card{background:#161d2e;border:1px solid #2a3550;border-radius:12px;padding:14px}
.card .n{font-size:24px;font-weight:700}
.card .l{color:#8fa0bd;font-size:12px;margin-top:4px}
h2{margin:18px 0 10px;font-size:18px}
h3{margin:14px 0 8px;font-size:15px;color:#aebdd8}
table{width:100%;border-collapse:collapse;background:#161d2e;border-radius:12px;overflow:hidden;margin:10px 0}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #232c44;font-size:13px}
th{color:#8fa0bd;font-weight:600;background:#131a2b}
tr:hover td{background:#1a2236}
.btn{display:inline-block;background:#2f6fed;color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:13px}
.btn.red{background:#c0392b}.btn.green{background:#1e9e5a}.btn.grey{background:#3a4663}
.btn.sm{padding:5px 10px;font-size:12px}
form.inline{display:inline}
input[type=text],input[type=number],input[type=password],select,textarea{background:#0d1322;border:1px solid #2a3550;color:#e8ecf4;border-radius:8px;padding:9px 12px;font-size:14px;width:100%}
textarea{min-height:90px}
label{display:block;margin:12px 0 4px;color:#aebdd8;font-weight:600}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
.row>div{flex:1;min-width:180px}
.note{color:#8fa0bd;font-size:12px;margin-top:4px}
.alert{background:#2a1f1f;border:1px solid #c0392b;border-radius:10px;padding:12px;margin:12px 0}
.okmsg{background:#14261c;border:1px solid #1e9e5a;border-radius:10px;padding:12px;margin:12px 0}
.login{max-width:380px;margin:80px auto;background:#161d2e;padding:28px;border-radius:14px;border:1px solid #2a3550}
.pager{margin:12px 0;display:flex;gap:8px}
.mut{color:#8fa0bd}
"""


def layout(title: str, body: str, active: str = "") -> str:
    def nav(href: str, label: str, key: str) -> str:
        cls = "on" if active == key else ""
        return f'<a href="/admin{href}" class="{cls}">{label}</a>'
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · Moovidex Admin</title><style>{_CSS}</style></head>
<body><nav><span class="brand">🎛 Moovidex Admin</span>
{nav("/", "📊 Dashboard", "dash")}
{nav("/activity", "📝 Activity", "act")}
{nav("/users", "👥 Users", "users")}
{nav("/broadcast", "📢 Broadcast", "bcast")}
{nav("/files", "📦 Files", "files")}
{nav("/requests", "🎞 Requests", "req")}
{nav("/cleanup", "🧹 Cleanup", "clean")}
{nav("/settings", "⚙️ Settings", "set")}
<a href="/admin/logout">🚪 Logout</a></nav>
<div class="wrap">{body}</div></body></html>"""


def page(title: str, body: str, active: str = "") -> HTMLResponse:
    return HTMLResponse(layout(title, body, active))


# ---------- auth ----------

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if _authed(request):
        return RedirectResponse("/admin/", status_code=303)
    if not settings.ADMIN_PASSWORD:
        body = ('<div class="login"><h2>🔒 Admin dashboard</h2>'
                '<div class="alert">ADMIN_PASSWORD is not set on the server. '
                "Set it in the environment and redeploy.</div></div>")
        return HTMLResponse(layout("Login", body))
    body = ('<div class="login"><h2>🔒 Admin login</h2>'
            '<form method="post"><label>Password</label>'
            '<input type="password" name="password" autofocus>'
            '<div style="margin-top:14px">'
            '<button class="btn" type="submit">Login</button></div>'
            "</form></div>")
    return HTMLResponse(layout("Login", body))


@router.post("/login")
async def login(request: Request, password: str = Form("")):
    # Constant-time comparison (no early-exit timing leak).
    ok = bool(settings.ADMIN_PASSWORD) and hmac.compare_digest(
        password.encode("utf-8"),
        settings.ADMIN_PASSWORD.encode("utf-8"))
    if ok:
        resp = RedirectResponse("/admin/", status_code=303)
        resp.set_cookie(COOKIE, _signer().dumps("ok"), httponly=True,
                        max_age=86400 * 7, samesite="lax", path="/admin")
        return resp
    body = ('<div class="login"><h2>🔒 Admin login</h2>'
            '<div class="alert">Wrong password.</div>'
            '<form method="post"><label>Password</label>'
            '<input type="password" name="password" autofocus>'
            '<div style="margin-top:14px">'
            '<button class="btn" type="submit">Login</button></div>'
            "</form></div>")
    return HTMLResponse(layout("Login", body), status_code=401)


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/admin")
    return resp


# ---------- dashboard homepage ----------

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    _need_auth(request)

    data = await analytics.overview(30)
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        n_files = (await s.execute(select(func.count(File.id)))).scalar() or 0
        n_users = (await s.execute(select(func.count(User.id)))).scalar() or 0
        n_groups = (await s.execute(select(func.count(Group.id)))).scalar() or 0
        n_open = (await s.execute(
            select(func.count(MovieRequest.id)).where(
                MovieRequest.status == "open"))).scalar() or 0
        n_banned = (await s.execute(
            select(func.count(User.id)).where(User.is_banned.is_(True))
        )).scalar() or 0
        # v10.8.10: total file size.
        total_bytes = (await s.execute(
            select(func.coalesce(func.sum(File.file_size), 0)))).scalar() or 0
        # v10.8.10: request fulfilment stats.
        req_total = (await s.execute(
            select(func.count(MovieRequest.id)))).scalar() or 0
        req_done = (await s.execute(
            select(func.count(MovieRequest.id)).where(
                MovieRequest.status == "done"))).scalar() or 0
        # today's requests -> per-user fulfilment ratio, averaged.
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        today_reqs = (await s.execute(
            select(MovieRequest.user_id, MovieRequest.status).where(
                MovieRequest.created_at >= today_start))).all()
        per_user: dict[int, list[int]] = {}
        for uid_, st in today_reqs:
            d, t = per_user.get(uid_, [0, 0])
            t += 1
            if st == "done":
                d += 1
            per_user[uid_] = [d, t]
        if per_user:
            avg_fulfil = (sum(d / t for d, t in per_user.values())
                          / len(per_user) * 100)
            fulfil_txt = f"{avg_fulfil:.0f}%"
        else:
            fulfil_txt = "—"
        # v10.8.10: file delivery rate today (downloads / searches).
        n_search_today = (await s.execute(
            select(func.count(EventLog.id)).where(
                EventLog.kind == "search",
                EventLog.created_at >= today_start))).scalar() or 0
        n_dl_today = (await s.execute(
            select(func.count(EventLog.id)).where(
                EventLog.kind == "download",
                EventLog.created_at >= today_start))).scalar() or 0
        if n_search_today:
            dl_rate = min(100.0, n_dl_today / n_search_today * 100)
            dl_txt = f"{dl_rate:.0f}%"
        else:
            dl_txt = "—"

    unfulfilled = req_total - req_done
    unf_pct = (unfulfilled / req_total * 100) if req_total else 0
    cards = "".join([
        f'<div class="card"><div class="n">{n_files:,}</div><div class="l">📦 Files</div></div>',
        f'<div class="card"><div class="n">{_fmt_size(total_bytes)}</div><div class="l">💾 Total file size</div></div>',
        f'<div class="card"><div class="n">{n_users:,}</div><div class="l">👥 Users</div></div>',
        f'<div class="card"><div class="n">{n_groups:,}</div><div class="l">👪 Groups</div></div>',
        f'<div class="card"><div class="n">{n_open}</div><div class="l">🎞 Open requests</div></div>',
        f'<div class="card"><div class="n">{n_banned:,}</div><div class="l">🚫 Banned</div></div>',
    ])

    req_stats = (
        "<h2>🎞 Requests</h2><table>"
        "<tr><th>Total</th><th>✅ Fulfilled</th><th>❌ Unfulfilled</th>"
        "<th>Unfulfilled %</th><th>Avg user fulfilment (today)</th></tr>"
        f"<tr><td><b>{req_total:,}</b></td><td>{req_done:,}</td>"
        f"<td>{unfulfilled:,}</td><td>{unf_pct:.1f}%</td>"
        f"<td><b>{fulfil_txt}</b></td></tr></table>"
        "<h2>📥 Delivery</h2><table>"
        "<tr><th>Searches today</th><th>Deliveries today</th>"
        "<th>Success delivery rate (today)</th></tr>"
        f"<tr><td><b>{n_search_today:,}</b></td><td>{n_dl_today:,}</td>"
        f"<td><b>{dl_txt}</b></td></tr></table>")

    def period_row(label: str, vals: dict) -> str:
        return ("<tr><td><b>" + esc(label) + "</b></td>" +
                "".join(f"<td>{vals[k]:,}</td>" for k in
                        ("searches", "downloads", "starts", "new_users",
                         "new_files", "new_requests")) + "</tr>")

    t = data["totals"]
    summary = ("""<h2>📈 Activity</h2><table><tr><th>Period</th><th>🔍 Searches</th>
<th>📥 Downloads</th><th>▶️ Starts</th><th>👤 New users</th><th>📦 New files</th>
<th>🎞 Requests</th></tr>"""
               + period_row("Today", t["today"])
               + period_row("Last 7 days", t["week"])
               + period_row("Last 30 days", t["month"]) + "</table>")

    daily = data["daily"]
    days = sorted(daily["searches"])[-14:]
    rows = ""
    for d in reversed(days):
        rows += ("<tr><td><b>" + esc(d) + "</b></td>" +
                 "".join(f"<td>{daily[k][d]:,}</td>" for k in
                         ("searches", "downloads", "starts", "new_users",
                          "new_files", "new_requests")) + "</tr>")
    detail = ("<h2>📅 Daily — last 14 days</h2><table><tr><th>Date</th>"
              "<th>🔍</th><th>📥</th><th>▶️</th><th>👤</th><th>📦</th><th>🎞</th>"
              "</tr>" + rows + "</table>")

    body = (f"<h2>📊 Dashboard</h2><div class='cards'>{cards}</div>"
            f"{req_stats}{summary}{detail}")
    return page("Dashboard", body, "dash")


# ---------- activity log ----------

ACT_KINDS = ("search_pm", "search_group", "ai", "request", "download",
             "start")
ACT_ICONS = {"search_pm": "🔍", "search_group": "👪", "ai": "🤖",
             "request": "🎞", "download": "📥", "start": "▶️"}


@router.get("/activity", response_class=HTMLResponse)
async def activity(request: Request, kind: str = "", q: str = "",
                   page_num: int = 1):
    _need_auth(request)
    # v10.8.10: lazy retention — prune on every view (the daily worker
    # is the backstop). Log channel keeps the permanent copy.
    asyncio.create_task(analytics.prune_activity_logs())
    per = 30
    page_num = max(1, page_num)
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        base = select(ActivityLog)
        if kind in ACT_KINDS:
            base = base.where(ActivityLog.kind == kind)
        if q and q.lstrip("-").isdigit():
            base = base.where(ActivityLog.user_id == int(q))
        total = (await s.execute(
            select(func.count()).select_from(base.subquery()))).scalar() or 0
        rows = (await s.execute(
            base.order_by(ActivityLog.id.desc())
            .offset((page_num - 1) * per).limit(per))).scalars().all()

    tabs = (f"<a class='btn sm {'green' if not kind else 'grey'}' "
            f"href='/admin/activity'>all</a> ")
    tabs += " ".join(
        f"<a class='btn sm {'green' if kind == k else 'grey'}' "
        f"href='/admin/activity?kind={k}'>{ACT_ICONS.get(k, '')} {k}</a>"
        for k in ACT_KINDS)
    trs = ""
    for r in rows:
        trs += ("<tr><td class='mut'>" +
                esc(str(r.created_at)[:19] if r.created_at else "—") +
                "</td><td>" + ACT_ICONS.get(r.kind, "📝") + " " +
                esc(r.kind) + "</td>"
                f"<td><code>{r.user_id or '—'}</code></td>"
                f"<td>{esc((r.detail or '')[:120])}</td></tr>")
    pages = max(1, (total + per - 1) // per)
    pager = (f"<div class='pager'><span class='mut'>Page {page_num}/{pages} "
             f"· {total:,} events (30-day retention)</span>")
    if page_num > 1:
        pager += (f" <a class='btn sm grey' href='/admin/activity?kind={esc(kind)}"
                  f"&q={esc(q)}&page_num={page_num - 1}'>← Prev</a>")
    if page_num < pages:
        pager += (f" <a class='btn sm grey' href='/admin/activity?kind={esc(kind)}"
                  f"&q={esc(q)}&page_num={page_num + 1}'>Next →</a>")
    pager += "</div>"
    body = (f"<h2>📝 Activity log</h2><div class='pager'>{tabs}</div>"
            "<form method='get'><div class='row'><div>"
            "<input type='text' name='q' placeholder='Filter by user id…' "
            f"value='{esc(q)}'>"
            + (f"<input type='hidden' name='kind' value='{esc(kind)}'>"
               if kind else "") +
            "</div><div style='flex:0'>"
            "<button class='btn' type='submit'>Filter</button></div></div></form>"
            f"{pager}<table><tr><th>Time</th><th>Event</th><th>User</th>"
            "<th>Detail</th></tr>"
            + (trs or "<tr><td colspan=4 class='mut'>No events yet.</td></tr>")
            + "</table>" + pager)
    return page("Activity", body, "act")

# ---------- users ----------

@router.get("/users", response_class=HTMLResponse)
async def users(request: Request, q: str = "", page_num: int = 1,
                msg: str = ""):
    _need_auth(request)
    per = 20
    page_num = max(1, page_num)
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        base = select(User)
        if q:
            like = f"%{q}%"
            if q.lstrip("-").isdigit():
                base = base.where(or_(User.id == int(q),
                                      User.username.ilike(like),
                                      User.first_name.ilike(like)))
            else:
                base = base.where(or_(User.username.ilike(like),
                                      User.first_name.ilike(like)))
        total = (await s.execute(
            select(func.count()).select_from(base.subquery()))).scalar() or 0
        rows = (await s.execute(
            base.order_by(User.last_seen.desc().nullslast())
            .offset((page_num - 1) * per).limit(per))).scalars().all()

    banner = f'<div class="okmsg">{esc(msg)}</div>' if msg else ""
    trs = ""
    for u in rows:
        status = "🚫 banned" if u.is_banned else "✅"
        trs += ("<tr><td><code>" + str(u.id) + "</code></td>"
                f"<td>{esc(u.first_name or '')}</td>"
                f"<td>{'@' + esc(u.username) if u.username else '<span class=mut>—</span>'}</td>"
                f"<td>{status}</td><td>⚠️ {u.warns or 0}</td>"
                f"<td class='mut'>{esc(str(u.last_seen)[:16] if u.last_seen else '—')}</td>"
                "<td>"
                f"<form class='inline' method='post' action='/admin/users/{u.id}/action'>"
                "<input type='hidden' name='action' value='ban'>"
                "<button class='btn sm red' type='submit'>Ban</button></form> "
                f"<form class='inline' method='post' action='/admin/users/{u.id}/action'>"
                "<input type='hidden' name='action' value='unban'>"
                "<button class='btn sm green' type='submit'>Unban</button></form> "
                f"<form class='inline' method='post' action='/admin/users/{u.id}/action'>"
                "<input type='hidden' name='action' value='warn_reset'>"
                "<button class='btn sm grey' type='submit'>Reset warns</button></form> "
                f"<form class='inline' method='post' action='/admin/users/{u.id}/action' "
                "onsubmit=\"var m=prompt('Message to user:');if(!m)return false;"
                "this.message.value=m;return true;\">"
                "<input type='hidden' name='action' value='message'>"
                "<input type='hidden' name='message' value=''>"
                "<button class='btn sm' type='submit'>✉️ PM</button></form>"
                "</td></tr>")
    pages = max(1, (total + per - 1) // per)
    pager = (f"<div class='pager'><span class='mut'>Page {page_num}/{pages} "
             f"· {total:,} users</span>")
    if page_num > 1:
        pager += f" <a class='btn sm grey' href='/admin/users?q={esc(q)}&page_num={page_num - 1}'>← Prev</a>"
    if page_num < pages:
        pager += f" <a class='btn sm grey' href='/admin/users?q={esc(q)}&page_num={page_num + 1}'>Next →</a>"
    pager += "</div>"
    body = (f"<h2>👥 Users</h2>{banner}"
            "<form method='get'><div class='row'><div>"
            "<input type='text' name='q' placeholder='Search id / username / name…' "
            f"value='{esc(q)}'></div><div style='flex:0'>"
            "<button class='btn' type='submit'>Search</button></div></div></form>"
            f"{pager}<table><tr><th>ID</th><th>Name</th><th>Username</th>"
            "<th>Status</th><th>Warns</th><th>Last seen</th><th>Actions</th></tr>"
            + (trs or "<tr><td colspan=7 class='mut'>No users found.</td></tr>")
            + "</table>" + pager)
    return page("Users", body, "users")


@router.post("/users/{uid}/action")
async def user_action(request: Request, uid: int,
                      action: str = Form(""),
                      message: str = Form("")):
    _need_auth(request)
    factory = get_session_factory(settings.DATABASE_URL)
    note = ""
    async with factory() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one_or_none()
        if not u:
            u = User(id=uid, warns=0)
            s.add(u)
        if action == "ban":
            u.is_banned = True
            note = f"Banned {uid}."
        elif action == "unban":
            u.is_banned = False
            note = f"Unbanned {uid}."
        elif action == "warn_reset":
            u.warns = 0
            note = f"Warns reset for {uid}."
        elif action == "message" and message.strip():
            client = bot_app.bot
            if client:
                try:
                    await client.send_message(uid, message.strip())
                    note = f"Message sent to {uid}."
                except Exception as exc:
                    note = f"Failed to message {uid}: {exc}"
            else:
                note = "Bot not ready."
        await s.commit()
    return RedirectResponse(f"/admin/users?msg={note}", status_code=303)


# ---------- broadcast ----------

_bcast = {"running": False, "total": 0, "sent": 0, "failed": 0,
          "target": "", "done": True}


async def _run_broadcast(text: str, target: str) -> None:
    _bcast.update(running=True, done=False, sent=0, failed=0,
                  total=0, target=target)
    client = bot_app.bot
    factory = get_session_factory(settings.DATABASE_URL)
    from pyrogram.errors import FloodWait, PeerIdInvalid, UserIsBlocked
    try:
        async with factory() as s:
            ids: list[int] = []
            if target in ("users", "both"):
                ids += (await s.execute(
                    select(User.id).where(User.is_banned.is_(False))
                )).scalars().all()
            if target in ("groups", "both"):
                ids += (await s.execute(select(Group.id))).scalars().all()
        _bcast["total"] = len(ids)
        if not client:
            _bcast["failed"] = len(ids)
            return
        sem = asyncio.Semaphore(6)

        async def _send(uid: int) -> None:
            async with sem:
                try:
                    await client.send_message(uid, text)
                except (UserIsBlocked, PeerIdInvalid):
                    _bcast["failed"] += 1
                    return
                except FloodWait as exc:
                    # one retry after the mandated wait, then give up
                    await asyncio.sleep(exc.value + 1)
                    try:
                        await client.send_message(uid, text)
                    except Exception:
                        _bcast["failed"] += 1
                        return
                except Exception:
                    _bcast["failed"] += 1
                    return
                _bcast["sent"] += 1

        await asyncio.gather(*(_send(uid) for uid in ids))
    finally:
        _bcast.update(running=False, done=True)


@router.get("/broadcast", response_class=HTMLResponse)
async def bcast_form(request: Request, msg: str = ""):
    _need_auth(request)
    banner = f'<div class="okmsg">{esc(msg)}</div>' if msg else ""
    status = ""
    if _bcast["running"] or not _bcast["done"]:
        status = (f"<div class='card'><b>📢 In progress:</b> "
                  f"{_bcast['sent'] + _bcast['failed']:,}/{_bcast['total']:,} "
                  f"(✅ {_bcast['sent']:,} ❌ {_bcast['failed']:,}) — "
                  "this page auto-refreshes.</div>"
                  "<script>setTimeout(()=>location.reload(),3000)</script>")
    body = (f"<h2>📢 Broadcast</h2>{banner}{status}"
            "<form method='post'><label>Message (HTML allowed)</label>"
            "<textarea name='text' required></textarea>"
            "<label>Target</label><select name='target'>"
            "<option value='users'>👥 All users (PM)</option>"
            "<option value='groups'>👪 All groups</option>"
            "<option value='both'>👥+👪 Users and groups</option>"
            "</select><div style='margin-top:14px'>"
            "<button class='btn' type='submit'>Send broadcast</button></div>"
            "</form>")
    return page("Broadcast", body, "bcast")


@router.post("/broadcast")
async def bcast_send(request: Request, text: str = Form(""),
                     target: str = Form("users")):
    _need_auth(request)
    if _bcast["running"]:
        return RedirectResponse("/admin/broadcast?msg=Already running",
                                status_code=303)
    if target not in ("users", "groups", "both"):
        target = "users"
    asyncio.create_task(_run_broadcast(text.strip(), target))
    return RedirectResponse("/admin/broadcast?msg=Broadcast started",
                            status_code=303)

# ---------- files ----------

def _fmt_size(n) -> str:
    if not n:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@router.get("/files", response_class=HTMLResponse)
async def files(request: Request, q: str = "", page_num: int = 1,
                msg: str = "", sort: str = "newest"):
    _need_auth(request)
    per = 20
    page_num = max(1, page_num)
    # v10.8.10: sortable columns.
    sorts = {
        "newest": (File.created_at.desc(), "🕘 Newest"),
        "oldest": (File.created_at.asc(), "🕘 Oldest"),
        "name_az": (File.file_name.asc(), "🔤 Name A–Z"),
        "name_za": (File.file_name.desc(), "🔤 Name Z–A"),
        "size_desc": (File.file_size.desc().nullslast(), "💾 Biggest"),
        "size_asc": (File.file_size.asc().nullslast(), "💾 Smallest"),
        "dl_desc": (File.downloads.desc(), "📥 Most downloaded"),
    }
    order, _label = sorts.get(sort, sorts["newest"])
    sort = sort if sort in sorts else "newest"
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        base = select(File)
        if q:
            like = f"%{q}%"
            base = base.where(or_(File.file_name.ilike(like),
                                  File.caption.ilike(like)))
        total = (await s.execute(
            select(func.count()).select_from(base.subquery()))).scalar() or 0
        rows = (await s.execute(
            base.order_by(order)
            .offset((page_num - 1) * per).limit(per))).scalars().all()

    banner = f'<div class="okmsg">{esc(msg)}</div>' if msg else ""
    trs = ""
    for f in rows:
        name = esc((f.file_name or "—")[:60])
        trs += ("<tr><td><a href='/admin/files/" + str(f.id) + "'>" + name + "</a></td>"
                f"<td>{_fmt_size(f.file_size)}</td>"
                f"<td>{esc(f.quality or '—')}</td>"
                f"<td>{esc(f.language or '—')}</td>"
                f"<td class='mut'>{esc(str(f.posted_at)[:10] if f.posted_at else '—')}</td>"
                "<td>"
                f"<form class='inline' method='post' action='/admin/files/{f.id}/delete' "
                "onsubmit=\"return confirm('Delete this file from the index?')\">"
                "<button class='btn sm red' type='submit'>Delete</button></form>"
                "</td></tr>")
    pages = max(1, (total + per - 1) // per)
    pager = (f"<div class='pager'><span class='mut'>Page {page_num}/{pages} "
             f"· {total:,} files</span>")
    if page_num > 1:
        pager += f" <a class='btn sm grey' href='/admin/files?q={esc(q)}&sort={sort}&page_num={page_num - 1}'>← Prev</a>"
    if page_num < pages:
        pager += f" <a class='btn sm grey' href='/admin/files?q={esc(q)}&sort={sort}&page_num={page_num + 1}'>Next →</a>"
    pager += "</div>"
    sort_opts = "".join(
        f"<option value='{k}' {'selected' if k == sort else ''}>{v[1]}</option>"
        for k, v in sorts.items())
    body = (f"<h2>📦 Files</h2>{banner}"
            "<form method='get'><div class='row'><div>"
            "<input type='text' name='q' placeholder='Search filename / caption…' "
            f"value='{esc(q)}'></div><div>"
            f"<select name='sort' onchange='this.form.submit()'>{sort_opts}</select>"
            "</div><div style='flex:0'>"
            "<button class='btn' type='submit'>Search</button></div></div></form>"
            f"{pager}<table><tr><th>Name</th><th>Size</th><th>Quality</th>"
            "<th>Lang</th><th>Posted</th><th></th></tr>"
            + (trs or "<tr><td colspan=6 class='mut'>No files found.</td></tr>")
            + "</table>" + pager)
    return page("Files", body, "files")


@router.get("/files/{fid}", response_class=HTMLResponse)
async def file_detail(request: Request, fid: int):
    _need_auth(request)
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        f = (await s.execute(select(File).where(File.id == fid))).scalar_one_or_none()
    if not f:
        return page("File", "<div class='alert'>File not found.</div>", "files")
    rows = "".join(
        f"<tr><th style='width:180px'>{k}</th><td>{esc(v)}</td></tr>"
        for k, v in [
            ("ID", f.id), ("Name", f.file_name), ("Size", _fmt_size(f.file_size)),
            ("MIME", f.mime_type), ("Quality", f.quality),
            ("Language", f.language),
            ("Channel ID", f.channel_id), ("Message ID", f.message_id),
            ("Views", f.views), ("Forwards", f.forwards),
            ("Posted at", f.posted_at), ("Indexed at", f.created_at),
            ("Caption", (f.caption or "")[:500]),
        ])
    body = (f"<h2>📦 File #{f.id}</h2><table>{rows}</table>"
            f"<form method='post' action='/admin/files/{f.id}/delete' "
            "onsubmit=\"return confirm('Delete this file from the index?')\">"
            "<button class='btn red' type='submit'>🗑 Delete from index</button></form> "
            "<a class='btn grey' href='/admin/files'>⬅️ Back</a>")
    return page("File detail", body, "files")


@router.post("/files/{fid}/delete")
async def file_delete(request: Request, fid: int):
    _need_auth(request)
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        f = (await s.execute(select(File).where(File.id == fid))).scalar_one_or_none()
        if f:
            await s.delete(f)
            await s.commit()
            note = f"Deleted file #{fid}."
        else:
            note = "File not found."
    return RedirectResponse(f"/admin/files?msg={note}", status_code=303)


# ---------- requests ----------

REQ_ACTIONS = {
    "approve": ("done", "✅ Your request <i>{t}</i> was <b>fulfilled</b> — try searching for it now!"),
    "not_available": ("not_available", "❌ Your request <i>{t}</i> is <b>not available</b> right now."),
    "already_uploaded": ("already_uploaded", "ℹ️ Your request <i>{t}</i> is <b>already uploaded</b> — try searching for it!"),
}


@router.get("/requests", response_class=HTMLResponse)
async def requests_page(request: Request, status: str = "open",
                        msg: str = ""):
    _need_auth(request)
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        q = select(MovieRequest).order_by(MovieRequest.created_at.desc())
        if status != "all":
            q = q.where(MovieRequest.status == status)
        rows = (await s.execute(q.limit(100))).scalars().all()
        counts = {}
        for st, n in (await s.execute(
                select(MovieRequest.status, func.count())
                .group_by(MovieRequest.status))).all():
            counts[st] = n

    banner = f'<div class="okmsg">{esc(msg)}</div>' if msg else ""
    tabs = " ".join(
        f"<a class='btn sm {'green' if status == s else 'grey'}' href='/admin/requests?status={s}'>"
        f"{s} ({counts.get(s, 0)})</a>"
        for s in ("open", "done", "not_available", "already_uploaded",
                  "rejected", "all"))
    trs = ""
    for r in rows:
        trs += ("<tr><td><b>#" + str(r.id) + "</b></td>"
                f"<td>{esc(r.text[:200])}</td>"
                f"<td><code>{r.user_id}</code></td>"
                f"<td>{esc(r.status)}</td>"
                f"<td class='mut'>{esc(str(r.created_at)[:16])}</td><td>")
        if r.status == "open":
            trs += (f"<form class='inline' method='post' action='/admin/requests/{r.id}'>"
                    "<input type='hidden' name='action' value='approve'>"
                    "<button class='btn sm green' type='submit'>✅ Approve</button></form> "
                    f"<form class='inline' method='post' action='/admin/requests/{r.id}'>"
                    "<input type='hidden' name='action' value='not_available'>"
                    "<button class='btn sm red' type='submit'>Not available</button></form> "
                    f"<form class='inline' method='post' action='/admin/requests/{r.id}'>"
                    "<input type='hidden' name='action' value='already_uploaded'>"
                    "<button class='btn sm grey' type='submit'>Already uploaded</button></form> "
                    f"<form class='inline' method='post' action='/admin/requests/{r.id}' "
                    "onsubmit=\"var m=prompt('Custom message to user:');if(!m)return false;"
                    "this.note.value=m;return true;\">"
                    "<input type='hidden' name='action' value='notify'>"
                    "<input type='hidden' name='note' value=''>"
                    "<button class='btn sm' type='submit'>✉️ Notify</button></form>")
        trs += "</td></tr>"
    body = (f"<h2>🎞 Requests</h2>{banner}<div class='pager'>{tabs}</div>"
            "<table><tr><th>ID</th><th>Request</th><th>User</th><th>Status</th>"
            "<th>At</th><th>Actions</th></tr>"
            + (trs or "<tr><td colspan=6 class='mut'>Nothing here.</td></tr>")
            + "</table>")
    return page("Requests", body, "req")


@router.post("/requests/{rid}")
async def request_action(request: Request, rid: int,
                         action: str = Form(""), note: str = Form("")):
    _need_auth(request)
    factory = get_session_factory(settings.DATABASE_URL)
    msg = ""
    async with factory() as s:
        r = (await s.execute(
            select(MovieRequest).where(MovieRequest.id == rid))).scalar_one_or_none()
        if not r:
            return RedirectResponse("/admin/requests?msg=Not found",
                                    status_code=303)
        if action in REQ_ACTIONS:
            new_status, tpl = REQ_ACTIONS[action]
            r.status = new_status
            user_text = tpl.format(t=esc(r.text[:120]))
        elif action == "notify" and note.strip():
            user_text = note.strip()
        else:
            return RedirectResponse("/admin/requests?msg=No action",
                                    status_code=303)
        user_id = r.user_id
        await s.commit()
    client = bot_app.bot
    if client and user_id:
        try:
            await client.send_message(user_id, f"🎞 {user_text}")
            msg = f"Request #{rid} updated + user notified."
        except Exception as exc:
            msg = f"Request #{rid} updated, notify failed: {exc}"
    else:
        msg = f"Request #{rid} updated."
    return RedirectResponse(f"/admin/requests?msg={msg}", status_code=303)

# ---------- cleanup: delete files ----------

@router.get("/cleanup", response_class=HTMLResponse)
async def cleanup_form(request: Request, msg: str = ""):
    _need_auth(request)
    banner = f'<div class="okmsg">{esc(msg)}</div>' if msg else ""
    body = (f"<h2>🧹 Delete files</h2>{banner}"
            "<div class='card'><h3>🔑 By keyword</h3>"
            "<form method='post'><input type='hidden' name='mode' value='keyword'>"
            "<label>Keyword (matches filename or caption)</label>"
            "<input type='text' name='keyword' required>"
            "<div class='note'>First shows how many match — then confirm.</div>"
            "<div style='margin-top:10px'><button class='btn red' type='submit'>"
            "Preview delete</button></div></form></div>"
            "<div class='card' style='margin-top:12px'><h3>📅 By date range</h3>"
            "<form method='post'><input type='hidden' name='mode' value='daterange'>"
            "<div class='row'><div><label>From (YYYY-MM-DD)</label>"
            "<input type='text' name='date_from' placeholder='2024-01-01' required></div>"
            "<div><label>To (YYYY-MM-DD)</label>"
            "<input type='text' name='date_to' placeholder='2024-12-31' required></div></div>"
            "<div class='note'>Matches files posted in this range.</div>"
            "<div style='margin-top:10px'><button class='btn red' type='submit'>"
            "Preview delete</button></div></form></div>"
            "<div class='card' style='margin-top:12px'><h3>💥 Delete ALL files</h3>"
            "<form method='post'><input type='hidden' name='mode' value='all'>"
            "<div class='alert'>This wipes the entire file index. The channels "
            "must be re-indexed afterwards.</div>"
            "<div style='margin-top:10px'><button class='btn red' type='submit'>"
            "Preview delete</button></div></form></div>")
    return page("Cleanup", body, "clean")


def _cleanup_filter(mode: str, keyword: str, date_from: str, date_to: str):
    q = select(File)
    label = ""
    if mode == "keyword":
        like = f"%{keyword}%"
        q = q.where(or_(File.file_name.ilike(like),
                        File.caption.ilike(like)))
        label = f"keyword “{keyword}”"
    elif mode == "daterange":
        try:
            d_from = datetime.strptime(date_from.strip(), "%Y-%m-%d")
            d_to = datetime.strptime(date_to.strip(), "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "Dates must be YYYY-MM-DD")
        q = q.where(File.posted_at >= d_from, File.posted_at <= d_to)
        label = f"posted {date_from} → {date_to}"
    elif mode == "all":
        label = "ALL files"
    else:
        raise HTTPException(400, "bad mode")
    return q, label


@router.post("/cleanup")
async def cleanup_run(request: Request, mode: str = Form(""),
                      keyword: str = Form(""), date_from: str = Form(""),
                      date_to: str = Form(""), confirm: str = Form("")):
    _need_auth(request)
    q, label = _cleanup_filter(mode, keyword, date_from, date_to)
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        count = (await s.execute(
            select(func.count()).select_from(q.subquery()))).scalar() or 0
        if not confirm:
            # step 1: show count, ask for confirmation
            body = (f"<h2>🧹 Confirm delete</h2>"
                    f"<div class='alert'><b>{count:,}</b> files match {esc(label)}.</div>"
                    "<form method='post'>"
                    f"<input type='hidden' name='mode' value='{esc(mode)}'>"
                    f"<input type='hidden' name='keyword' value='{esc(keyword)}'>"
                    f"<input type='hidden' name='date_from' value='{esc(date_from)}'>"
                    f"<input type='hidden' name='date_to' value='{esc(date_to)}'>"
                    "<input type='hidden' name='confirm' value='yes'>"
                    "<button class='btn red' type='submit'>"
                    f"Yes, delete {count:,} files</button> "
                    "<a class='btn grey' href='/admin/cleanup'>Cancel</a></form>")
            return page("Confirm delete", body, "clean")
        # step 2: execute
        from sqlalchemy import delete as sa_delete
        stmt = sa_delete(File)
        if q.whereclause is not None:
            stmt = stmt.where(q.whereclause)
        result = await s.execute(stmt)
        await s.commit()
        deleted = result.rowcount or 0
    return RedirectResponse(
        f"/admin/cleanup?msg=Deleted {deleted:,} files ({label})",
        status_code=303)


# ---------- settings ----------

@router.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request, msg: str = ""):
    _need_auth(request)
    banner = f'<div class="okmsg">{esc(msg)}</div>' if msg else ""
    fields = ""
    for key, spec in rt.MANAGED.items():
        cur = await rt.aget_setting(key)
        hint = spec.get("hint", "")
        secret = spec.get("secret")
        display = "••••••••" if secret and cur else esc(cur if cur is not None else "")
        typ = spec["type"]
        if typ == "bool":
            checked = "checked" if cur else ""
            inp = (f"<input type='checkbox' name='{key}' value='1' {checked} "
                   "style='width:auto;transform:scale(1.4)'>")
        elif typ == "int":
            inp = (f"<input type='number' name='{key}' value='{esc(cur or 0)}'>")
        elif typ == "text":
            inp = (f"<textarea name='{key}'>{esc(cur or '')}</textarea>")
        else:
            it = "password" if secret else "text"
            inp = (f"<input type='{it}' name='{key}' value='{display}' "
                   f"placeholder='{'leave blank to keep' if secret else ''}'>")
        fields += (f"<label>{esc(spec['label'])}</label>{inp}"
                   f"<div class='note'>{esc(hint)}"
                   + (f" <span class='mut'>(env default: {esc(getattr(settings, spec['env'], ''))})</span>"
                      if spec.get("env") else "") + "</div>")
    body = (f"<h2>⚙️ Settings</h2>{banner}"
            "<form method='post'>" + fields +
            "<div style='margin-top:16px'>"
            "<button class='btn' type='submit'>💾 Save all</button></div>"
            "</form><div class='note'>Saved values override the environment "
            "immediately — no redeploy needed.</div>")
    return page("Settings", body, "set")


@router.post("/settings")
async def settings_save(request: Request):
    _need_auth(request)
    form = await request.form()
    changed = 0
    for key, spec in rt.MANAGED.items():
        typ = spec["type"]
        if typ == "bool":
            raw = "1" if form.get(key) else "0"
        else:
            raw = form.get(key, "")
            if raw is None:
                continue
            raw = str(raw)
            if spec.get("secret") and not raw.strip():
                continue  # keep existing secret
        await rt.set_setting(key, raw)
        changed += 1
    return RedirectResponse(
        f"/admin/settings?msg=Saved {changed} settings.", status_code=303)
