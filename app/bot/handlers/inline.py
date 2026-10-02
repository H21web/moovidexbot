"""v10: inline mode — @botname <query> works in any chat.

Each result is an article with a deep-link button; tapping it opens the
bot PM where the existing dl_<id> handler delivers the file.

NOTE: enable inline mode for the bot in @BotFather (/setinline) and set
an inline placeholder — otherwise Telegram never sends inline queries.
"""
from __future__ import annotations

import logging

from pyrogram import Client
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

from app import search_v9
from app.bot import ui

log = logging.getLogger(__name__)

_MAX_RESULTS = 8


async def _inline_query(client: Client, inline_query) -> None:
    q = (inline_query.query or "").strip()
    uid = inline_query.from_user.id if inline_query.from_user else 0
    if len(q) < 2:
        try:
            await inline_query.answer(
                [], cache_time=5, is_personal=True,
                switch_pm_text="🔍 Type a movie name…",
                switch_pm_parameter="inline")
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        res = await search_v9.smart_search(uid, q)
    except Exception:  # noqa: BLE001
        log.debug("inline search failed", exc_info=True)
        res = {"files": []}
    files = (res.get("files") or [])[:_MAX_RESULTS]
    me = await client.get_me()
    username = me.username or ""
    results: list[InlineQueryResultArticle] = []
    for f in files:
        fid = f.get("id")
        name = (f.get("file_name") or "file")[:60]
        bits = [x for x in (f.get("quality"), f.get("language")) if x]
        size = ui.fmt_size(f.get("file_size")) if f.get("file_size") else ""
        desc = " • ".join([x for x in (" • ".join(bits), size) if x])
        deep = f"https://t.me/{username}?start=dl_{fid}" if username else None
        kb = None
        if deep:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📥 Get File", url=deep)]])
        results.append(InlineQueryResultArticle(
            title=name,
            description=desc or "tap to get this file",
            input_message_content=InputTextMessageContent(
                f"🎬 <b>{ui.esc(name)}</b>"),
            reply_markup=kb,
        ))
    try:
        await inline_query.answer(results, cache_time=10, is_personal=True)
    except Exception:  # noqa: BLE001
        log.debug("inline answer failed", exc_info=True)


def register(bot: Client) -> None:
    bot.on_inline_query()(_inline_query)
