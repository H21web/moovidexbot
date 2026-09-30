"""Dedupe files on (file_name, file_size) + unique constraint.

Same filename + same size = same file. Reposts get fresh file_ids from
Telegram, so file_id dedup alone misses them. Existing duplicate rows are
removed (earliest kept) before the constraint is created.
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # drop dupes first (keep earliest row), else the unique index fails
    op.execute(
        """
        DELETE FROM files a USING files b
        WHERE a.id > b.id
          AND a.file_name IS NOT NULL
          AND a.file_size IS NOT NULL
          AND a.file_name = b.file_name
          AND a.file_size = b.file_size
        """
    )
    op.create_unique_constraint("uq_files_name_size", "files",
                                ["file_name", "file_size"])


def downgrade() -> None:
    op.drop_constraint("uq_files_name_size", "files", type_="unique")
