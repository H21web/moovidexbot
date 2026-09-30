"""Pyrogram client — SINGLE bot-token client, pure MTProto.

The bot account does everything over MTProto (no Bot API library):

* search, buttons, inline mode, file delivery
* real-time auto-index of new channel posts (bot is admin)
* historical ``/index`` backfill — bots CAN read channel history via
  ``messages.getHistory`` when they are admin of the channel
  (this is exactly how Tech VJ-style bots index; no user session needed)
* web-player streaming — ``/dl`` reads file bytes with raw
  ``upload.GetFile`` (any file size, HTTP Range seeking works)

Requirement: the bot must be **admin** in every indexed channel.
"""
from __future__ import annotations

import logging

from pyrogram import Client

from app.config import settings

log = logging.getLogger(__name__)

bot: Client | None = None


def build_bot() -> Client:
    return Client(
        "moovidex-bot",
        api_id=settings.TG_API_ID,
        api_hash=settings.TG_API_HASH,
        bot_token=settings.BOT_TOKEN,
        in_memory=True,
        sleep_threshold=30,
    )


async def start_all() -> None:
    """Start the single bot client."""
    global bot
    bot = build_bot()
    await bot.start()
    me = await bot.get_me()
    log.info("bot started as @%s (%d)", me.username, me.id)


async def stop_all() -> None:
    global bot
    if bot:
        try:
            await bot.stop()
        except Exception:
            pass
        bot = None
