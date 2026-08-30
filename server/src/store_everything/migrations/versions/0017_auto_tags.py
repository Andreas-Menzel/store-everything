"""What a machine claims about a file's tags, and the review a suggestion waits for.

`file_auto_tag` is the fourth derived table and has the same shape as the other three: the
version it describes and the run that produced it
([02 § invariants](../../../../specs/02-domain-model.md#invariants) #3). Two details are the
design rather than the schema:

- **Per version, not per file.** A machine's claim is about bytes, so a new version replaces it;
  a person's word is about the file and outlives every upload — which is why curation stays in
  `file_tag` and reprocessing never touches that table (invariant #4).
- **The run is in the unique key.** Two extractors may both see a cat, and a generation swap of
  one of them must not take the other's word away with it.

`tag` gains the review record a suggestion waits for: which run proposed the word, and who
decided about it ([F-003/FR-12](../../../../features/F-003-tagging.md)). The run reference is
`SET NULL` rather than `CASCADE` — a suggestion outlives the run that made it, because deleting
the word would quietly undo an admin's pending decision.

Nothing is backfilled: no extractor has claimed a tag on any instance yet.

Check constraints carry their *short* names: the metadata naming convention interpolates
`%(constraint_name)s`, so a fully qualified name would come out doubled.

Revision ID: 0017_auto_tags
Revises: 0016_tags
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_auto_tags"
down_revision: str | None = "0016_tags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_auto_tag",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_file_auto_tag"),
        sa.ForeignKeyConstraint(
            ["file_version_id"],
            ["file_version.id"],
            name="fk_file_auto_tag_file_version_id_file_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_run.id"],
            name="fk_file_auto_tag_run_id_extraction_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tag.id"], name="fk_file_auto_tag_tag_id_tag", ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "file_version_id",
            "tag_id",
            "run_id",
            name="uq_file_auto_tag_file_version_id_tag_id_run_id",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_is_a_ratio",
        ),
    )
    op.create_index("ix_file_auto_tag_file_version_id", "file_auto_tag", ["file_version_id"])
    op.create_index("ix_file_auto_tag_tag_id", "file_auto_tag", ["tag_id"])
    op.create_index("ix_file_auto_tag_run_id", "file_auto_tag", ["run_id"])

    op.add_column(
        "tag", sa.Column("suggested_by_run_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("tag", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tag", sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_tag_suggested_by_run_id_extraction_run",
        "tag",
        "extraction_run",
        ["suggested_by_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_tag_reviewed_by_app_user",
        "tag",
        "app_user",
        ["reviewed_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "review_is_whole", "tag", "(reviewed_at IS NULL) = (reviewed_by IS NULL)"
    )


def downgrade() -> None:
    # The short name, like the creation used: the naming convention qualifies it on the way in
    # and would qualify a qualified name twice.
    op.drop_constraint("review_is_whole", "tag", type_="check")
    op.drop_constraint("fk_tag_reviewed_by_app_user", "tag", type_="foreignkey")
    op.drop_constraint("fk_tag_suggested_by_run_id_extraction_run", "tag", type_="foreignkey")
    op.drop_column("tag", "reviewed_by")
    op.drop_column("tag", "reviewed_at")
    op.drop_column("tag", "suggested_by_run_id")
    op.drop_table("file_auto_tag")
