"""Whether a worker is listening to a workspace's root, and why not when it is not.

The watcher is a lossy doorbell (ADR-0019): losing it costs latency, never correctness, so *not
watching* is an ordinary state rather than a failure. But invisible is not the same as harmless:
a user whose share is silently unwatched has no way to tell why a change took an hour to appear.
These two columns are that answer, written by the worker holding the subscription and reported by
`import-status`.

Nothing reads them to make a decision. They are a record of what the app is doing, which is why
a stale `watching` left behind by a `kill -9` is acceptable: the next worker start rewrites every
row it takes responsibility for.

Revision ID: 0008_watch_state
Revises: 0007_reconciliation_and_versions
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from store_everything.tables import WORKSPACE_WATCH_STATES, one_of

revision: str = "0008_watch_state"
down_revision: str | None = "0007_reconciliation_and_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspace",
        sa.Column("watch_state", sa.Text(), server_default=sa.text("'unwatched'"), nullable=False),
    )
    op.add_column("workspace", sa.Column("watch_detail", sa.Text(), nullable=True))
    # The *short* name: the metadata naming convention interpolates `%(constraint_name)s`, so a
    # qualified one comes out doubled — on the way out as well as in.
    op.create_check_constraint(
        "watch_state_known", "workspace", one_of("watch_state", WORKSPACE_WATCH_STATES)
    )


def downgrade() -> None:
    op.drop_constraint("watch_state_known", "workspace", type_="check")
    op.drop_column("workspace", "watch_detail")
    op.drop_column("workspace", "watch_state")
