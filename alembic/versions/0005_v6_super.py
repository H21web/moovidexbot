"""v6 super update — AI + personalization tables.

- ``user_prefs``: per-user behavior counters (quality/language/size/codec/
  genre) + personalization toggle. Learned from downloads.
- ``chat_memory``: per-user AI conversation memory (last ~20, 7-day TTL).
- ``ai_cache``: global question -> answer cache for Groq (30-day TTL).
- ``ai_quota``: per-user daily Groq call counter.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_prefs",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("downloads", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("counters", JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "chat_memory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), index=True),
    )
    op.create_table(
        "ai_cache",
        sa.Column("qkey", sa.String(64), primary_key=True),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "ai_quota",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("ai_quota")
    op.drop_table("ai_cache")
    op.drop_table("chat_memory")
    op.drop_table("user_prefs")
