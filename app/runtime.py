"""DB-backed runtime settings, manageable from the admin dashboard.

``MANAGED`` declares every setting the dashboard can change. Values are
stored in the ``bot_settings`` table and override the environment defaults
from :mod:`app.config`. A short in-memory cache avoids a DB hit per read.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select

from app.config import settings
from app.db import get_session_factory
from app.models import BotSetting

log = logging.getLogger(__name__)

# key -> {type, label, hint, env_default}
MANAGED: dict[str, dict] = {
    "FORCE_SUB_CHANNELS": {
        "type": "str", "label": "Force-sub channels",
        "hint": "Comma separated @usernames or ids. Empty = off.",
        "env": "FORCE_SUB_CHANNELS",
    },
    "FSUB_JOIN_REQUEST": {
        "type": "bool", "label": "Force-sub: request-to-join links",
        "hint": "Join buttons open a join REQUEST (auto-approved) instead "
                "of a direct join. Bot must be admin with invite rights.",
        "env": "FSUB_JOIN_REQUEST", "default": True,
    },
    "FSUB_AUTO_APPROVE": {
        "type": "bool", "label": "Force-sub: auto-approve join requests",
        "hint": "Automatically approve channel join requests.",
        "env": "FSUB_AUTO_APPROVE", "default": True,
    },
    "AUTO_DELETE_SECONDS": {
        "type": "int", "label": "Default auto-delete (seconds)",
        "hint": "0 = off. Per-group values override this.",
        "env": "AUTO_DELETE_SECONDS",
    },
    "RESULTS_PER_PAGE": {
        "type": "int", "label": "Results per page",
        "hint": "Buttons per search results page.",
        "env": "RESULTS_PER_PAGE",
    },
    "PROTECT_CONTENT": {
        "type": "bool", "label": "Protect content",
        "hint": "Block forwarding of delivered files.",
        "env": "PROTECT_CONTENT",
    },
    "REQUEST_CHANNEL": {
        "type": "str", "label": "Request channel",
        "hint": "@username or id where /request posts go.",
        "env": "REQUEST_CHANNEL",
    },
    "LOG_CHANNEL": {
        "type": "str", "label": "Log channel",
        "hint": "@username or id for admin logs.",
        "env": "LOG_CHANNEL",
    },
    "TMDB_API_KEY": {
        "type": "str", "label": "TMDB API key", "secret": True,
        "hint": "Posters on movie cards. Empty = off.",
        "env": "TMDB_API_KEY",
    },
    "WARN_LIMIT": {
        "type": "int", "label": "Warns before auto-ban",
        "hint": "User is auto-banned after this many warns.",
        "env": None, "default": 3,
    },
    "WELCOME_PM": {
        "type": "text", "label": "PM welcome text",
        "hint": "Sent on /start in private. HTML allowed. Empty = built-in.",
        "env": None, "default": "",
    },
    "WELCOME_GROUP": {
        "type": "text", "label": "Group welcome text",
        "hint": "Sent when bot joins a group. Empty = off.",
        "env": None, "default": "",
    },
}

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 60.0


def _env_default(key: str):
    spec = MANAGED[key]
    if spec.get("env"):
        return getattr(settings, spec["env"], spec.get("default"))
    return spec.get("default")


def _coerce(key: str, value):
    typ = MANAGED[key]["type"]
    if value is None:
        return _env_default(key)
    try:
        if typ == "int":
            return int(value)
        if typ == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        return str(value)
    except (TypeError, ValueError):
        return _env_default(key)


def get_setting(key: str):
    """Read a managed setting (DB override wins, else env/default). Sync
    cache lookup only — use :func:`aget_setting` for a fresh read."""
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    return _env_default(key)


async def aget_setting(key: str):
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    value = _env_default(key)
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            row = (await s.execute(
                select(BotSetting).where(BotSetting.key == key)
            )).scalar_one_or_none()
            if row is not None and row.value is not None:
                value = _coerce(key, row.value.get("v"))
    except Exception as exc:
        log.debug("aget_setting %s failed: %s", key, exc)
    _CACHE[key] = (now, value)
    return value


async def set_setting(key: str, raw: str) -> object:
    """Persist a dashboard change. Returns the coerced value."""
    if key not in MANAGED:
        raise KeyError(key)
    value = _coerce(key, raw)
    try:
        factory = get_session_factory(settings.DATABASE_URL)
        async with factory() as s:
            row = (await s.execute(
                select(BotSetting).where(BotSetting.key == key)
            )).scalar_one_or_none()
            if row is None:
                s.add(BotSetting(key=key, value={"v": value}))
            else:
                row.value = {"v": value}
            await s.commit()
    except Exception as exc:
        log.warning("set_setting %s failed: %s", key, exc)
        raise
    _CACHE[key] = (time.monotonic(), value)
    return value


def invalidate(key: str | None = None) -> None:
    if key:
        _CACHE.pop(key, None)
    else:
        _CACHE.clear()
