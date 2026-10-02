"""v10 — per-user watchlist (saved files).

Non-destructive: creates one new table, touches nothing else.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("file_id", sa.Integer(),
                  sa.ForeignKey("files.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "file_id",
                            name="uq_saved_user_file"),
    )


def downgrade() -> None:
    op.drop_table("saved_files")
