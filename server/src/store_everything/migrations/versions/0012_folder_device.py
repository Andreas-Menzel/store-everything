"""Which filesystem each directory was on when a scan last listed it.

The evidence `.workspace/marker` cannot provide. The marker proves the workspace *root* is
mounted (F-001/FR-17), but a mount point below the root that lost its mount is not an error and
not an absence: it is the empty directory that was always underneath it, on the parent's
filesystem, listing perfectly. Nothing in a listing distinguishes that from a directory somebody
emptied, so the whole indexed subtree would be reconciled as deleted.

`st_dev` is that distinction, and one number per directory is enough for it — recorded on every
listing that agrees with what is under it, and deliberately *left alone* while a subtree is
blocked, so the block lifts by itself when the storage comes back (F-001/FR-22).

NULL for every existing row: a folder no scan has listed since this migration has no recorded
filesystem, and the check needs two observations to say anything. The first scan after an upgrade
fills them in and concludes nothing, which is the correct answer for an unknown baseline.

Revision ID: 0012_folder_device
Revises: 0011_folder_identity
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_folder_device"
down_revision: str | None = "0011_folder_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("folder", sa.Column("device_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("folder", "device_id")
