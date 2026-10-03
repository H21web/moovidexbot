"""Callback queries: pagination, movie cards, delivery, misc."""
from __future__ import annotations

import asyncio
import logging
import math

from pyrogram import Client, filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.errors import FloodWait, PeerIdInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app import personalize, state
from app import autodelete
from app.analytics import log_event
from app.bot import forcesub, ui, v8_ui
from app.bot.handlers.common import is_banned
from app.bot.handlers.groups import effective_autodelete
from app.config import settings
from app.db import bump_file_downloads, get_session_factory
from app.models import File
from app.tmdb import get_movie
from app.web.tokens import watch_url

log = logging.getLogger(__name__)


async def _safe_edit(message, text: str, reply_markup=None,
                     parse_mode: ParseMode | None = ParseMode.HTML) -> bool:
    """Edit a results/card message whether it is text or a photo.

    Falls back to ``edit_caption`` when the message is a photo card,
    and to a fresh reply when editing is impossible. Returns True when
    the original message was updated in place.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup,
                                parse_mode=parse_mode)
        return True
    except Exception:
        pass
    try:
        await message.edit_caption(caption=text, reply_markup=reply_markup,
                                   parse_mode=parse_mode)
        return True
    except Exception:
        pass
    try:
        await message.reply_text(text, reply_markup=reply_markup,
                                 parse_mode=parse_mode)
    except Exception:
        log.debug("safe_edit failed completely", exc_info=True)
    return False


def _page_data(token: str, page: int):
    data = state.results_get(token)
    if not data:
        return None, None, None
    groups = data["groups"]
    per = settings.RESULTS_PER_PAGE
    total_pages = max(1, math.ceil(len(groups) / per))
    page = max(0, min(page, total_pages - 1))
    return data, groups[page * per:(page + 1) * per], (page, total_pages,
                                                     page * per)


async def _pg(client: Client, query):
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    try:
        token, page_s = query.data.split(":")[1:]
        page = int(page_s)
    except (ValueError, IndexError):
        return
    data, chunk, meta = _page_data(token, page)
    if not data:
        await query.answer("⌛ Expired — search again.", show_alert=True)
        return
    page, total_pages, start = meta
    text = (f"🔍 <b>Results for</b> {ui.esc(data['query'])}\n"
            f"<i>{len(data['groups'])} found</i>")
    await query.message.edit_text(
        text, reply_markup=ui.results_kb(token, page, total_pages, chunk,
                                         page_start=start))
    await query.answer()


async def _movie(client: Client, query):
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    try:
        _, token, gidx_s = query.data.split(":")
        gidx = int(gidx_s)
    except (ValueError, IndexError):
        return
    data = state.results_get(token)
    if not data or gidx >= len(data["groups"]):
        await query.answer("⌛ Expired — search again.", show_alert=True)
        return
    group = data["groups"][gidx]
    per = settings.RESULTS_PER_PAGE
    page = gidx // per
    await query.answer()
    poster = None
    meta = None
    try:
        meta = await get_movie(group.get("display"), group.get("year"))
        poster = (meta or {}).get("poster_url")
    except Exception as exc:
        log.debug("tmdb failed: %s", exc)
    # v6: in PM, mark cards as personalized + order quality buttons by taste.
    is_pm = not str(query.message.chat.id).startswith("-")
    qorder = None
    if is_pm:
        uid = data.get("user_id") or query.from_user.id
        prefs = await personalize.get_prefs(uid)
        if prefs["enabled"] and prefs["downloads"] >= personalize.MIN_DOWNLOADS:
            qorder = personalize.quality_order(prefs)
    text = ui.movie_card(group, meta=meta,
                         personalized=bool(qorder))
    kb = ui.movie_kb(token, gidx, group, page, qorder=qorder)
    try:
        if poster:
            await query.message.edit_text("🎬 <i>Loading…</i>",
                                          parse_mode=ParseMode.HTML)
            sent = await query.message.reply_photo(
                poster, caption=text, reply_markup=kb,
                parse_mode=ParseMode.HTML)
            await query.message.delete()
            # The old results message had autodelete in groups — the new
            # photo must not outlive it.
            ad = await effective_autodelete(query.message.chat.id)
            if ad > 0:
                await autodelete.schedule(query.message.chat.id,
                                          sent.id, ad)
        else:
            await query.message.edit_text(text, reply_markup=kb,
                                          parse_mode=ParseMode.HTML)
    except Exception as exc:
        log.debug("movie card edit failed: %s", exc)
        # Don't leave the message stuck on "Loading…" — show the text card.
        await _safe_edit(query.message, text, reply_markup=kb)


async def _back(client: Client, query):
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    try:
        _, token, page_s = query.data.split(":")
        page = int(page_s)
    except (ValueError, IndexError):
        return
    await query.answer()
    data, chunk, meta = _page_data(token, page)
    if not data:
        await query.message.edit_text("⌛ Expired — search again.")
        return
    page, total_pages, start = meta
    # Go back by editing the same message (it may currently be a card).
    text = (f"🔍 <b>Results for</b> {ui.esc(data['query'])}\n"
            f"<i>{len(data['groups'])} found</i>")
    await _safe_edit(
        query.message, text,
        reply_markup=ui.results_kb(token, page, total_pages, chunk,
                                   page_start=start))


async def _send_file(client: Client, target_id: int, f, uid: int):
    """Send a File row to ``target_id`` via cached media.

    Shared by in-PM delivery, group→PM delivery, and the ``dl_``
    deep-link handler. Returns the sent message.
    """
    # send_cached_media (not send_document): send_document rejects
    # non-document file_ids ("Expected DOCUMENT, got VIDEO"), which
    # broke delivery for every video file.
    sent = await client.send_cached_media(
        target_id,
        file_id=f.file_id,
        caption=ui.file_caption({
            "file_name": f.file_name, "quality": f.quality,
            "language": f.language, "file_size": f.file_size}),
        parse_mode=ParseMode.HTML,
        reply_markup=v8_ui.v8_file_kb(f.id, uid),
        protect_content=settings.PROTECT_CONTENT,
    )
    asyncio.create_task(log_event("download", user_id=uid,
                                  chat_id=sent.chat.id))
    # v8.1: per-file download counter (drives "most downloaded = best pick").
    asyncio.create_task(bump_file_downloads(f.id))
    # v10.2: the user's own /deltimer wins; otherwise the group/global
    # default (no per-group row exists for a user id in PM).
    from app.bot.handlers.deltimer import get_user_del_timer
    ad = await get_user_del_timer(uid)
    if ad is None:
        ad = await effective_autodelete(target_id)
    if ad > 0:
        await autodelete.schedule(sent.chat.id, sent.id, ad)
    # v6: learn from this download (fire-and-forget, plain dict — the ORM
    # object detaches after the session closes).
    personalize.fire_record_download(
        uid, {"file_name": f.file_name, "file_size": f.file_size})
    return sent


async def _get_file(file_db_id: int):
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        return (await session.execute(
            select(File).where(File.id == file_db_id))).scalar_one_or_none()


async def _deliver(client: Client, query):
    await query.answer("📤 Preparing your file…")
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    src = query.message.chat
    in_group = src.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    try:
        file_db_id = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        return
    kb = await forcesub.ensure_joined(client, uid, chat_id=src.id)
    if kb:
        # v10.2.1: remember which file they wanted — "✅ I've joined"
        # auto-delivers it instead of making them tap download again.
        state.pending_dl[uid] = file_db_id
        # Reuse the card message: swap its content for the join prompt.
        await _safe_edit(
            query.message,
            "📢 <b>Join our channels to download</b>",
            reply_markup=kb)
        return
    f = await _get_file(file_db_id)
    if not f:
        await query.answer("❌ File not found (removed?).", show_alert=True)
        return

    # Group searches deliver to the user's PM only — never in the group.
    target = uid if in_group else src.id
    if not in_group:
        await _safe_edit(query.message, "📤 <i>Uploading…</i>")
    try:
        await _send_file(client, target, f, uid)
    except PeerIdInvalid:
        # User never started the bot in PM — one-tap deep link that
        # delivers this exact file once they tap START.
        me = await client.get_me()
        username = me.username
        if username:
            deep = f"https://t.me/{username}?start=dl_{f.id}"
            await _safe_edit(
                query.message,
                "👋 <b>Almost there!</b>\n\n"
                "Tap below to open my private chat — "
                "your file will be sent there:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("▶️ Open bot & get file",
                                         url=deep)]]))
        else:
            # No username: a t.me link would point nowhere — tell the
            # user to open the PM manually instead.
            await _safe_edit(
                query.message,
                "👋 <b>Almost there!</b>\n\n"
                "Please open my PM manually and tap START — "
                "then search again and I'll send your file there.")
        return
    except FloodWait as exc:
        await _safe_edit(
            query.message,
            f"⏳ Flood control — retry in {exc.value}s.",
            parse_mode=None)
        return
    except Exception as exc:
        log.warning("deliver failed for file %d: %s", f.id, exc)
        await _safe_edit(query.message,
                         "❌ Couldn't send the file. Try again later.",
                         parse_mode=None)
        return
    if in_group:
        await query.answer("📥 File sent to your private chat ✅")
    else:
        try:
            await query.message.delete()
        except Exception:
            pass


async def _spell(client: Client, query):
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    suggestion = query.data.split(":", 1)[1]
    await query.answer()
    # v10.2: re-run the full v10 search flow and render the SAME v8 card
    # model as a normal search — no more old button model anywhere.
    from app.bot.handlers.search import _search_and_send
    try:
        await query.message.delete()
    except Exception:
        pass
    sent = await _search_and_send(client, query.message.chat.id, uid,
                                  suggestion)
    if not sent:
        await client.send_message(query.message.chat.id,
                                  "❌ Still nothing found.")


async def _dym(client: Client, query):
    """Did-you-mean confirm: ``dym:{token}:yes`` / ``dym:{token}:no``.

    Flow diagram: ✅ Yes -> normal AutoFilter search with the corrected
    title -> results. ❌ No -> save the ORIGINAL search as a movie
    request -> "Request submitted".
    """
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    try:
        _, token, action = query.data.split(":")
    except (ValueError, AttributeError):
        return
    data = state.dym_tokens.pop(token, None)
    if not data or data.get("uid") != uid:
        await query.answer("⌛ Expired — search again.", show_alert=True)
        return
    original = data.get("original") or ""
    corrected = data.get("corrected") or ""
    if action == "yes" and corrected:
        await query.answer(f"🔍 {corrected[:40]}")
        try:
            await query.message.edit_text(
                f"🔍 <i>Searching <b>{ui.esc(corrected[:80])}</b>…</i>",
                parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001
            pass
        try:
            from app.bot.handlers import search as search_handlers
            await search_handlers._v9_search_flow(
                client, query.message, uid, corrected, _confirmed=True)
        except Exception:  # noqa: BLE001
            log.exception("dym yes-flow failed")
        return
    # no -> save the original search as a movie request
    await query.answer("🎞 Saving as request…")
    try:
        from app.bot.handlers.requests import submit_request
        rid = await submit_request(client, uid,
                                   query.message.chat.id, original)
    except Exception:  # noqa: BLE001
        log.exception("dym request submit failed")
        await query.message.reply_text("❌ Could not save your request — "
                                       "try again later.")
        return
    try:
        await query.message.edit_text(
            f"✅ <b>Request submitted!</b>\n"
            f"We'll try to add <b>{ui.esc(original[:80])}</b> soon. 🎬",
            parse_mode=ParseMode.HTML)
    except Exception:  # noqa: BLE001
        pass


async def _fsub_retry(client: Client, query):
    uid = query.from_user.id
    kb = await forcesub.ensure_joined(client, uid,
                                      chat_id=query.message.chat.id)
    if kb:
        await query.answer("❌ You haven't joined all channels yet.",
                           show_alert=True)
    else:
        await query.answer("✅ All joined!", show_alert=True)
        # v10.2.1: auto-deliver the waiting file (download / deep-link).
        # v10.3: auto-continue the waiting search — no retyping.
        dl_id = state.pending_dl.pop(uid, None)
        q = state.pending_search.pop(uid, None)
        if q:
            # Turn the join prompt into a status line (photo-safe), then
            # run the normal search flow against it.
            await _safe_edit(
                query.message,
                "✅ <i>All joined — continuing your search…</i>")
            try:
                from app.bot.handlers import search as search_handlers
                await search_handlers._handle_text_query(
                    client, query.message, uid, q)
            except Exception as exc:  # noqa: BLE001
                log.warning("post-join search failed for user %d: %s",
                            uid, exc)
        else:
            try:
                await query.message.delete()
            except Exception:
                pass
        if dl_id:
            f = await _get_file(dl_id)
            if f:
                try:
                    await _send_file(client, uid, f, uid)
                except Exception as exc:
                    log.warning("retry deliver failed for file %d: %s",
                                dl_id, exc)
                    try:
                        await client.send_message(
                            uid, "❌ Couldn't send the file. "
                                 "Try again later.")
                    except Exception:
                        pass
        if not dl_id and not q:
            # The join prompt is gone and this callback carries no
            # results token — point the user back to search.
            try:
                await client.send_message(
                    uid, "✅ All joined! Search again to get your results 🔍")
            except Exception:
                pass


async def _ixstop(client: Client, query):
    uid = query.from_user.id if query.from_user else None
    if not settings.is_admin(uid):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    try:
        job_id = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        return
    job = state.job_get(job_id)
    if not job:
        await query.answer("No active job.", show_alert=True)
        return
    job.cancel_event.set()
    await query.answer("🛑 Stopping…", show_alert=False)


async def _pset(client: Client, query):
    """Personalization toggle / reset from /settings (non-admin users)."""
    uid = query.from_user.id
    action = query.data.split(":", 1)[1] if ":" in query.data else ""
    if action == "toggle":
        prefs = await personalize.get_prefs(uid)
        await personalize.set_enabled(uid, not prefs["enabled"])
        await query.answer("✅ Updated")
    elif action == "reset":
        await personalize.reset(uid)
        await query.answer("🗑 Taste reset")
    else:
        await query.answer()
        return
    prefs = await personalize.get_prefs(uid)
    await query.message.edit_text(
        ui.user_settings_text(prefs["enabled"], prefs["downloads"]),
        reply_markup=ui.user_settings_kb(prefs["enabled"]),
        parse_mode=ParseMode.HTML)


async def _v8page(client: Client, query) -> None:
    """v8 results pagination: ``v8:{token}:{page}``."""
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    try:
        _, token, page = query.data.split(":")
        page = int(page)
    except (ValueError, IndexError):
        return
    data = state.v8_get(token)
    # v10.2: group result cards are shared — any member may paginate.
    if not data or (not data.get("group") and data.get("user_id") != uid):
        await query.answer("⌛ Results expired — search again.", show_alert=True)
        return
    await query.answer()
    from app.bot.handlers.search import render_v8_results
    await render_v8_results(client, query.message, token, uid, page)


async def _rfilter(client: Client, query) -> None:
    """v8 filter selectors.

    ``rf:{token}:{kind}`` -> show options (edits keyboard only)
    ``rf:{token}:{kind}:{idx}`` -> apply option
    ``rf:{token}:{kind}:x`` -> clear filter
    ``rf:{token}:back`` -> back to the results view
    """
    uid = query.from_user.id
    if await is_banned(uid):
        await query.answer("⛔ You are banned.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        return
    token = parts[1]
    kind = parts[2]
    data = state.v8_get(token)
    # v10.2: group result cards are shared — any member may paginate.
    if not data or (not data.get("group") and data.get("user_id") != uid):
        await query.answer("⌛ Results expired — search again.", show_alert=True)
        return
    await query.answer()
    from app.bot.handlers.search import render_v8_results

    if kind == "back" or len(parts) == 3:
        if kind == "back":
            await render_v8_results(client, query.message, token, uid, 0)
            return
        # show options for this filter kind
        options = (data.get("filter_opts") or {}).get(kind) or []
        if not options:
            await query.answer("No options for this filter.", show_alert=True)
            return
        kb, label = v8_ui.v8_filter_options_kb(
            token, kind, options, data.get("filters") or {})
        try:
            await query.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            log.debug("filter options edit failed", exc_info=True)
        return

    # apply / clear
    choice = parts[3]
    new_filters = dict(data.get("filters") or {})
    if choice == "x":
        new_filters.pop(kind, None)
    else:
        options = (data.get("filter_opts") or {}).get(kind) or []
        try:
            value = options[int(choice)]
        except (ValueError, IndexError):
            return
        new_filters[kind] = value
    data["filters"] = new_filters
    await render_v8_results(client, query.message, token, uid, 0)

def register(bot: Client) -> None:
    bot.on_callback_query(filters.regex(r"^pg:"))(_pg)
    bot.on_callback_query(filters.regex(r"^mv:"))(_movie)
    bot.on_callback_query(filters.regex(r"^bk:"))(_back)
    bot.on_callback_query(filters.regex(r"^dl:"))(_deliver)
    bot.on_callback_query(filters.regex(r"^sp:"))(_spell)
    bot.on_callback_query(filters.regex(r"^fsub_retry$"))(_fsub_retry)
    bot.on_callback_query(filters.regex(r"^dym:"))(_dym)
    bot.on_callback_query(filters.regex(r"^ixstop:"))(_ixstop)
    bot.on_callback_query(filters.regex(r"^pset:"))(_pset)
    bot.on_callback_query(filters.regex(r"^v8:"))(_v8page)
    bot.on_callback_query(filters.regex(r"^rf:"))(_rfilter)
