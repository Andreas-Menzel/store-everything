"""Finding the visual assets a file has: thumbnails, placeholders, previews.

The generation is an extractor's work ([09](../../../specs/09-previews.md)); this is the reading
half — the queries an API needs to answer *which* thumbnail, *is there* one at all, and *what*
should a grid paint while it waits.

Two rules shape it:

- **Sizes are a fixed set, and a request snaps up into it.** Asking for 300 gets 512. That is
  what keeps a thumbnail URL immutable and cacheable forever
  ([F-028/FR-1](../../../features/F-028-thumbnails-and-previews.md)) — free-form resizing would
  mean an unbounded number of derived files and a cache key per layout.
- **A missing thumbnail is an answer, not an error.** Plenty of files have nothing to render, and
  a listing has to be able to say so per row rather than firing a request that fails (FR-3).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything.tables import derived_asset, file_version, metadata_entry

#: The set, largest first (09 § thumbnails, Q42). Fixed here as well as in the extractor because
#: the API's snapping rule and the generator's output have to agree, and this is the side a
#: client sees.
THUMBNAIL_SIZES = (256, 512, 1024)

THUMBNAIL_KIND = "thumbnail"

#: The well-known metadata key a grid reads before any image arrives
#: ([02 § MetadataEntry](../../../specs/02-domain-model.md#metadataentry)).
PLACEHOLDER_KEY = "placeholder_hash"


def snap(requested: int | None) -> int:
    """The tier a request gets: the smallest one at least as large, else the largest there is.

    Snapping *up* rather than down, because a client asking for 300 px is describing the space it
    has to fill — handing it 256 would make it upscale, which is the one thing a fixed set is
    supposed to prevent.
    """
    if requested is None:
        return THUMBNAIL_SIZES[0]
    for size in THUMBNAIL_SIZES:
        if requested <= size:
            return size
    return THUMBNAIL_SIZES[-1]


@dataclass(frozen=True, slots=True)
class Thumbnail:
    """One stored thumbnail: where its bytes are, and what they are."""

    name: str
    media_type: str
    size: int
    """The tier it answers for, which is not necessarily its longest edge: a small image's tiers
    are all the size of the original, because nothing is upscaled."""

    width: int | None
    height: int | None
    source_hash: str
    """The *version's* content hash — the derived store's directory, and the ETag."""


async def thumbnail(
    connection: AsyncConnection, *, file_version_id: UUID, size: int
) -> Thumbnail | None:
    """The thumbnail of one version at one tier, or `None` if there is none.

    `None` covers both cases a caller must not tell apart from the outside: a file with no
    thumbnail source, and one whose generation has not finished yet. Both mean "not now"; the
    difference is in the extraction status, not here.
    """
    rows = await connection.execute(
        select(
            derived_asset.c.name,
            derived_asset.c.media_type,
            derived_asset.c.params,
            file_version.c.content_hash,
        )
        .select_from(derived_asset)
        .join(file_version, file_version.c.id == derived_asset.c.file_version_id)
        .where(
            derived_asset.c.file_version_id == file_version_id,
            derived_asset.c.kind == THUMBNAIL_KIND,
            # The tier lives in the asset's own params, so the query is one indexed lookup and
            # the naming convention stays the extractor's business (02 § DerivedAsset).
            derived_asset.c.params["size"].astext == str(size),
        )
        # The newest generation wins: reprocessing writes new rows beside the old ones, and a
        # client asking for a thumbnail wants the current answer (ADR-0004).
        .order_by(derived_asset.c.generation.desc(), derived_asset.c.created_at.desc())
        .limit(1)
    )
    row = rows.first()
    if row is None:
        return None
    params = dict(row.params or {})
    return Thumbnail(
        name=row.name,
        media_type=row.media_type,
        size=size,
        width=params.get("width"),
        height=params.get("height"),
        source_hash=row.content_hash,
    )


async def placeholders(connection: AsyncConnection, version_ids: list[UUID]) -> dict[UUID, str]:
    """The placeholder for each of these versions, where one exists.

    Per page rather than per row: a listing of fifty files is one query, which is the whole point
    of inlining the placeholder instead of making a client ask fifty times (F-028/FR-5).
    """
    if not version_ids:
        return {}
    rows = await connection.execute(
        select(metadata_entry.c.file_version_id, metadata_entry.c.value_text)
        .where(
            metadata_entry.c.file_version_id.in_(version_ids),
            metadata_entry.c.key == PLACEHOLDER_KEY,
            metadata_entry.c.value_text.is_not(None),
        )
        .order_by(metadata_entry.c.file_version_id, metadata_entry.c.generation.desc())
    )
    found: dict[UUID, str] = {}
    for row in rows.all():
        # First per version wins, and the order above makes that the newest generation's.
        found.setdefault(row.file_version_id, row.value_text)
    return found


async def with_thumbnails(connection: AsyncConnection, version_ids: list[UUID]) -> set[UUID]:
    """Which of these versions have a thumbnail at all — one query for a whole page.

    A listing needs this to decide between an image and a type icon *without* the client
    discovering it by requesting an image that answers `404` (F-028/FR-3).
    """
    if not version_ids:
        return set()
    rows = await connection.execute(
        select(derived_asset.c.file_version_id)
        .where(
            derived_asset.c.file_version_id.in_(version_ids),
            derived_asset.c.kind == THUMBNAIL_KIND,
        )
        .distinct()
    )
    return {row.file_version_id for row in rows.all()}


__all__ = [
    "PLACEHOLDER_KEY",
    "THUMBNAIL_KIND",
    "THUMBNAIL_SIZES",
    "Thumbnail",
    "placeholders",
    "snap",
    "thumbnail",
    "with_thumbnails",
]
