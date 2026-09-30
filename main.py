"""Moovidex MTProto — entrypoint.

Runs the Pyrogram bot client (updates, search, delivery, streaming) and
the FastAPI web player in ONE asyncio loop on Render.

    python main.py
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn
from sqlalchemy import text

from app.bot import app as bot_app
from app.bot.handlers import register_all
from app.config import settings
from app.db import get_session_factory
from app.web.app import create_app

log = logging.getLogger("moovidex")


async def check_db() -> None:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        await session.execute(text("SELECT 1"))
    log.info("database OK")


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.BOT_TOKEN or not settings.TG_API_ID or not settings.TG_API_HASH:
        raise SystemExit("BOT_TOKEN / TG_API_ID / TG_API_HASH are required")
    if not settings.DATABASE_URL:
        raise SystemExit("DATABASE_URL is required")

    await check_db()

    bot = bot_app.build_bot()
    register_all(bot)
    bot_app.bot = bot
    await bot.start()
    me = await bot.get_me()
    log.info("bot online as @%s", me.username)

    web = create_app()
    server = uvicorn.Server(uvicorn.Config(
        web, host="0.0.0.0", port=settings.PORT, log_level="warning"))
    log.info("web player on port %d", settings.PORT)
    try:
        await server.serve()
    finally:
        await bot_app.stop_all()


if __name__ == "__main__":
    asyncio.run(amain())
