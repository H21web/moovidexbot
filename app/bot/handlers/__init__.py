"""Register every handler module on the bot client."""
from __future__ import annotations

from pyrogram import Client

from app.bot.handlers import admin, callbacks, groups, index, inline, requests, saved, search, start


def register_all(bot: Client) -> None:
    start.register(bot)
    groups.register(bot)
    search.register(bot)
    callbacks.register(bot)
    admin.register(bot)
    index.register(bot)
    requests.register(bot)
    saved.register(bot)   # v10: watchlist + /saved + /mystats
    inline.register(bot)  # v10: inline mode
