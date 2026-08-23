"""One run of one extractor over one file version — the provenance anchor.

Deliberately separate from the `operation` row that carries the same id. The operation is the
queue: terminal rows are pruned once a 10 TB import has left millions behind (12 § queue
hygiene, Q33). The run is the *record*: every derived row will reference it (02 § invariants
#3), so "which extractor version produced this" must stay answerable long after the job is
forgotten.

Nothing is backfilled. Existing file versions get no runs, which is the honest state: they were
registered before any extractor existed, and re-running extraction over them is
[F-009](../../../../features/F-009-reprocessing.md)'s job rather than a migration's.

Check constraints carry their *short* names: the metadata naming convention interpolates
`%(constraint_name)s`, so a fully qualified name would come out doubled.

Revision ID: 0014_extraction_runs
Revises: 0013_extractor_registry
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_extraction_runs"
down_revision: str | None = "0013_extractor_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out rather than imported from `tables.py`: a migration has to keep applying unchanged
#: after the application's vocabularies move on (11 § migrations).
_STATES = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "dead_letter",
    "cancelled",
    "superseded",
)


def upgrade() -> None:
    op.create_table(
        "extraction_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extractor_id", sa.Text(), nullable=False),
        sa.Column("file_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=True),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extraction_run"),
        sa.ForeignKeyConstraint(
            ["extractor_id"],
            ["extractor.id"],
            name="fk_extraction_run_extractor_id_extractor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_version_id"],
            ["file_version.id"],
            name="fk_extraction_run_file_version_id_file_version",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "file_version_id",
            "extractor_id",
            "generation",
            name="uq_extraction_run_file_version_id_extractor_id_generation",
        ),
        sa.CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in _STATES) + ")",
            name="state_known",
        ),
        sa.CheckConstraint("generation >= 1", name="generation_positive"),
    )
    op.create_index("ix_extraction_run_file_version_id", "extraction_run", ["file_version_id"])
    op.create_index(
        "ix_extraction_run_extractor_id_state", "extraction_run", ["extractor_id", "state"]
    )


def downgrade() -> None:
    op.drop_index("ix_extraction_run_extractor_id_state", table_name="extraction_run")
    op.drop_index("ix_extraction_run_file_version_id", table_name="extraction_run")
    op.drop_table("extraction_run")
