"""Workspace scans: the run, its durable cursor, and what it reports instead of registering.

The shape here is a crawler's, and deliberately so
([12 § job atomicity](../../../../specs/12-reliability.md#job-atomicity)): a scan is one
logical operation too big to be atomic, so it checkpoints. `scan_frontier` **is**
the cursor — a table of directories discovered and not yet processed. One batch pops a
directory, registers what is in it, pushes its subdirectories and deletes its own row, all in
one transaction, so a `kill -9` costs at most one directory's work and no bookkeeping.

Two columns join existing tables for the same reason they exist at all:

- `workspace.scan_interval_minutes` — ADR-0019's per-workspace schedule, hourly by default.
- `file.last_seen_at` — what a scan stamps on every file it finds. "What did this run not
  see" is then one indexed comparison against the run's start instead of a temporary table
  with a row per file, and it survives a run being interrupted.

Two partial indexes carry rules no column can: `uq_scan_run_active` allows one running scan
per workspace (a second traversal would double the IO and race its own registrations), and
`uq_scan_run_operation_id` is what makes a re-claimed operation resume *its* run.

Check constraints carry their *short* names: the metadata naming convention interpolates
`%(constraint_name)s`, so a fully qualified name would come out doubled.

Revision ID: 0006_workspace_scans
Revises: 0005_files_versions_and_uploads
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from store_everything.names import MAX_PATH_BYTES
from store_everything.tables import (
    SCAN_FINDING_KINDS,
    SCAN_STATES,
    SCAN_TRIGGERS,
    one_of,
)

revision: str = "0006_workspace_scans"
down_revision: str | None = "0005_files_versions_and_uploads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspace",
        sa.Column(
            "scan_interval_minutes", sa.Integer(), server_default=sa.text("60"), nullable=False
        ),
    )
    # The *short* name here too: `create_check_constraint` renders through the same naming
    # convention as a table's own constraints, so a qualified name comes out doubled.
    op.create_check_constraint("scan_interval_positive", "workspace", "scan_interval_minutes > 0")
    op.add_column("file", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "scan_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("root_path", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "directories_scanned", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("files_seen", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("files_registered", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("conflicts", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("skipped", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_scan_run"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_scan_run_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("operation_id", name="uq_scan_run_operation_id"),
        sa.CheckConstraint(one_of("trigger", SCAN_TRIGGERS), name="trigger_known"),
        sa.CheckConstraint(one_of("state", SCAN_STATES), name="state_known"),
    )
    op.create_index(
        "ix_scan_run_workspace_id_started_at", "scan_run", ["workspace_id", "started_at"]
    )
    op.create_index(
        "uq_scan_run_active",
        "scan_run",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("state = 'running'"),
    )

    op.create_table(
        "scan_frontier",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("run_id", "path", name="pk_scan_frontier"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["scan_run.id"], name="fk_scan_frontier_run_id_scan_run", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["folder.id"],
            name="fk_scan_frontier_folder_id_folder",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(f"octet_length(path) <= {MAX_PATH_BYTES}", name="path_length"),
    )

    op.create_table(
        "scan_finding",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scan_finding"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["scan_run.id"], name="fk_scan_finding_run_id_scan_run", ondelete="CASCADE"
        ),
        sa.CheckConstraint(one_of("kind", SCAN_FINDING_KINDS), name="kind_known"),
    )
    op.create_index("ix_scan_finding_run_id_id", "scan_finding", ["run_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_scan_finding_run_id_id", table_name="scan_finding")
    op.drop_table("scan_finding")

    op.drop_table("scan_frontier")

    op.drop_index("uq_scan_run_active", table_name="scan_run")
    op.drop_index("ix_scan_run_workspace_id_started_at", table_name="scan_run")
    op.drop_table("scan_run")

    op.drop_column("file", "last_seen_at")
    # The short name again: the naming convention renders on the way out as well as in.
    op.drop_constraint("scan_interval_positive", "workspace", type_="check")
    op.drop_column("workspace", "scan_interval_minutes")
