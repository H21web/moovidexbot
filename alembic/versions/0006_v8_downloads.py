"""v8.1 — per-file download counter (drives "most downloaded = best pick").

Non-destructive: adds one integer column with default 0.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "files",
        sa.Column("downloads", sa.Integer(), nullable=False,
                  server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("files", "downloads")
