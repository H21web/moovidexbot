"""Register every handler module on the bot client."""
from __future__ import annotations

from pyrogram import Client

from app.bot.handlers import admin, callbacks, index, requests, search, start


def register_all(bot: Client) -> None:
    start.register(bot)
    search.register(bot)
    callbacks.register(bot)
    admin.register(bot)
    index.register(bot)
    requests.register(bot)
