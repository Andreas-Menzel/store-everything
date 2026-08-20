"""Baseline: PostgreSQL extensions the single datastore is built on.

No domain tables — those arrive with the features that own them. What is created here is
the datastore capability ADR-0001 commits to: vector search and trigram name search.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
