"""What a re-scan needs to recognise a directory that was renamed rather than replaced.

A directory renamed on the storage looks like a deletion and a creation. The files inside it
already survive that ([F-001/FR-19](../../../../features/F-001-upload-and-import.md)) — they are
matched by content and relocated, keeping their UUIDs. The *folder* does not, and everything a
later phase attaches to a folder — grants, tags, share links — hangs off exactly that UUID
([F-015/FR-7](../../../../features/F-015-folders.md)).

Two records make the recognition possible, and neither can be reconstructed afterwards:

- **`folder.last_seen_at`** — stamped when a directory's *parent* listing accounts for it, the
  same rule that stamps a file. A directory the scan could not read was still listed by its
  parent, so it is seen; one renamed away is not. That difference is what "vanished" means.
- **`scan_relocation`** — where a folder's content went, counted as it moves. Once a file's row
  says its new folder, nothing anywhere says which folder it left.

Two counters on the run report the outcome, because "your folder kept its identity" and "the
evidence was ambiguous, so it did not" are both things a user asking about an import wants told.

Revision ID: 0011_folder_identity
Revises: 0010_folder_aggregates
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_folder_identity"
down_revision: str | None = "0010_folder_aggregates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("folder", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "scan_run",
        sa.Column(
            "folders_transferred", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "scan_run",
        sa.Column(
            "folders_ambiguous", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
    )

    op.create_table(
        "scan_relocation",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("files", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("folders", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "run_id", "from_folder_id", "to_folder_id", name="pk_scan_relocation"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["scan_run.id"],
            name="fk_scan_relocation_run_id_scan_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_folder_id"],
            ["folder.id"],
            name="fk_scan_relocation_from_folder_id_folder",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_folder_id"],
            ["folder.id"],
            name="fk_scan_relocation_to_folder_id_folder",
            ondelete="CASCADE",
        ),
        # The *short* names: the metadata naming convention interpolates `%(constraint_name)s`,
        # so a qualified one comes out doubled.
        sa.CheckConstraint("files >= 0 AND folders >= 0", name="counts_not_negative"),
        sa.CheckConstraint("from_folder_id <> to_folder_id", name="from_is_not_to"),
    )


def downgrade() -> None:
    op.drop_table("scan_relocation")
    op.drop_column("scan_run", "folders_ambiguous")
    op.drop_column("scan_run", "folders_transferred")
    op.drop_column("folder", "last_seen_at")
