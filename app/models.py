"""SQLAlchemy models — single squashed schema (migration 0001)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class File(Base):
    """One indexed Telegram file. file_id is the MTProto file_id string."""

    __tablename__ = "files"

    # Same filename + same size = same file (reposts get new file_ids,
    # so file_id alone can't catch them). DB-wide dedup.
    __table_args__ = (
        UniqueConstraint("file_name", "file_size",
                         name="uq_files_name_size"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    file_name: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    caption: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    quality: Mapped[str | None] = mapped_column(String(16), index=True)
    language: Mapped[str | None] = mapped_column(String(32), index=True)
    title_key: Mapped[str | None] = mapped_column(Text, index=True)
    views: Mapped[int | None] = mapped_column(Integer)
    forwards: Mapped[int | None] = mapped_column(Integer)
    # v8.1: how many times the bot actually delivered this file
    # (bot PM deliveries + explicit ⬇ Download link hits). Drives the
    # "most downloaded = best pick" rule.
    downloads: Mapped[int] = mapped_column(Integer, nullable=False,
                                          server_default="0", default=0)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# pg_trgm GIN indexes for fast ILIKE / similarity search.
Index("ix_files_file_name_trgm", File.file_name,
      postgresql_using="gin", postgresql_ops={"file_name": "gin_trgm_ops"})
Index("ix_files_caption_trgm", File.caption,
      postgresql_using="gin", postgresql_ops={"caption": "gin_trgm_ops"})
Index("ix_files_title_key_trgm", File.title_key,
      postgresql_using="gin", postgresql_ops={"title_key": "gin_trgm_ops"})


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    username: Mapped[str | None] = mapped_column(String(64))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    warns: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(256))
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MovieRequest(Base):
    __tablename__ = "movie_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SearchLog(Base):
    __tablename__ = "search_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    query: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class EventLog(Base):
    """Lightweight analytics events: start, download, request, ..."""

    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class TmdbCache(Base):
    __tablename__ = "tmdb_cache"

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    cached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BackfillJob(Base):
    """Resumable /index job state (survives restarts)."""

    __tablename__ = "backfill_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_token: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    channel_ref: Mapped[str | None] = mapped_column(String(256))
    channel_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    offset_id: Mapped[int] = mapped_column(Integer, default=0)
    total_scanned: Mapped[int] = mapped_column(Integer, default=0)
    total_indexed: Mapped[int] = mapped_column(Integer, default=0)
    total_skipped: Mapped[int] = mapped_column(Integer, default=0)
    total_errors: Mapped[int] = mapped_column(Integer, default=0)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSON)


class IndexSession(Base):
    """Interactive /index setup state — survives bot restarts.

    The in-memory ``state.pending_*`` dict is only an L1 cache; this
    table is the source of truth so a number the admin sends after a
    button tap is never lost (and never leaks to search).
    """

    __tablename__ = "index_sessions"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserPref(Base):
    """Per-user personalization profile (v6 super update).

    ``counters`` holds behavior counters learned from downloads, e.g.
    ``{"quality": {"1080p": 12, "720p": 3},
       "language": {"Malayalam": 9},
       "size": {"M": 10}, "codec": {"x264": 7},
       "genre": {"Action": 5}}``.
    """

    __tablename__ = "user_prefs"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    downloads: Mapped[int] = mapped_column(Integer, default=0)
    counters: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChatMemory(Base):
    """Per-user conversation memory for the AI chat (v6).

    Last ~20 messages per user, 7-day TTL (trimmed lazily on write).
    """

    __tablename__ = "chat_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(16))  # "user" | "assistant"
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AiCache(Base):
    """Global question → answer cache for Groq calls (v6).

    Same question from any user is answered instantly without spending
    quota. 30-day TTL.
    """

    __tablename__ = "ai_cache"

    qkey: Mapped[str] = mapped_column(String(64), primary_key=True)
    answer: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AiQuota(Base):
    """Per-user daily AI action counter (v6)."""

    __tablename__ = "ai_quota"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
