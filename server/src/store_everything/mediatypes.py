"""The media type of a file, and the coarse class every file carries from the moment it lands.

[04 § identification](../../../specs/04-ingestion-pipeline.md#2-identification) assigns a
**media class** — `image | video | audio | document | archive | other` — from the detected
MIME type, before any extractor runs, so type-scoped listings work the instant a file appears.
The mapping is core-owned and versioned with the core; changing it re-derives stored classes
in the database rather than re-running extraction.

Detection here is deliberately shallow: the name's extension first, the client's declared
`Content-Type` second. No content sniffing and no `libmagic` — a native dependency would need
its own licence review (ADR-0016) to answer a question phase 2 answers properly anyway, when
extractors read the bytes. The honest consequence is that a file with a wrong or missing
extension gets `other` until an extractor corrects it, which costs a listing facet, not data.
"""

from __future__ import annotations

import mimetypes
from typing import Final

#: 04 § identification. `other` is not a failure — it is "nothing built-in keys off this".
MEDIA_CLASSES: Final = ("image", "video", "audio", "document", "archive", "other")

DEFAULT_MEDIA_TYPE: Final = "application/octet-stream"

#: Document families named one by one, because "document" is a product decision rather than a
#: property of the MIME tree: `text/*` plus PDF, the office and OpenDocument families, RTF and
#: EPUB (04 § identification).
_DOCUMENT_TYPES: Final = frozenset(
    {
        "application/pdf",
        "application/rtf",
        "application/epub+zip",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
        "application/vnd.oasis.opendocument.graphics",
    }
)

_ARCHIVE_TYPES: Final = frozenset(
    {
        "application/zip",
        "application/x-tar",
        "application/gzip",
        "application/x-bzip2",
        "application/x-xz",
        "application/x-7z-compressed",
        "application/vnd.rar",
        "application/x-rar-compressed",
    }
)


def normalize(declared: str | None) -> str | None:
    """A media type without its parameters, lower-cased — or `None` if it is not one.

    `image/jpeg; charset=binary` and `IMAGE/JPEG` are one type; `not a type` is none.
    """
    if declared is None:
        return None
    candidate = declared.partition(";")[0].strip().lower()
    if candidate.count("/") != 1 or not all(candidate.partition("/")[::2]):
        return None
    return candidate


def detect(name: str, declared: str | None = None) -> str:
    """The media type to record for a file called `name`, uploaded with `declared`.

    The extension wins where it says anything, because browsers routinely declare
    `application/octet-stream` for files they cannot classify, and a name the user chose is
    better evidence than that. A declared type is the fallback for the extension-less case
    (`IMG_0001`), and `application/octet-stream` is the honest last resort.
    """
    guessed = normalize(mimetypes.guess_type(name)[0])
    if guessed is not None:
        return guessed
    return normalize(declared) or DEFAULT_MEDIA_TYPE


def media_class(media_type: str) -> str:
    """The coarse class the default library pages build on (F-017)."""
    normalized = normalize(media_type) or DEFAULT_MEDIA_TYPE
    family = normalized.partition("/")[0]
    if family in {"image", "video", "audio"}:
        return family
    if family == "text" or normalized in _DOCUMENT_TYPES:
        return "document"
    if normalized in _ARCHIVE_TYPES:
        return "archive"
    return "other"
