"""v10.13 — sharding: drop saved_files.file_id foreign key.

``saved_files.file_id`` now stores the *global* (shard-packed) file id —
the File row may live on any shard, so a database-level foreign key is
impossible. The column stays (BigInteger, indexed); only the constraint
goes. Application code resolves the owning shard via
``app.db_shard.decode_gid``.

Safe on fresh databases too: 0007 creates the FK, this drops it.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "saved_files_file_id_fkey", "saved_files", type_="foreignkey"
    )


def downgrade() -> None:
    op.create_foreign_key(
        "saved_files_file_id_fkey",
        "saved_files",
        "files",
        ["file_id"],
        ["id"],
        ondelete="CASCADE",
    )
