"""Initial schema — single squashed revision for the MTProto rewrite.

Covers: files (+pg_trgm), users, groups, movie_requests, search_logs,
tmdb_cache, backfill_jobs, bot_settings.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("file_id", sa.Text(), nullable=False, unique=True),
        sa.Column("file_name", sa.Text()),
        sa.Column("file_size", sa.BigInteger()),
        sa.Column("mime_type", sa.String(128)),
        sa.Column("caption", sa.Text()),
        sa.Column("channel_id", sa.BigInteger(), index=True),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("quality", sa.String(16), index=True),
        sa.Column("language", sa.String(32), index=True),
        sa.Column("title_key", sa.Text(), index=True),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("duration", sa.Integer()),
        sa.Column("views", sa.Integer()),
        sa.Column("forwards", sa.Integer()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.execute(
        "CREATE INDEX ix_files_file_name_trgm ON files "
        "USING gin (file_name gin_trgm_ops)")
    op.execute(
        "CREATE INDEX ix_files_caption_trgm ON files "
        "USING gin (caption gin_trgm_ops)")
    op.execute(
        "CREATE INDEX ix_files_title_key_trgm ON files "
        "USING gin (title_key gin_trgm_ops)")

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("first_name", sa.String(128)),
        sa.Column("username", sa.String(64)),
        sa.Column("is_banned", sa.Boolean(), default=False),
        sa.Column("joined_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "groups",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.String(256)),
        sa.Column("settings", postgresql.JSONB(), server_default="{}"),
        sa.Column("joined_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.create_table(
        "movie_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), index=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), default="open", index=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.create_table(
        "search_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("query", sa.Text()),
        sa.Column("user_id", sa.BigInteger()),
        sa.Column("hits", sa.Integer(), default=0),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), index=True),
    )
    op.create_table(
        "tmdb_cache",
        sa.Column("key", sa.String(256), primary_key=True),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("cached_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.create_table(
        "backfill_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_token", sa.String(32), nullable=False, unique=True),
        sa.Column("channel_ref", sa.String(256)),
        sa.Column("channel_id", sa.BigInteger(), index=True),
        sa.Column("status", sa.String(16), default="pending", index=True),
        sa.Column("offset_id", sa.Integer(), default=0),
        sa.Column("total_scanned", sa.Integer(), default=0),
        sa.Column("total_indexed", sa.Integer(), default=0),
        sa.Column("total_skipped", sa.Integer(), default=0),
        sa.Column("total_errors", sa.Integer(), default=0),
        sa.Column("stats", postgresql.JSONB(), server_default="{}"),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
    )
    op.create_table(
        "bot_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON()),
    )


def downgrade() -> None:
    for table in ("bot_settings", "backfill_jobs", "tmdb_cache",
                  "search_logs", "movie_requests", "groups", "users",
                  "files"):
        op.drop_table(table)
