"""The tag vocabulary and what carries a tag.

Six tables for one idea (ADR-0006): tags are a global DAG, a file or folder carries only the
**most specific** ones, and breadth is a query-time expansion over a precomputed closure. No
ancestor is ever written onto a file, so restructuring the taxonomy touches zero file rows.

Two shapes are worth reading twice:

- **`tag` has no name.** Every spelling lives in `tag_name`, keyed by its folded form, so a
  canonical name and a synonym cannot both claim `car` — they would be the same primary key.
  Two tables would need a cross-table check no constraint can express, and the case it would
  let through is the bad one: `car` meaning two things, resolved by whichever query ran first.
- **`tag_closure` is keyed by the pair, not by the path.** This graph is a DAG, so two tags can
  be connected by paths of different lengths; `depth` is the shortest, because expansion asks
  whether the row exists and the number only renders a breadcrumb.

`file_tag` holds **user curation only** — `manual`, `confirmed`, `rejected`. A machine's claim
is derived data stamped with its run and gets its own table with the other derived rows, which
is what makes [02 § invariants](../../../../specs/02-domain-model.md#invariants) #4 structural:
the path that replaces a generation's output has no reason to name this table.

Nothing is backfilled: no instance has a vocabulary yet.

Check constraints carry their *short* names: the metadata naming convention interpolates
`%(constraint_name)s`, so a fully qualified name would come out doubled.

Revision ID: 0016_tags
Revises: 0015_extraction_results
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_tags"
down_revision: str | None = "0015_extraction_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out rather than imported: a migration has to keep applying unchanged after the
#: application's vocabularies move on (11 § migrations).
_STATUSES = ("active", "suggested", "rejected")
_CURATION_STATES = ("manual", "confirmed", "rejected")
_MAX_NAME_LENGTH = 100


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "tag",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tag"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_tag_created_by_app_user",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_in("status", _STATUSES), name="status_known"),
    )
    op.create_index("ix_tag_status", "tag", ["status"])

    op.create_table(
        "tag_name",
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_alias", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("name_key", name="pk_tag_name"),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tag.id"], name="fk_tag_name_tag_id_tag", ondelete="CASCADE"
        ),
        sa.CheckConstraint(f"length(name) BETWEEN 1 AND {_MAX_NAME_LENGTH}", name="name_length"),
        sa.CheckConstraint("length(name_key) >= 1", name="key_present"),
    )
    # One canonical name per tag; every other spelling of it is an alias.
    op.create_index(
        "uq_tag_name_canonical",
        "tag_name",
        ["tag_id"],
        unique=True,
        postgresql_where=sa.text("NOT is_alias"),
    )
    # `LIKE 'inv%'` reaches a btree only through a collation-independent operator class, and
    # completion runs on every keystroke.
    op.create_index(
        "ix_tag_name_prefix",
        "tag_name",
        ["name_key"],
        postgresql_ops={"name_key": "text_pattern_ops"},
    )
    op.create_index("ix_tag_name_tag_id", "tag_name", ["tag_id"])

    op.create_table(
        "tag_edge",
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("parent_id", "child_id", name="pk_tag_edge"),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["tag.id"], name="fk_tag_edge_parent_id_tag", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["child_id"], ["tag.id"], name="fk_tag_edge_child_id_tag", ondelete="CASCADE"
        ),
        sa.CheckConstraint("parent_id <> child_id", name="no_self_parent"),
    )
    op.create_index("ix_tag_edge_child_id", "tag_edge", ["child_id"])

    op.create_table(
        "tag_closure",
        sa.Column("ancestor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("descendant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("ancestor_id", "descendant_id", name="pk_tag_closure"),
        sa.ForeignKeyConstraint(
            ["ancestor_id"], ["tag.id"], name="fk_tag_closure_ancestor_id_tag", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["descendant_id"],
            ["tag.id"],
            name="fk_tag_closure_descendant_id_tag",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("depth >= 0", name="depth_not_negative"),
        sa.CheckConstraint(
            "(ancestor_id = descendant_id) = (depth = 0)", name="self_row_at_depth_zero"
        ),
    )
    op.create_index("ix_tag_closure_descendant_id", "tag_closure", ["descendant_id"])

    op.create_table(
        "file_tag",
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("file_id", "tag_id", name="pk_file_tag"),
        sa.ForeignKeyConstraint(
            ["file_id"], ["file.id"], name="fk_file_tag_file_id_file", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tag.id"], name="fk_file_tag_tag_id_tag", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name="fk_file_tag_user_id_app_user", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(_in("provenance", _CURATION_STATES), name="provenance_known"),
    )
    op.create_index("ix_file_tag_tag_id", "file_tag", ["tag_id"])

    op.create_table(
        "folder_tag",
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("folder_id", "tag_id", name="pk_folder_tag"),
        sa.ForeignKeyConstraint(
            ["folder_id"], ["folder.id"], name="fk_folder_tag_folder_id_folder", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tag.id"], name="fk_folder_tag_tag_id_tag", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name="fk_folder_tag_user_id_app_user", ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_folder_tag_tag_id", "folder_tag", ["tag_id"])


def downgrade() -> None:
    op.drop_table("folder_tag")
    op.drop_table("file_tag")
    op.drop_table("tag_closure")
    op.drop_table("tag_edge")
    op.drop_table("tag_name")
    op.drop_table("tag")
