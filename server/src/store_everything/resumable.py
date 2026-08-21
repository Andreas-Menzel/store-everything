"""The IETF resumable-upload wire dialect: headers in, headers out, versions in one table.

ADR-0017 makes `draft-ietf-httpbis-resumable-upload-12` the **only** upload format, which
means this module is the whole vocabulary: what a client may say, what we answer, and which
draft iterations we recognise. Kept separate from the session logic on purpose — the wire
format changes on the IETF's schedule, the session's crash-safety does not.

Three things are worth knowing before reading the code.

**Versions are a table, not branches.** `Upload-Draft-Interop-Version` selects a dialect;
today 9 (draft-12), 8 (drafts -09…-11) and 6 (iOS 18.1+ `URLSession`) are wire-identical for
this flow, so the table's rows are equal and the *mechanism* is what matters: a difference
later is a row, not an `if`. A missing or unrecognised version is not an error — the request
is served as an ordinary, non-resumable upload, which is the fallback the draft mandates.

**We send no interim `104`.** The draft says a server SHOULD announce the upload resource in
an interim response "unless the server is not capable of sending interim responses", and ASGI
has no message for one: uvicorn speaks `http.response.start` and `http.response.body`, plus
`100 Continue`, and nothing else. So the upload resource is announced in the `201 Created`
that ends the creation request — the same `Location`, one round trip later. Q58 tracks what
this costs Apple's background uploader, which is the one client known to prefer the 104.

**Booleans are structured fields** (RFC 9651): `?1` and `?0`, never `true`/`1`. We emit
exactly that and parse leniently, because a client that gets this wrong is easier to
interoperate with than to argue with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: The media type an append must carry. Also how `security` recognises an append without
#: knowing a single route.
MEDIA_TYPE: Final = "application/partial-upload"

# Header names, lower-cased because that is how ASGI delivers them.
INTEROP_VERSION_HEADER: Final = "upload-draft-interop-version"
COMPLETE_HEADER: Final = "upload-complete"
OFFSET_HEADER: Final = "upload-offset"
LENGTH_HEADER: Final = "upload-length"
LIMIT_HEADER: Final = "upload-limit"

#: The registered problem type for an append at the wrong offset. Deliberately *not* one of
#: ours (08 § errors): a protocol-aware client — including one we did not write — recognises
#: this URI, and interoperability outranks a tidy namespace for this one response.
OFFSET_MISMATCH_TYPE: Final = (
    "https://iana.org/assignments/http-problem-types#mismatching-upload-offset"
)


@dataclass(frozen=True, slots=True)
class Dialect:
    """One recognised iteration of the draft."""

    interop_version: int
    note: str


#: Recognised dialects, newest first. Equal in behaviour today and separate on purpose.
DIALECTS: Final = (
    Dialect(9, "draft-12, the version this implementation is written against"),
    Dialect(8, "drafts -09 to -11; wire-identical for creation, append and offset probe"),
    Dialect(6, "iOS 18.1+ and macOS 15.1+ URLSession, the dialect tusd also implements"),
)

_BY_VERSION: Final = {dialect.interop_version: dialect for dialect in DIALECTS}


def boolean(value: bool) -> str:
    """A structured-field boolean, the only spelling we emit."""
    return "?1" if value else "?0"


def parse_boolean(raw: str | None) -> bool | None:
    """A structured-field boolean, parsed leniently. `None` means "absent or unintelligible"."""
    if raw is None:
        return None
    match raw.strip().lower():
        case "?1" | "1" | "true":
            return True
        case "?0" | "0" | "false":
            return False
        case _:
            return None


def parse_integer(raw: str | None) -> int | None:
    """A non-negative structured-field integer, or `None` if it is not one."""
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate.isdigit():
        return None
    return int(candidate)


def dialect_for(raw: str | None) -> Dialect | None:
    """The dialect a request declares, or `None` for "serve this as an ordinary upload".

    Absence and an unknown number are the same answer deliberately: the draft's own fallback
    is a plain upload, so a future client speaking a version we have never heard of degrades
    instead of failing.
    """
    version = parse_integer(raw)
    return None if version is None else _BY_VERSION.get(version)


@dataclass(frozen=True, slots=True)
class Limits:
    """What this instance will accept, as published in `Upload-Limit`.

    A limit nobody enforces is worse than no limit, so every field here is checked by the
    endpoints — and `max_size = None` genuinely means unlimited rather than a large number
    pretending to be one.
    """

    max_size: int | None
    min_append_size: int
    max_append_size: int
    max_age_seconds: int

    def render(self) -> str:
        """The header value: a structured dictionary, parameters in the draft's spelling."""
        parameters = [
            *(() if self.max_size is None else (f"max-size={self.max_size}",)),
            f"min-append-size={self.min_append_size}",
            f"max-append-size={self.max_append_size}",
            f"max-age={self.max_age_seconds}",
        ]
        return ", ".join(parameters)
