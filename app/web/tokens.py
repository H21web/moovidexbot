"""Signed tokens for /watch and /dl links."""
from __future__ import annotations

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings

_SALT = "moovidex-watch-v1"
MAX_AGE = 24 * 3600  # 24 hours — bearer download URLs expire daily


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.WEB_SECRET, salt=_SALT)


def make_watch_token(file_db_id: int, user_id: int) -> str:
    return _signer().dumps({"f": file_db_id, "u": user_id})


def parse_watch_token(token: str) -> dict | None:
    """Verify signature + expiry and return the payload.

    The ``u`` (requesting user) binding written by :func:`make_watch_token`
    is enforced here: a present-but-malformed binding rejects the token.
    (The /watch and /dl endpoints are unauthenticated bearer links, so the
    binding is a tamper-evidence field, not a requester check.)
    """
    try:
        data = _signer().loads(token, max_age=MAX_AGE)
        if isinstance(data, dict) and "f" in data:
            u = data.get("u")
            if u is not None and not isinstance(u, int):
                return None
            return data
    except BadSignature:
        pass
    return None


def _web_base() -> str:
    """Public base URL for /watch and /dl links.

    v10.2: Telegram web-app buttons SILENTLY fail on plain http, so an
    http:// WEB_URL is upgraded to https:// (Render serves https) with a
    loud warning instead of dead buttons.
    """
    base = (settings.WEB_URL or "").rstrip("/")
    if base.startswith("http://"):
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "WEB_URL is http:// -- Telegram web apps need https; upgrading")
        base = "https://" + base[len("http://"):]
    return base


def watch_url(file_db_id: int, user_id: int) -> str | None:
    base = _web_base()
    if not base:
        return None
    return f"{base}/watch/{make_watch_token(file_db_id, user_id)}"


def dl_url(file_db_id: int, user_id: int) -> str | None:
    """Direct download link for a file (Tech VJ style: stream + download URLs).

    ``?dl=1`` marks an explicit download hit so the /dl route can count it
    (the web player's stream URL has no such marker — plays ≠ downloads).
    """
    base = _web_base()
    if not base:
        return None
    return f"{base}/dl/{make_watch_token(file_db_id, user_id)}?dl=1"


def sub_pick_url(file_db_id: int, user_id: int) -> str | None:
    """Telegram Web App URL for the subtitle language picker."""
    base = _web_base()
    if not base:
        return None
    return f"{base}/subs/pick/{make_watch_token(file_db_id, user_id)}"
