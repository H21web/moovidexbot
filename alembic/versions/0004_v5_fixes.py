"""v5 fixes.

- Drop ``files.width`` / ``files.height`` / ``files.duration`` — Telegram
  only sends those attributes for videos, documents carry none, so the
  columns were dead weight.
- New ``index_sessions`` table backing the interactive /index setup flow
  (in-memory ``state.pending_*`` is only an L1 cache now — survives
  restarts and multi-instance deploys).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # P2#30: plain drop_column — batch_alter_table on PG recreates the
    # files table, risking trigram index loss.
    op.drop_column("files", "width")
    op.drop_column("files", "height")
    op.drop_column("files", "duration")
    op.create_table(
        "index_sessions",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("data", JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("index_sessions")
    op.add_column("files", sa.Column("width", sa.Integer(), nullable=True))
    op.add_column("files", sa.Column("height", sa.Integer(), nullable=True))
    op.add_column("files", sa.Column("duration", sa.Integer(), nullable=True))
