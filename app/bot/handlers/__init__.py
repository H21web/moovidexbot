"""Register every handler module on the bot client."""
from __future__ import annotations

from pyrogram import Client

from app.bot.handlers import admin, callbacks, deltimer, groups, index, requests, saved, search, start
from app.bot import forcesub


def register_all(bot: Client) -> None:
    start.register(bot)
    groups.register(bot)
    search.register(bot)
    callbacks.register(bot)
    admin.register(bot)
    index.register(bot)
    requests.register(bot)
    saved.register(bot)   # v10: watchlist + /saved + /mystats
    deltimer.register(bot)  # v10.2: /deltimer per-user auto-delete
    forcesub.register(bot)  # v10.8.10: auto-approve join requests
    # v10.9.0: inline mode removed per admin request.
