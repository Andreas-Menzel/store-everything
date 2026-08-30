"""What an extraction run was *asked for*, when somebody asked for something specific.

Eager generation covers what every file needs. On-demand generation covers what somebody is
waiting for — page 40 of a 300-page PDF, a rendition behind a button
([09 § generation policy](../../../../specs/09-previews.md#generation-policy)) — and those
requests are separate pieces of work over the same input. Without a discriminator the second one
would converge onto the first: the unique key is *(version, extractor, generation, input)*, and
two page requests match on all four.

So a run gains `variant`: NULL for routing's own jobs, `page:3` for a request. It joins the
unique key (which is already `NULLS NOT DISTINCT`, so the ordinary case keeps behaving) and the
idempotency key, which means asking for page 3 twice still converges on one job — the caching
[F-028/FR-7](../../../../features/F-028-thumbnails-and-previews.md) asks for, upstream of the
cache itself.

Nothing is backfilled: every existing run is routing's, and NULL says exactly that.

Revision ID: 0018_run_variants
Revises: 0017_auto_tags
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_run_variants"
down_revision: str | None = "0017_auto_tags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNIQUE = "uq_extraction_run_one_per_input"


def upgrade() -> None:
    op.add_column("extraction_run", sa.Column("variant", sa.Text(), nullable=True))
    op.drop_constraint(_UNIQUE, "extraction_run", type_="unique")
    # `NULLS NOT DISTINCT` has no Alembic operation, so the DDL is written out — the same reason
    # migration 0015 spells this constraint by hand.
    op.execute(
        f"ALTER TABLE extraction_run ADD CONSTRAINT {_UNIQUE} "
        "UNIQUE NULLS NOT DISTINCT "
        "(file_version_id, extractor_id, generation, input_asset_id, variant)"
    )


def downgrade() -> None:
    op.drop_constraint(_UNIQUE, "extraction_run", type_="unique")
    op.execute(
        f"ALTER TABLE extraction_run ADD CONSTRAINT {_UNIQUE} "
        "UNIQUE NULLS NOT DISTINCT (file_version_id, extractor_id, generation, input_asset_id)"
    )
    op.drop_column("extraction_run", "variant")
