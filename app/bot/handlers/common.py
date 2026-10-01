"""Shared handler helpers: admin gate, user tracking."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.types import Message
from sqlalchemy import select

from app.config import settings
from app.db import get_session_factory
from app.models import User

log = logging.getLogger(__name__)


def admin_only(func):
    """Decorator: only ADMIN_IDS may invoke."""

    async def wrapper(client: Client, message: Message, *args, **kwargs):
        uid = message.from_user.id if message.from_user else None
        if not settings.is_admin(uid):
            await message.reply_text("⛔ Admins only.")
            return
        return await func(client, message, *args, **kwargs)

    wrapper.__name__ = func.__name__
    return wrapper


async def track_user(message: Message) -> User | None:
    """Upsert the sender into users. Returns None if banned."""
    u = message.from_user
    if not u:
        return None
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            user = (await session.execute(
                select(User).where(User.id == u.id))).scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if user is None:
                user = User(id=u.id, first_name=(u.first_name or "")[:128],
                            username=(u.username or "")[:64], last_seen=now)
                session.add(user)
            else:
                user.first_name = (u.first_name or "")[:128]
                user.username = (u.username or "")[:64]
                user.last_seen = now
            await session.commit()
            return user
    except Exception as exc:
        log.debug("track_user failed: %s", exc)
        return None


async def is_banned(user_id: int) -> bool:
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            user = (await session.execute(
                select(User).where(User.id == user_id))).scalar_one_or_none()
            return bool(user and user.is_banned)
    except Exception:
        return False
