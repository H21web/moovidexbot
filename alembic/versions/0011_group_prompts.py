"""v10.14.2 — group_prompts: restart-proof pending group-setting replies.

Mirrors ``index_sessions`` (see migration history for that table):
the group panel's "reply in PM" flow kept its pending state in a pure
in-memory dict, so any restart — or a second instance briefly alive —
between the button tap and the admin's reply silently swallowed the
setting (welcome / start message / caption never saved).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_prompts",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("data", JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("group_prompts")
