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
    # NOTE: no user session needed. The bot walks channel history itself
    # via MTProto (channels.GetMessages ID-walk — messages.GetHistory is
    # [400 BOT_METHOD_INVALID] for bots). It must be ADMIN in each
    # indexed channel. This is how Tech VJ-style bots index.

    # --- Database ---
    DATABASE_URL: str = ""           # shard 0 (primary); all small tables live here
    # v10.13 sharding: comma-separated shard DATABASE_URLs in fill order.
    # Empty = single-DB mode (DATABASE_URL only). Shard 0 must equal
    # DATABASE_URL. Writes fill shard 0 first, then rotate to the next
    # shard once it passes SHARD_SIZE_MB.
    DATABASE_URLS: str = ""
    # Per-shard soft size cap in MB — writes rotate to the next shard
    # past this. Keep under the provider's hard cap (Supabase free: 500).
    SHARD_SIZE_MB: int = 400

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
    # v10.8.4: comma-separated extra keys — the bot rotates to the next
    # key when one is rate-limited. Keys from DIFFERENT Groq accounts
    # each get their own daily token budget; keys from the same account
    # share one budget (Groq TPD is per-organization).
    GROQ_API_KEYS: str = ""
    AI_MODEL: str = "openai/gpt-oss-20b"  # the one working model on our key
    AI_PARSE_MODEL: str = "openai/gpt-oss-20b"  # fast model for v7 query parsing
    # Dead llama models removed (Groq retired them — 404 even on fresh keys).
    # Single working model only: no wasted fallback attempts.
    AI_FALLBACK_MODELS: str = ""
    WEBSEARCH_API_URL: str = "https://moovidex.alwaysdata.net"  # web search API for ai_web_answer
    # Max AI actions per user per day (one AI search/chat ≈ 2 Groq calls).
    AI_DAILY_QUOTA: int = 50

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

    @field_validator("DATABASE_URLS", mode="before")
    @classmethod
    def _fix_db_urls(cls, v: str) -> str:
        # Same asyncpg scheme fix as DATABASE_URL, per comma-separated URL.
        if not v:
            return v
        out = []
        for u in v.split(","):
            u = u.strip()
            if u.startswith("postgres://"):
                u = u.replace("postgres://", "postgresql+asyncpg://", 1)
            elif u.startswith("postgresql://"):
                u = u.replace("postgresql://", "postgresql+asyncpg://", 1)
            out.append(u)
        return ",".join(out)

    @field_validator("TG_API_ID", mode="before")
    @classmethod
    def _fix_api_id(cls, v):
        # P3: empty env string must not blow up import with a raw
        # pydantic ValidationError — coerce to the 0 default instead.
        if v == "" or v is None:
            return 0
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
