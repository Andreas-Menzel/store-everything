"""What a folder's subtree adds up to, and the queue that keeps it current.

[F-015/FR-8](../../../../features/F-015-folders.md) in three objects:

- **`folder_aggregate`** — recursive file count and recursive size per folder, plus when the
  numbers were last checked against ground truth. Its own table rather than columns on `folder`,
  because these are rewritten on every upload while the `folder` row is read by every closure
  join.
- **`folder_delta`** — the outbox. One row per change, written in the same transaction as the
  change, holding the amount to add to that folder *and to every one of its ancestors*.
- **`workspace.aggregates_as_of`** — the moment that workspace's queue was last observed empty.

The backfill is the honest part of this migration: an instance that already holds files would
otherwise start reporting zeros, so every folder's row is computed from ground truth here — the
same query the rotating drift sweep uses, spelled out again because a migration must not depend
on application code that will keep changing.

Check constraints carry their *short* names: the metadata naming convention interpolates
`%(constraint_name)s`, so a fully qualified name would come out doubled.

Revision ID: 0010_folder_aggregates
Revises: 0009_deferrable_file_containment
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_folder_aggregates"
down_revision: str | None = "0009_deferrable_file_containment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Ground truth for every folder at once. `folder_closure` carries a depth-0 row per folder, so
#: grouping by `ancestor_id` covers folders with nothing in them — with `LEFT JOIN` making those
#: an honest zero rather than a missing row.
_BACKFILL = """
INSERT INTO folder_aggregate (folder_id, total_files, total_bytes)
SELECT c.ancestor_id,
       count(f.id),
       coalesce(sum(v.size_bytes), 0)
  FROM folder_closure c
  LEFT JOIN file f
    ON f.folder_id = c.descendant_id
   AND f.state = 'live'
  LEFT JOIN file_version v
    ON v.file_id = f.id
   AND v.is_current
 GROUP BY c.ancestor_id
"""


def upgrade() -> None:
    op.add_column(
        "workspace",
        sa.Column(
            "aggregates_as_of",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "folder_aggregate",
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("total_files", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("folder_id", name="pk_folder_aggregate"),
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["folder.id"],
            name="fk_folder_aggregate_folder_id_folder",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("total_files >= 0", name="total_files_not_negative"),
        sa.CheckConstraint("total_bytes >= 0", name="total_bytes_not_negative"),
    )
    op.create_index("ix_folder_aggregate_verified_at", "folder_aggregate", ["verified_at"])

    op.create_table(
        "folder_delta",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_count", sa.BigInteger(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_folder_delta"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_folder_delta_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["folder.id"],
            name="fk_folder_delta_folder_id_folder",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_folder_delta_workspace_id_id", "folder_delta", ["workspace_id", "id"])
    op.create_index("ix_folder_delta_folder_id", "folder_delta", ["folder_id"])

    op.execute(_BACKFILL)


def downgrade() -> None:
    op.drop_index("ix_folder_delta_folder_id", table_name="folder_delta")
    op.drop_index("ix_folder_delta_workspace_id_id", table_name="folder_delta")
    op.drop_table("folder_delta")

    op.drop_index("ix_folder_aggregate_verified_at", table_name="folder_aggregate")
    op.drop_table("folder_aggregate")

    op.drop_column("workspace", "aggregates_as_of")
