"""What extraction produces: typed facts, positioned text, and generated files.

Three tables sharing one shape — the version they describe and the run that produced them. That
second reference is [02 § invariants](../../../../specs/02-domain-model.md#invariants) #3 made
structural: every derived row can name the extractor, version, model and generation behind it,
because it points at the run that carries them. All three cascade from their run, because derived
data is regenerable by definition (invariant #5) and a run without its outputs would be a lie.

The `extraction_run` changes are the other half of chaining. A job over a *derived* input — a
keyframe, a converted PDF — is a different piece of work from a job over the file's own bytes, so
the input joins the key: one video's fifty keyframes are fifty jobs for one extractor rather than
one. `NULLS NOT DISTINCT` is what keeps the ordinary case correct at the same time, since two runs
over a file's own bytes both carry a NULL there and PostgreSQL would otherwise call them
different. `reused_from` records the case where the rows were copied from an earlier run over
byte-identical content instead of computed (F-009/FR-8).

Nothing is backfilled: no extractor has produced anything on any instance yet.

Check constraints carry their *short* names: the metadata naming convention interpolates
`%(constraint_name)s`, so a fully qualified name would come out doubled.

Revision ID: 0015_extraction_results
Revises: 0014_extraction_runs
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_extraction_results"
down_revision: str | None = "0014_extraction_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out rather than imported: a migration has to keep applying unchanged after the
#: application's vocabularies move on (11 § migrations).
_VALUE_TYPES = (
    "string",
    "text",
    "integer",
    "float",
    "boolean",
    "datetime",
    "date",
    "duration",
    "geo",
    "json",
)
_ANCHOR_KINDS = ("page", "time", "line", "sheet", "region", "whole")
_OLD_RUN_UNIQUE = "uq_extraction_run_file_version_id_extractor_id_generation"
#: Named by hand: the convention would generate 71 characters and PostgreSQL truncates at 63.
_NEW_RUN_UNIQUE = "uq_extraction_run_one_per_input"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "derived_asset",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "digest_algorithm", sa.Text(), server_default=sa.text("'sha256'"), nullable=False
        ),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("rendition_kind", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_derived_asset"),
        sa.ForeignKeyConstraint(
            ["file_version_id"],
            ["file_version.id"],
            name="fk_derived_asset_file_version_id_file_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_run.id"],
            name="fk_derived_asset_run_id_extraction_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "file_version_id",
            "name",
            "generation",
            name="uq_derived_asset_file_version_id_name_generation",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="size_not_negative"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 64", name="name_length"),
        sa.CheckConstraint("digest_algorithm IN ('sha256')", name="digest_algorithm_known"),
    )
    op.create_index(
        "ix_derived_asset_file_version_id_kind", "derived_asset", ["file_version_id", "kind"]
    )
    op.create_index("ix_derived_asset_run_id", "derived_asset", ["run_id"])
    op.create_index("ix_derived_asset_content_hash", "derived_asset", ["content_hash"])

    op.create_table(
        "metadata_entry",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value_type", sa.Text(), nullable=False),
        sa.Column("provenance", sa.Text(), server_default=sa.text("'auto'"), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_number", sa.Float(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("value_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value_latitude", sa.Float(), nullable=True),
        sa.Column("value_longitude", sa.Float(), nullable=True),
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_metadata_entry"),
        sa.ForeignKeyConstraint(
            ["file_version_id"],
            ["file_version.id"],
            name="fk_metadata_entry_file_version_id_file_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_run.id"],
            name="fk_metadata_entry_run_id_extraction_run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_in("value_type", _VALUE_TYPES), name="value_type_known"),
        sa.CheckConstraint("provenance IN ('auto', 'manual')", name="provenance_known"),
        sa.CheckConstraint("length(key) BETWEEN 1 AND 100", name="key_length"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_is_a_ratio",
        ),
        sa.CheckConstraint(
            "(provenance = 'auto') = (run_id IS NOT NULL)", name="auto_values_name_their_run"
        ),
    )
    op.create_index(
        "ix_metadata_entry_file_version_id_key", "metadata_entry", ["file_version_id", "key"]
    )
    op.create_index("ix_metadata_entry_key_value_text", "metadata_entry", ["key", "value_text"])
    op.create_index("ix_metadata_entry_key_value_number", "metadata_entry", ["key", "value_number"])
    op.create_index("ix_metadata_entry_key_value_time", "metadata_entry", ["key", "value_time"])
    op.create_index("ix_metadata_entry_run_id", "metadata_entry", ["run_id"])
    op.create_index(
        "ix_metadata_entry_value_latitude_value_longitude",
        "metadata_entry",
        ["value_latitude", "value_longitude"],
    )

    op.create_table(
        "segment",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("anchor_kind", sa.Text(), nullable=False),
        sa.Column(
            "anchor",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_segment"),
        sa.ForeignKeyConstraint(
            ["file_version_id"],
            ["file_version.id"],
            name="fk_segment_file_version_id_file_version",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_run.id"],
            name="fk_segment_run_id_extraction_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_segment_run_id_ordinal"),
        sa.CheckConstraint(_in("anchor_kind", _ANCHOR_KINDS), name="anchor_kind_known"),
        sa.CheckConstraint("ordinal >= 0", name="ordinal_not_negative"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_is_a_ratio",
        ),
    )
    op.create_index("ix_segment_file_version_id_ordinal", "segment", ["file_version_id", "ordinal"])

    # The run's two new facts, and the key they change. `derived_asset` has to exist first: a
    # chained run points at the asset it is about.
    op.add_column(
        "extraction_run", sa.Column("input_asset_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "extraction_run", sa.Column("reused_from", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_extraction_run_input_asset_id_derived_asset",
        "extraction_run",
        "derived_asset",
        ["input_asset_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_extraction_run_reused_from_extraction_run",
        "extraction_run",
        "extraction_run",
        ["reused_from"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(_OLD_RUN_UNIQUE, "extraction_run", type_="unique")
    # `NULLS NOT DISTINCT` has no Alembic operation, so the DDL is written out. Without it two
    # runs over a file's own bytes — both with a NULL input — would not conflict, and the
    # idempotence of routing would quietly stop being structural.
    op.execute(
        f"ALTER TABLE extraction_run ADD CONSTRAINT {_NEW_RUN_UNIQUE} "
        "UNIQUE NULLS NOT DISTINCT (file_version_id, extractor_id, generation, input_asset_id)"
    )


def downgrade() -> None:
    op.drop_constraint(_NEW_RUN_UNIQUE, "extraction_run", type_="unique")
    op.create_unique_constraint(
        _OLD_RUN_UNIQUE, "extraction_run", ["file_version_id", "extractor_id", "generation"]
    )
    op.drop_constraint(
        "fk_extraction_run_reused_from_extraction_run", "extraction_run", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_extraction_run_input_asset_id_derived_asset", "extraction_run", type_="foreignkey"
    )
    op.drop_column("extraction_run", "reused_from")
    op.drop_column("extraction_run", "input_asset_id")

    op.drop_index("ix_segment_file_version_id_ordinal", table_name="segment")
    op.drop_table("segment")

    op.drop_index("ix_metadata_entry_value_latitude_value_longitude", table_name="metadata_entry")
    op.drop_index("ix_metadata_entry_run_id", table_name="metadata_entry")
    op.drop_index("ix_metadata_entry_key_value_time", table_name="metadata_entry")
    op.drop_index("ix_metadata_entry_key_value_number", table_name="metadata_entry")
    op.drop_index("ix_metadata_entry_key_value_text", table_name="metadata_entry")
    op.drop_index("ix_metadata_entry_file_version_id_key", table_name="metadata_entry")
    op.drop_table("metadata_entry")

    op.drop_index("ix_derived_asset_content_hash", table_name="derived_asset")
    op.drop_index("ix_derived_asset_run_id", table_name="derived_asset")
    op.drop_index("ix_derived_asset_file_version_id_kind", table_name="derived_asset")
    op.drop_table("derived_asset")
