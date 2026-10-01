"""Signed tokens for /watch and /dl links."""
from __future__ import annotations

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings

_SALT = "moovidex-watch-v1"
MAX_AGE = 7 * 24 * 3600  # 7 days


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.WEB_SECRET, salt=_SALT)


def make_watch_token(file_db_id: int, user_id: int) -> str:
    return _signer().dumps({"f": file_db_id, "u": user_id})


def parse_watch_token(token: str) -> dict | None:
    try:
        data = _signer().loads(token, max_age=MAX_AGE)
        if isinstance(data, dict) and "f" in data:
            return data
    except BadSignature:
        pass
    return None


def watch_url(file_db_id: int, user_id: int) -> str | None:
    base = (settings.WEB_URL or "").rstrip("/")
    if not base:
        return None
    return f"{base}/watch/{make_watch_token(file_db_id, user_id)}"
