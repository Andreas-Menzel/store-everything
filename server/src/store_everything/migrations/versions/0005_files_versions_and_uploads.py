"""Files, their versions, and the resumable-upload session that produces them.

Three tables and one added constraint, each carrying a rule that would otherwise live in
code and be forgotten:

- `uq_folder_id_workspace_id` exists only to be the target of `file`'s **composite** foreign
  key, which makes "a file's workspace is its folder's workspace" (02 § invariant 1)
  structural rather than conventional. Getting that wrong would file rows into another
  workspace's tree — a permission leak, not a typo.
- `uq_file_version_current` is a partial unique index, so "exactly one current version per
  file" is enforced instead of maintained. The obvious alternative — `current_version_id` on
  `file` — is a circular foreign key that has to be deferred.
- `ix_upload_session_expires_at` is partial on `state = 'open'`: the expiry sweep reads only
  live sessions, and a finished upload's row is dead weight in that index.

Check constraints carry their *short* names: the metadata naming convention interpolates
`%(constraint_name)s`, so a fully qualified name would come out doubled.

Revision ID: 0005_files_versions_and_uploads
Revises: 0004_workspaces_and_folders
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from store_everything.names import MAX_NAME_BYTES, MAX_PATH_BYTES
from store_everything.tables import (
    DIGEST_ALGORITHMS,
    FILE_STATES,
    MEDIA_CLASSES,
    UPLOAD_STATES,
    VERSION_ORIGINS,
    one_of,
)

revision: str = "0005_files_versions_and_uploads"
down_revision: str | None = "0004_workspaces_and_folders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_folder_id_workspace_id", "folder", ["id", "workspace_id"])

    op.create_table(
        "file",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'live'"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_file"),
        sa.ForeignKeyConstraint(
            ["folder_id", "workspace_id"],
            ["folder.id", "folder.workspace_id"],
            name="fk_file_folder_id_workspace_id_folder",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("folder_id", "name_key", name="uq_file_folder_id_name_key"),
        sa.CheckConstraint(one_of("state", FILE_STATES), name="state_known"),
        sa.CheckConstraint(
            f"octet_length(name) BETWEEN 1 AND {MAX_NAME_BYTES}", name="name_length"
        ),
    )
    op.create_index("ix_file_workspace_id_state", "file", ["workspace_id", "state"])

    op.create_table(
        "file_version",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "digest_algorithm", sa.Text(), server_default=sa.text("'sha256'"), nullable=False
        ),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("media_class", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_file_version"),
        sa.ForeignKeyConstraint(
            ["file_id"], ["file.id"], name="fk_file_version_file_id_file", ondelete="CASCADE"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="size_not_negative"),
        sa.CheckConstraint(one_of("origin", VERSION_ORIGINS), name="origin_known"),
        sa.CheckConstraint(one_of("media_class", MEDIA_CLASSES), name="media_class_known"),
        sa.CheckConstraint(
            one_of("digest_algorithm", DIGEST_ALGORITHMS), name="digest_algorithm_known"
        ),
    )
    op.create_index(
        "uq_file_version_current",
        "file_version",
        ["file_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index("ix_file_version_content_hash", "file_version", ["content_hash"])

    op.create_table(
        "upload_session",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_path", sa.Text(), nullable=False),
        sa.Column("declared_length", sa.BigInteger(), nullable=True),
        sa.Column("declared_hash", sa.Text(), nullable=True),
        sa.Column("media_type", sa.Text(), nullable=True),
        sa.Column("interop_version", sa.SmallInteger(), nullable=True),
        sa.Column("committed_offset", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_upload_session"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_upload_session_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_upload_session_created_by_app_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"], ["file.id"], name="fk_upload_session_file_id_file", ondelete="SET NULL"
        ),
        sa.CheckConstraint(one_of("state", UPLOAD_STATES), name="state_known"),
        sa.CheckConstraint("committed_offset >= 0", name="offset_not_negative"),
        sa.CheckConstraint(
            "declared_length IS NULL OR committed_offset <= declared_length",
            name="offset_within_length",
        ),
        sa.CheckConstraint(
            f"octet_length(target_path) <= {MAX_PATH_BYTES}", name="target_path_length"
        ),
    )
    op.create_index(
        "ix_upload_session_expires_at",
        "upload_session",
        ["expires_at"],
        postgresql_where=sa.text("state = 'open'"),
    )
    op.create_index(
        "ix_upload_session_workspace_id_created_at",
        "upload_session",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_upload_session_workspace_id_created_at", table_name="upload_session")
    op.drop_index("ix_upload_session_expires_at", table_name="upload_session")
    op.drop_table("upload_session")

    op.drop_index("ix_file_version_content_hash", table_name="file_version")
    op.drop_index("uq_file_version_current", table_name="file_version")
    op.drop_table("file_version")

    op.drop_index("ix_file_workspace_id_state", table_name="file")
    op.drop_table("file")

    op.drop_constraint("uq_folder_id_workspace_id", "folder", type_="unique")
