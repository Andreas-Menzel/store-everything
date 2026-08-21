"""Workspaces and the folder tree, with its ancestry closure.

A workspace is a root directory plus who owns it, which placement chose the path, and the
`fs-check` verdict that admitted it (ADR-0018, ADR-0019). Its folders mirror the directory
tree 1:1 and carry their own identity (F-015), with ancestry precomputed in a closure table
so "everything under folder F" is one indexed join.

Two indexes deserve a note, because both encode a rule no column can:

- `uq_folder_workspace_root` is partial (`WHERE parent_id IS NULL`) because PostgreSQL treats
  NULLs as distinct, so the composite unique constraint cannot stop a second root folder.
- `uq_workspace_root_path` catches only *equal* roots. Overlap — one root inside another — is
  a relation between rows and is checked under an advisory lock in `workspaces.py`.

Check constraints carry their *short* names: the metadata naming convention interpolates
`%(constraint_name)s`, so a fully qualified name would come out doubled.

Revision ID: 0004_workspaces_and_folders
Revises: 0003_operation_records
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from store_everything.names import MAX_NAME_BYTES, MAX_PATH_BYTES
from store_everything.tables import (
    WORKSPACE_PLACEMENTS,
    WORKSPACE_SOURCES,
    WORKSPACE_STATES,
    one_of,
)

revision: str = "0004_workspaces_and_folders"
down_revision: str | None = "0003_operation_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), server_default=sa.text("'local'"), nullable=False),
        sa.Column("placement", sa.Text(), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'provisioning'"), nullable=False),
        sa.Column("fs_check", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "fs_checked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace"),
        # RESTRICT: a user owns files, so removing an account is a data-lifecycle decision
        # (phase 4), never a foreign key's side effect.
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["app_user.id"],
            name="fk_workspace_owner_id_app_user",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("owner_id", "name_key", name="uq_workspace_owner_id_name_key"),
        sa.UniqueConstraint("root_path", name="uq_workspace_root_path"),
        sa.CheckConstraint(one_of("source", WORKSPACE_SOURCES), name="source_known"),
        sa.CheckConstraint(one_of("placement", WORKSPACE_PLACEMENTS), name="placement_known"),
        sa.CheckConstraint(one_of("state", WORKSPACE_STATES), name="state_known"),
        sa.CheckConstraint(
            f"octet_length(name) BETWEEN 1 AND {MAX_NAME_BYTES}", name="name_length"
        ),
        sa.CheckConstraint(
            f"octet_length(name_key) BETWEEN 1 AND {MAX_NAME_BYTES}", name="key_length"
        ),
        sa.CheckConstraint("root_path LIKE '/%'", name="root_path_absolute"),
        sa.CheckConstraint(f"octet_length(root_path) <= {MAX_PATH_BYTES}", name="root_path_length"),
    )
    op.create_index("ix_workspace_owner_id_created_at", "workspace", ["owner_id", "created_at"])

    op.create_table(
        "folder",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_folder"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_folder_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["folder.id"], name="fk_folder_parent_id_folder", ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "parent_id",
            "name_key",
            name="uq_folder_workspace_id_parent_id_name_key",
        ),
        sa.CheckConstraint("(parent_id IS NULL) = (name = '')", name="root_has_no_name"),
        sa.CheckConstraint("(parent_id IS NULL) = (depth = 0)", name="depth_matches_parent"),
        sa.CheckConstraint(f"octet_length(name) <= {MAX_NAME_BYTES}", name="name_length"),
    )
    op.create_index(
        "uq_folder_workspace_root",
        "folder",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
    )
    op.create_index("ix_folder_parent_id", "folder", ["parent_id"])

    op.create_table(
        "folder_closure",
        sa.Column("ancestor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("descendant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("ancestor_id", "descendant_id", name="pk_folder_closure"),
        sa.ForeignKeyConstraint(
            ["ancestor_id"],
            ["folder.id"],
            name="fk_folder_closure_ancestor_id_folder",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["descendant_id"],
            ["folder.id"],
            name="fk_folder_closure_descendant_id_folder",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("depth >= 0", name="depth_not_negative"),
        sa.CheckConstraint(
            "(ancestor_id = descendant_id) = (depth = 0)", name="self_row_at_depth_zero"
        ),
    )
    op.create_index(
        "ix_folder_closure_descendant_id_depth", "folder_closure", ["descendant_id", "depth"]
    )


def downgrade() -> None:
    op.drop_index("ix_folder_closure_descendant_id_depth", table_name="folder_closure")
    op.drop_table("folder_closure")

    op.drop_index("ix_folder_parent_id", table_name="folder")
    op.drop_index("uq_folder_workspace_root", table_name="folder")
    op.drop_table("folder")

    op.drop_index("ix_workspace_owner_id_created_at", table_name="workspace")
    op.drop_table("workspace")
