"""v10.9.0 — manual taste overrides on user_prefs.

Non-destructive: adds one nullable JSONB column, touches nothing else.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_prefs",
        sa.Column("manual", postgresql.JSONB(), nullable=True,
                  server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("user_prefs", "manual")
