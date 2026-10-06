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
from app import analytics
from app import autodelete
from app import keepalive
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
    if not settings.WEB_SECRET or settings.WEB_SECRET == "change-me":
        raise SystemExit(
            "WEB_SECRET must be set to a long random value — "
            "unset/default forges /watch, /dl and admin cookies")
    if not settings.DATABASE_URL:
        raise SystemExit("DATABASE_URL is required")
    if not settings.WEB_URL:
        # P3: warn loudly, don't crash — player/download links degrade
        # to in-Telegram delivery, but the bot itself still works.
        log.warning(
            "WEB_URL is unset — /watch and /dl links will be broken; "
            "set it to the public base URL (e.g. https://moovidex.run.place)")

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
    ad_task = autodelete.start()
    prune_task = analytics.start_prune_task()  # v10.8.10: activity log 30d
    ka_task = keepalive.start()  # v10.11.10: DB idle-sleep keepalive
    try:
        await server.serve()
    finally:
        # P3: await the cancel — a bare cancel() leaves the task
        # dangling and can swallow shutdown errors.
        ad_task.cancel()
        prune_task.cancel()
        ka_task.cancel()
        try:
            await ad_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("autodelete shutdown: %s", exc)
        try:
            await prune_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("prune shutdown: %s", exc)
        try:
            await ka_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("keepalive shutdown: %s", exc)
        # Close shared httpx clients and the DB pool.
        try:
            from app import ai as ai_mod
            from app import tmdb as tmdb_mod
            await ai_mod.close_client()
            await tmdb_mod.close_client()
        except Exception as exc:  # noqa: BLE001
            log.debug("httpx client close failed: %s", exc)
        try:
            from app.db import get_engine
            await get_engine(settings.DATABASE_URL).dispose()
        except Exception as exc:  # noqa: BLE001
            log.debug("engine dispose failed: %s", exc)
        await bot_app.stop_all()


if __name__ == "__main__":
    asyncio.run(amain())
