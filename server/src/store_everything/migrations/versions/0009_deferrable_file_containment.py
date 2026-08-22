"""Let a file and its folder change workspace together.

`file`'s composite foreign key is what makes 02 § invariant 1 structural: a file's workspace *is*
its folder's workspace, so no code path can file a row into another workspace's tree. A
**cross-workspace folder move**
([F-015/FR-4](../../../../features/F-015-folders.md)) has to rewrite both sides of that pair, and
whichever it writes first would fail the check on its own — the folder now belongs to the new
workspace while its files still claim the old one, or the reverse.

So the constraint becomes `DEFERRABLE INITIALLY IMMEDIATE`: it still fires on every ordinary
statement, exactly as before, and only a transaction that explicitly asks
(`SET CONSTRAINTS ... DEFERRED`) may hold it open until commit. The invariant is unchanged — it is
checked before anything becomes visible to anyone — but the *moment* of the check is now the
commit for the one operation that needs it.

`INITIALLY IMMEDIATE` rather than `INITIALLY DEFERRED` is the careful half: nothing else in the
system loses its per-statement failure, so a bug that files a row into the wrong workspace still
fails where it happens rather than at commit.

Revision ID: 0009_deferrable_file_containment
Revises: 0008_watch_state
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_deferrable_file_containment"
down_revision: str | None = "0008_watch_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "fk_file_folder_id_workspace_id_folder"


def upgrade() -> None:
    op.drop_constraint(_NAME, "file", type_="foreignkey")
    op.create_foreign_key(
        _NAME,
        "file",
        "folder",
        ["folder_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="CASCADE",
        deferrable=True,
        initially="IMMEDIATE",
    )


def downgrade() -> None:
    op.drop_constraint(_NAME, "file", type_="foreignkey")
    op.create_foreign_key(
        _NAME,
        "file",
        "folder",
        ["folder_id", "workspace_id"],
        ["id", "workspace_id"],
        ondelete="CASCADE",
    )
