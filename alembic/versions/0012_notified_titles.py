"""v10.15.8 — notified_titles: atomic dedup for new-title announcements.

Nova-Filter-Bot's notified_media pattern: the UNIQUE constraints turn
"is this title new?" into one atomic INSERT ... ON CONFLICT DO NOTHING,
so concurrent live posts can never double-announce the same movie.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notified_titles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title_key", sa.Text(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("announced_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("title_key", "year",
                            name="uq_notified_title_year"),
        sa.UniqueConstraint("provider_id",
                            name="uq_notified_provider_id"),
    )


def downgrade() -> None:
    op.drop_table("notified_titles")
