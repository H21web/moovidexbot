"""v10.8.10 — bot activity log table.

Non-destructive: creates one new table, touches nothing else.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False, index=True),
        sa.Column("user_id", sa.BigInteger(), index=True),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), index=True),
    )


def downgrade() -> None:
    op.drop_table("activity_logs")
