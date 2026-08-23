"""The extractor registry: who may analyse files, with which credential, producing which kinds.

Three tables for [ADR-0020](../../../../decisions/ADR-0020-extractor-dispatch-and-wire-protocol.md):

- **`extractor`** — one row per extractor id, keyed by that id rather than by a surrogate UUID
  (the id is already the text the operation kind, the admin URL and every provenance stamp are
  written in). Manifest columns are nullable because an admin provisions the id *before* the
  container exists to describe itself; a row with no manifest is an extractor that has never
  started, not a broken one.
- **`extractor_token`** — the per-extractor bearer credential, stored as a SHA-256 like every
  other token (07 § tokens & credentials).
- **`extractor_claim`** — the single-provider rule, made structural: the primary key over
  *(claim_type, kind)* is what makes two producers of `searchable-pdf` impossible, so the
  question of which one wins never needs an answer.

Nothing is backfilled: on an existing instance the registry starts empty, which is exactly
right — no extractor has been installed yet.

Check constraints carry their *short* names: the metadata naming convention interpolates
`%(constraint_name)s`, so a fully qualified name would come out doubled.

Revision ID: 0013_extractor_registry
Revises: 0012_folder_device
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_extractor_registry"
down_revision: str | None = "0012_folder_device"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kept as literal text rather than imported from `tables.py`: a migration must keep applying
#: unchanged after the application's vocabularies move on (11 § migrations).
_SLUG = "^[a-z0-9]+(-[a-z0-9]+)*$"


def upgrade() -> None:
    op.create_table(
        "extractor",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("api_version", sa.Text(), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("cost_class", sa.Text(), nullable=True),
        sa.Column("gpu", sa.Text(), nullable=True),
        sa.Column("network", sa.Text(), nullable=True),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_extractor"),
        sa.CheckConstraint(f"id ~ '{_SLUG}'", name="id_slug"),
        sa.CheckConstraint("length(id) BETWEEN 1 AND 64", name="id_length"),
        sa.CheckConstraint(
            "(manifest IS NULL) = (version IS NULL)"
            " AND (manifest IS NULL) = (api_version IS NULL)"
            " AND (manifest IS NULL) = (registered_at IS NULL)",
            name="registration_is_all_or_nothing",
        ),
        sa.CheckConstraint(
            "cost_class IS NULL OR cost_class IN ('light', 'medium', 'heavy')",
            name="cost_class_known",
        ),
        sa.CheckConstraint(
            "gpu IS NULL OR gpu IN ('none', 'optional', 'required')", name="gpu_known"
        ),
        sa.CheckConstraint(
            "network IS NULL OR network IN ('none', 'outbound')", name="network_known"
        ),
    )

    op.create_table(
        "extractor_token",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extractor_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_extractor_token"),
        sa.ForeignKeyConstraint(
            ["extractor_id"],
            ["extractor.id"],
            name="fk_extractor_token_extractor_id_extractor",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="uq_extractor_token_token_hash"),
        sa.UniqueConstraint("extractor_id", "name", name="uq_extractor_token_extractor_id_name"),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 100", name="name_length"),
    )

    op.create_table(
        "extractor_claim",
        sa.Column("claim_type", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("extractor_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("claim_type", "kind", name="pk_extractor_claim"),
        sa.ForeignKeyConstraint(
            ["extractor_id"],
            ["extractor.id"],
            name="fk_extractor_claim_extractor_id_extractor",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "claim_type IN ('rendition', 'derived_asset', 'embedding_space')",
            name="claim_type_known",
        ),
        sa.CheckConstraint(f"kind ~ '{_SLUG}'", name="kind_slug"),
        sa.CheckConstraint("length(kind) BETWEEN 1 AND 64", name="kind_length"),
    )
    op.create_index("ix_extractor_claim_extractor_id", "extractor_claim", ["extractor_id"])


def downgrade() -> None:
    op.drop_index("ix_extractor_claim_extractor_id", table_name="extractor_claim")
    op.drop_table("extractor_claim")
    op.drop_table("extractor_token")
    op.drop_table("extractor")
