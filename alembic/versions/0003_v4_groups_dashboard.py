"""v4: analytics events + user warns.

- New ``event_logs`` table (start/download/request analytics).
- ``users.warns`` counter for the warn system (auto-ban at WARN_LIMIT).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_event_logs_kind", "event_logs", ["kind"])
    op.create_index("ix_event_logs_user_id", "event_logs", ["user_id"])
    op.create_index("ix_event_logs_created_at", "event_logs", ["created_at"])
    op.add_column("users",
                  sa.Column("warns", sa.Integer(), nullable=False,
                            server_default="0"))


def downgrade() -> None:
    op.drop_column("users", "warns")
    op.drop_index("ix_event_logs_created_at", "event_logs")
    op.drop_index("ix_event_logs_user_id", "event_logs")
    op.drop_index("ix_event_logs_kind", "event_logs")
    op.drop_table("event_logs")
