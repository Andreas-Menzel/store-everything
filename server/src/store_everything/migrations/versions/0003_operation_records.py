"""Operation records: the durable intent row every effectful operation starts as.

One table for all of them (ADR-0013), carrying the state machine, the lease, the fencing
token and the durable schedule that 12-reliability.md specifies.

Check constraints carry their *short* names: the metadata naming convention interpolates
`%(constraint_name)s`, so a fully qualified name would come out doubled.

Revision ID: 0003_operation_records
Revises: 0002_identity_and_events
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from store_everything.tables import MAX_PRIORITY, MIN_PRIORITY, OPERATION_STATES, one_of

revision: str = "0003_operation_records"
down_revision: str | None = "0002_identity_and_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operation",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("2"), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "next_due_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("leased_by", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("subject_type", sa.Text(), nullable=True),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_operation"),
        sa.CheckConstraint(one_of("state", OPERATION_STATES), name="state_known"),
        sa.CheckConstraint(
            f"priority BETWEEN {MIN_PRIORITY} AND {MAX_PRIORITY}", name="priority_in_range"
        ),
        sa.CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        sa.CheckConstraint("attempt >= 0", name="attempt_not_negative"),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR leased_by IS NOT NULL", name="lease_has_an_owner"
        ),
    )
    op.create_index(
        "ix_operation_claimable",
        "operation",
        ["priority", "next_due_at"],
        postgresql_where=sa.text("state IN ('queued', 'running')"),
    )
    op.create_index(
        "uq_operation_idempotency_key",
        "operation",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL AND state = 'queued'"),
    )
    op.create_index("ix_operation_kind_state", "operation", ["kind", "state"])
    op.create_index(
        "ix_operation_subject_type_subject_id", "operation", ["subject_type", "subject_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_operation_subject_type_subject_id", table_name="operation")
    op.drop_index("ix_operation_kind_state", table_name="operation")
    op.drop_index("uq_operation_idempotency_key", table_name="operation")
    op.drop_index("ix_operation_claimable", table_name="operation")
    op.drop_table("operation")
