"""Environment-based configuration (12-factor).

Every setting comes from the environment; nothing secret lives in code.
See ``.env.example`` for the full list.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Telegram ---
    BOT_TOKEN: str = ""
    TG_API_ID: int = 0
    TG_API_HASH: str = ""
    # NOTE: no user session needed. The bot reads channel history itself
    # via MTProto (messages.getHistory) — it must be ADMIN in each
    # indexed channel. This is how Tech VJ-style bots index.

    # --- Database ---
    DATABASE_URL: str = ""

    # --- Access control ---
    ADMIN_IDS: str = ""            # comma separated telegram user ids
    FORCE_SUB_CHANNELS: str = ""   # comma separated @usernames or ids

    # --- Web ---
    WEB_URL: str = ""              # public base URL, e.g. https://moovidex.run.place
    WEB_SECRET: str = "change-me"  # signs /watch and /dl tokens
    PORT: int = 8000
    ADMIN_PASSWORD: str = ""       # admin dashboard login (required for /admin)

    # --- Optional integrations ---
    TMDB_API_KEY: str = ""
    REQUEST_CHANNEL: str = ""      # where /request posts go (@username or id)
    LOG_CHANNEL: str = ""          # admin log channel (@username or id)

    # --- AI (Groq) — v6 super update ---
    GROQ_API_KEY: str = ""         # empty = AI features disabled gracefully
    AI_MODEL: str = "llama-3.3-70b-versatile"
    # Max AI actions per user per day (one AI search/chat ≈ 2 Groq calls).
    AI_DAILY_QUOTA: int = 20

    # --- Behaviour ---
    RESULTS_PER_PAGE: int = 8
    PROTECT_CONTENT: bool = False  # forward-protection on delivered files
    AUTO_DELETE_SECONDS: int = 0   # 0 = disabled; else delete bot's file msg after N sec
    INDEX_BATCH_SIZE: int = 500    # DB rows per bulk insert during /index

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _fix_db_url(cls, v: str) -> str:
        # SQLAlchemy async needs the driver scheme; tolerate plain URLs.
        if v and v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v and v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    # --- helpers ---
    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.ADMIN_IDS.replace(";", ",").split(",") if x.strip().isdigit()}

    @property
    def force_sub_channels(self) -> list[str]:
        return [x.strip() for x in self.FORCE_SUB_CHANNELS.replace(";", ",").split(",") if x.strip()]

    def is_admin(self, user_id: int | None) -> bool:
        return bool(user_id) and int(user_id) in self.admin_ids


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
