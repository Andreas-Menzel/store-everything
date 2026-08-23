"""The result envelope, and what applying it means.

One envelope per job, applied in **one transaction**: every row lands with the run that produced
it, or nothing does (05 § dispatch). That is what makes at-least-once delivery safe to build on:
a second attempt replaces this run's own rows rather than doubling them, and a crash halfway
leaves the version looking exactly as it did before.

Three shapes of output, three tables, one rule between them: **the run is the provenance**
([02 § invariants](../../../specs/02-domain-model.md#invariants) #3). Nothing here writes a
derived row without one, which is why the extractor never gets to say who produced its results.

The envelope tolerates unknown *fields* and refuses unknown *values*, and the difference is
deliberate. A newer extractor sending a field this core has not heard of must keep working
(05 § compatibility rules) — so extra fields are kept and ignored. But an unknown metadata type
or anchor kind is data we could not store correctly, and accepting it would mean dropping it
silently; those are refused with a field-level problem instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import derived as derived_store
from store_everything.derived import DerivedStore, InvalidAssetNameError
from store_everything.ids import new_id
from store_everything.tables import derived_asset, metadata_entry, segment

MAX_METADATA_ENTRIES = 500
MAX_SEGMENTS = 50_000
MAX_ASSETS = 500
MAX_SEGMENT_LENGTH = 100_000


class _Part(BaseModel):
    """Base for every envelope object: unknown fields are kept, not refused."""

    model_config = ConfigDict(extra="allow", frozen=True)


# ------------------------------------------------------------------------------------ anchors


class PageAnchor(_Part):
    """A page, and optionally where on it — the anchor a document hit is rendered from."""

    kind: Literal["page"] = "page"
    page: int = Field(ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)


class TimeAnchor(_Part):
    """A span of a recording, in milliseconds — *at 04:12* (F-006)."""

    kind: Literal["time"] = "time"
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms precedes start_ms")
        return self


class LineAnchor(_Part):
    """A line range, for text, markdown and code."""

    kind: Literal["line"] = "line"
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("end_line precedes start_line")
        return self


class SheetAnchor(_Part):
    """A sheet and a row range — a better position for a spreadsheet than an invented page."""

    kind: Literal["sheet"] = "sheet"
    sheet: str = Field(min_length=1, max_length=200)
    start_row: int = Field(ge=1)
    end_row: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_row < self.start_row:
            raise ValueError("end_row precedes start_row")
        return self


class RegionAnchor(_Part):
    """A rectangle on an image, normalised so it survives every rendition's scaling."""

    kind: Literal["region"] = "region"
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)


class WholeAnchor(_Part):
    """The whole file — an image's single OCR block, a short note's only segment."""

    kind: Literal["whole"] = "whole"


Anchor = Annotated[
    PageAnchor | TimeAnchor | LineAnchor | SheetAnchor | RegionAnchor | WholeAnchor,
    Field(discriminator="kind"),
]


# ------------------------------------------------------------------------------------ outputs


class MetadataEntry(_Part):
    """One typed fact. The type decides both how it is stored and what can be asked of it."""

    key: str = Field(min_length=1, max_length=100)
    type: Literal[
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
    ]
    value: Any
    confidence: float | None = Field(default=None, ge=0, le=1)


class SegmentEntry(_Part):
    """A span of text and where it is."""

    text: str = Field(min_length=1, max_length=MAX_SEGMENT_LENGTH)
    anchor: Anchor
    confidence: float | None = Field(default=None, ge=0, le=1)
    language: str | None = Field(default=None, min_length=2, max_length=35)


class AssetEntry(_Part):
    """One staged file, and what it is.

    `content_hash` is how it was staged and how the core finds it; `name` is what it will be
    called in the derived store. The extractor chooses the name because it knows what the file
    is; the core chooses the directory because it knows where the version's data belongs.
    """

    kind: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=derived_store.MAX_NAME_LENGTH)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=3, max_length=255)
    params: dict[str, Any] = Field(default_factory=dict)
    rendition_kind: str | None = Field(default=None, min_length=1, max_length=64)
    """Set when this asset is a downloadable alternative form of the whole file (ADR-0008)."""


class Envelope(_Part):
    """Everything one job produced. Every part optional — most extractors produce one shape."""

    metadata: tuple[MetadataEntry, ...] = Field(default=(), max_length=MAX_METADATA_ENTRIES)
    text_segments: tuple[SegmentEntry, ...] = Field(default=(), max_length=MAX_SEGMENTS)
    derived_assets: tuple[AssetEntry, ...] = Field(default=(), max_length=MAX_ASSETS)

    @model_validator(mode="after")
    def _names_are_distinct(self) -> Self:
        names = [asset.name for asset in self.derived_assets]
        if len(set(names)) != len(names):
            raise ValueError("two derived assets claim the same name")
        return self


@dataclass(frozen=True, slots=True)
class Applied:
    """What an envelope actually wrote, reported back so an extractor can check itself."""

    metadata: int
    segments: int
    assets: int


class MissingAssetError(Exception):
    """An envelope references a staged asset that was never uploaded, or has been collected."""

    def __init__(self, content_hash: str) -> None:
        super().__init__(content_hash)
        self.content_hash = content_hash


# ------------------------------------------------------------------------------ applying it


async def apply(
    connection: AsyncConnection,
    *,
    run_id: UUID,
    file_version_id: UUID,
    source_hash: str,
    generation: int,
    envelope: Envelope,
    store: DerivedStore,
    operation_id: UUID,
) -> Applied:
    """Write one envelope's outputs. Called inside the transaction that completes the job.

    Order matters twice over. This run's previous rows go first, so a re-applied result replaces
    rather than doubles. Then bytes before rows, per asset: a committed file with no row is
    debris the janitor collects, while a row with no file is a broken promise
    (02 § invariants #8).
    """
    await discard(connection, run_id=run_id)

    for entry in envelope.metadata:
        await connection.execute(
            insert(metadata_entry).values(
                id=new_id(),
                file_version_id=file_version_id,
                run_id=run_id,
                key=entry.key,
                value_type=entry.type,
                provenance="auto",
                confidence=entry.confidence,
                generation=generation,
                **_stored_value(entry),
            )
        )

    for ordinal, span in enumerate(envelope.text_segments):
        await connection.execute(
            insert(segment).values(
                id=new_id(),
                file_version_id=file_version_id,
                run_id=run_id,
                generation=generation,
                ordinal=ordinal,
                text=span.text,
                anchor_kind=span.anchor.kind,
                anchor=span.anchor.model_dump(mode="json", exclude={"kind"}),
                confidence=span.confidence,
                language=span.language,
            )
        )

    for asset in envelope.derived_assets:
        staged = store.staging_path(operation_id=operation_id, digest=asset.content_hash)
        placed = store.path_for(source_hash, asset.name)
        if not staged.is_file() and not placed.is_file():
            raise MissingAssetError(asset.content_hash)
        size = (staged if staged.is_file() else placed).stat().st_size
        store.commit(staged, source_hash=source_hash, name=asset.name)
        await connection.execute(
            insert(derived_asset).values(
                id=new_id(),
                file_version_id=file_version_id,
                run_id=run_id,
                generation=generation,
                kind=asset.kind,
                name=asset.name,
                media_type=asset.media_type,
                size_bytes=size,
                content_hash=asset.content_hash,
                params=asset.params,
                rendition_kind=asset.rendition_kind,
            )
        )

    return Applied(
        metadata=len(envelope.metadata),
        segments=len(envelope.text_segments),
        assets=len(envelope.derived_assets),
    )


async def discard(connection: AsyncConnection, *, run_id: UUID) -> None:
    """Forget what one run wrote. The rows only; the bytes are content-addressed and shared.

    Deleting the files would be wrong here: a derived asset lives under the *source* hash, so a
    second version of identical content — or a reused run — points at the same bytes. Reclaiming
    them is the purge's job, once nothing references them at all (F-014).
    """
    for table in (segment, metadata_entry, derived_asset):
        await connection.execute(delete(table).where(table.c.run_id == run_id))


def _stored_value(entry: MetadataEntry) -> dict[str, Any]:
    """The one column this value belongs in, typed as its declaration says.

    Coercion is deliberately narrow: an extractor that says `integer` and sends `"seven"` has a
    bug worth hearing about, and guessing would put a string in a numeric facet.
    """
    value = entry.value
    match entry.type:
        case "string" | "text":
            return {"value_text": str(value)}
        case "integer":
            return {"value_number": float(int(value))}
        case "float" | "duration":
            # `duration` is seconds, as the well-known registry defines it (02 § MetadataEntry).
            return {"value_number": float(value)}
        case "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{entry.key} is declared boolean but is {type(value).__name__}")
            return {"value_boolean": value}
        case "datetime" | "date":
            return {"value_time": _moment(value, entry.type)}
        case "geo":
            latitude, longitude = _coordinates(value)
            return {"value_latitude": latitude, "value_longitude": longitude}
        case "json":
            return {"value_json": value}


def _moment(value: Any, declared: str) -> datetime:
    """A point in time from what an extractor sent — ISO 8601, or already a datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        if declared == "datetime":
            return datetime.fromisoformat(value)
        return datetime.combine(date.fromisoformat(value), datetime.min.time())
    raise ValueError(f"{value!r} is not a {declared}")


def _coordinates(value: Any) -> tuple[float, float]:
    """`{"lat": …, "lon": …}` — named rather than positional, because x/y order is a trap."""
    if not isinstance(value, dict):
        raise ValueError("a geo value must be an object with `lat` and `lon`")
    try:
        latitude = float(value["lat"])
        longitude = float(value["lon"])
    except (KeyError, TypeError, ValueError) as malformed:
        raise ValueError("a geo value needs numeric `lat` and `lon`") from malformed
    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        raise ValueError("a geo value must lie on the earth")
    return latitude, longitude


def validate_asset_names(envelope: Envelope) -> None:
    """Refuse an asset name before anything is written. Raises `InvalidAssetNameError`."""
    for asset in envelope.derived_assets:
        derived_store.validate_name(asset.name)


# --------------------------------------------------------------------------------- reading


@dataclass(frozen=True, slots=True)
class StoredMetadata:
    key: str
    value_type: str
    value: Any
    provenance: str
    confidence: float | None
    generation: int
    extractor_id: str | None


@dataclass(frozen=True, slots=True)
class StoredSegment:
    id: UUID
    ordinal: int
    text: str
    anchor_kind: str
    anchor: dict[str, Any]
    confidence: float | None
    language: str | None
    generation: int
    extractor_id: str | None


async def metadata_of(connection: AsyncConnection, file_version_id: UUID) -> list[StoredMetadata]:
    """Every fact known about one version, with the extractor behind each one."""
    from store_everything.tables import extraction_run

    rows = await connection.execute(
        select(
            metadata_entry.c.key,
            metadata_entry.c.value_type,
            metadata_entry.c.value_text,
            metadata_entry.c.value_number,
            metadata_entry.c.value_boolean,
            metadata_entry.c.value_time,
            metadata_entry.c.value_latitude,
            metadata_entry.c.value_longitude,
            metadata_entry.c.value_json,
            metadata_entry.c.provenance,
            metadata_entry.c.confidence,
            metadata_entry.c.generation,
            extraction_run.c.extractor_id,
        )
        .outerjoin(extraction_run, extraction_run.c.id == metadata_entry.c.run_id)
        .where(metadata_entry.c.file_version_id == file_version_id)
        .order_by(metadata_entry.c.key, metadata_entry.c.created_at)
    )
    return [
        StoredMetadata(
            key=row.key,
            value_type=row.value_type,
            value=_read_value(row),
            provenance=row.provenance,
            confidence=row.confidence,
            generation=row.generation,
            extractor_id=row.extractor_id,
        )
        for row in rows.all()
    ]


def _read_value(row: Any) -> Any:
    match row.value_type:
        case "string" | "text":
            return row.value_text
        case "integer":
            return None if row.value_number is None else int(row.value_number)
        case "float" | "duration":
            return row.value_number
        case "boolean":
            return row.value_boolean
        case "datetime" | "date":
            return row.value_time
        case "geo":
            if row.value_latitude is None:
                return None
            return {"lat": row.value_latitude, "lon": row.value_longitude}
        case _:
            return row.value_json


async def segments_of(
    connection: AsyncConnection, file_version_id: UUID, *, limit: int, after: int | None = None
) -> list[StoredSegment]:
    """A version's segments in reading order — the positional half of a search hit."""
    from store_everything.tables import extraction_run

    query = (
        select(
            segment.c.id,
            segment.c.ordinal,
            segment.c.text,
            segment.c.anchor_kind,
            segment.c.anchor,
            segment.c.confidence,
            segment.c.language,
            segment.c.generation,
            extraction_run.c.extractor_id,
        )
        .outerjoin(extraction_run, extraction_run.c.id == segment.c.run_id)
        .where(segment.c.file_version_id == file_version_id)
        .order_by(segment.c.ordinal, segment.c.id)
        .limit(limit)
    )
    if after is not None:
        query = query.where(segment.c.ordinal > after)

    rows = await connection.execute(query)
    return [
        StoredSegment(
            id=row.id,
            ordinal=row.ordinal,
            text=row.text,
            anchor_kind=row.anchor_kind,
            anchor=dict(row.anchor or {}),
            confidence=row.confidence,
            language=row.language,
            generation=row.generation,
            extractor_id=row.extractor_id,
        )
        for row in rows.all()
    ]


async def value_of(connection: AsyncConnection, *, file_version_id: UUID, key: str) -> Any:
    """One metadata value, for the routing predicate that binds to a well-known key."""
    rows = await connection.execute(
        select(
            metadata_entry.c.value_type,
            metadata_entry.c.value_text,
            metadata_entry.c.value_number,
            metadata_entry.c.value_boolean,
            metadata_entry.c.value_time,
            metadata_entry.c.value_latitude,
            metadata_entry.c.value_longitude,
            metadata_entry.c.value_json,
        )
        .where(metadata_entry.c.file_version_id == file_version_id, metadata_entry.c.key == key)
        # The most recent wins: a later run's answer supersedes an earlier one's.
        .order_by(metadata_entry.c.created_at.desc())
        .limit(1)
    )
    row = rows.first()
    return None if row is None else _read_value(row)


__all__ = [
    "Applied",
    "AssetEntry",
    "Envelope",
    "InvalidAssetNameError",
    "MetadataEntry",
    "MissingAssetError",
    "SegmentEntry",
    "StoredMetadata",
    "StoredSegment",
    "apply",
    "discard",
    "metadata_of",
    "segments_of",
    "validate_asset_names",
    "value_of",
]
