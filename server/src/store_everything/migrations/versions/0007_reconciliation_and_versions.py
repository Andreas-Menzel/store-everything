"""Reconciliation: what a re-scan concludes about files it already knew, and version history.

[F-001/FR-6](../../../../features/F-001-upload-and-import.md) needs four outcomes for a file
the app already knows — unchanged, changed, moved, gone — and two of them need somewhere to
write the conclusion down:

- `file_version.restorable` is [F-007/FR-9](../../../../features/F-007-versioning.md)'s
  "option b": a version the app snapshotted into `versions/` can be restored; one that was
  overwritten directly on the storage cannot, and says so instead of failing when tried.
- `trash_entry` is [F-014](../../../../features/F-014-deletion-and-trash.md)'s data model, as
  far as phase 1 needs it: a deletion that already happened on disk becomes a trash entry with
  a deadline, never a silent drop from the index. Nothing purges yet.

Two changes are less obvious and load-bearing:

- **`file`'s sibling uniqueness becomes partial**, covering `live` rows only. Otherwise a file
  deleted on the storage keeps its name reserved by its own trash entry, and re-uploading it
  would be refused as a collision with something that is not there — while F-014/FR-1 says a
  trashed item does not reserve its path.
- **`scan_blocked`** is how F-001/FR-16 is enforced rather than promised: a directory the scan
  could not read is recorded, and the sweep excludes its whole subtree. Trashing the contents
  of a share that failed to mount is the worst bug this feature could have.

Check constraints carry their *short* names: the metadata naming convention interpolates
`%(constraint_name)s`, so a fully qualified name would come out doubled — on the way out as
well as in.

Revision ID: 0007_reconciliation_and_versions
Revises: 0006_workspace_scans
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from store_everything.names import MAX_PATH_BYTES
from store_everything.tables import TRASH_ORIGINS, UPLOAD_CONFLICT_MODES, one_of

revision: str = "0007_reconciliation_and_versions"
down_revision: str | None = "0006_workspace_scans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every counter reconciliation adds to a run, all shaped the same way.
_RUN_COUNTERS = ("files_changed", "files_moved", "files_trashed", "files_restored")


def upgrade() -> None:
    op.add_column(
        "file_version",
        sa.Column("restorable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )

    # Live rows only, so a trash entry stops reserving the path it used to hold.
    op.drop_constraint("uq_file_folder_id_name_key", "file", type_="unique")
    op.create_index(
        "uq_file_live_name",
        "file",
        ["folder_id", "name_key"],
        unique=True,
        postgresql_where=sa.text("state = 'live'"),
    )
    op.create_index("ix_file_folder_id_last_seen_at", "file", ["folder_id", "last_seen_at"])

    op.create_table(
        "trash_entry",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column(
            "trashed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("trashed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trash_entry"),
        sa.ForeignKeyConstraint(
            ["file_id"], ["file.id"], name="fk_trash_entry_file_id_file", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["trashed_by"],
            ["app_user.id"],
            name="fk_trash_entry_trashed_by_app_user",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("file_id", name="uq_trash_entry_file_id"),
        sa.CheckConstraint(one_of("origin", TRASH_ORIGINS), name="origin_known"),
        sa.CheckConstraint(f"octet_length(path) <= {MAX_PATH_BYTES}", name="path_length"),
    )
    op.create_index("ix_trash_entry_batch_id", "trash_entry", ["batch_id"])
    op.create_index("ix_trash_entry_purge_after", "trash_entry", ["purge_after"])

    op.create_table(
        "scan_blocked",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("run_id", "folder_id", name="pk_scan_blocked"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["scan_run.id"], name="fk_scan_blocked_run_id_scan_run", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["folder.id"],
            name="fk_scan_blocked_folder_id_folder",
            ondelete="CASCADE",
        ),
    )

    for counter in _RUN_COUNTERS:
        op.add_column(
            "scan_run",
            sa.Column(counter, sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        )

    op.add_column(
        "upload_session",
        sa.Column("if_exists", sa.Text(), server_default=sa.text("'reject'"), nullable=False),
    )
    op.create_check_constraint(
        "if_exists_known", "upload_session", one_of("if_exists", UPLOAD_CONFLICT_MODES)
    )


def downgrade() -> None:
    op.drop_constraint("if_exists_known", "upload_session", type_="check")
    op.drop_column("upload_session", "if_exists")

    for counter in reversed(_RUN_COUNTERS):
        op.drop_column("scan_run", counter)

    op.drop_table("scan_blocked")

    op.drop_index("ix_trash_entry_purge_after", table_name="trash_entry")
    op.drop_index("ix_trash_entry_batch_id", table_name="trash_entry")
    op.drop_table("trash_entry")

    op.drop_index("ix_file_folder_id_last_seen_at", table_name="file")
    op.drop_index("uq_file_live_name", table_name="file")
    # Going back means uniqueness covers every state again. A trashed file whose path was
    # since taken by a new one is a legal pair *here* and an illegal one *there*, so this
    # statement can fail on a database that has such a pair — deliberately, rather than
    # deleting a user's trash to make a rollback quieter. Purging the colliding entry is the
    # operator's fix, and the failure names the row.
    op.create_unique_constraint("uq_file_folder_id_name_key", "file", ["folder_id", "name_key"])

    op.drop_column("file_version", "restorable")
