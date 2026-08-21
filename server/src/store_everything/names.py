"""The name policy: what a file, folder or workspace may be called, and when two names are one.

The same tree is written by Linux, macOS and Windows machines whose name rules disagree, so
"is this the same name?" cannot be answered with `==`. ADR-0019 answers it once, here:

- names are **stored exactly as found** — case-preserving, byte-preserving;
- siblings must be unique on a **comparison key**: NFC-normalized and case-folded, so
  `Foo.txt` and `foo.txt` cannot coexist, and neither can the NFC and NFD spellings of one
  name (the macOS-over-SMB case that produces Nextcloud's phantom duplicates);
- names arriving **through the API** are normalized to NFC before storage; names found **on
  disk** are stored verbatim, with the key derived from them;
- the limits are explicit — 255 bytes per name, 4096 bytes per workspace-relative path — so
  an over-long name fails predictably here instead of at the filesystem's whim.

Every path lookup compares keys, never raw strings. That is a discipline each query has to
follow ([03 § names on disk](../../../specs/03-storage-and-portability.md#names-on-disk)),
not an optimization, which is why the key lives in a column of its own.
"""

from __future__ import annotations

import unicodedata

#: The single directory the app writes into a user's tree (ADR-0018). Its layout lives in
#: `workspacefs`; the name lives here, because "reserved at a workspace root" is a rule of
#: the name policy.
CONTROL_DIRECTORY = ".workspace"

#: Per name, in UTF-8 bytes. The common filesystem limit, made ours so it is enforced
#: consistently rather than reported differently by every backend.
MAX_NAME_BYTES = 255

#: Per workspace-relative path, in UTF-8 bytes — Linux's `PATH_MAX`, used for the same reason.
MAX_PATH_BYTES = 4096

#: Names the app owns at a workspace root, and therefore refuses to hand out
#: (ADR-0018, [F-015/FR-6](../../../features/F-015-folders.md)).
RESERVED_ROOT_NAMES = frozenset({CONTROL_DIRECTORY})

#: Entries that address a directory rather than name one. A filesystem could never hold them
#: as names, and letting one through would turn a path into traversal.
_TRAVERSAL_NAMES = frozenset({".", ".."})


class InvalidNameError(ValueError):
    """A name breaks the policy.

    Carries the rule it broke rather than the value: the API echoes the rule and never the
    submitted name (08-api-principles.md § errors).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def comparison_key(name: str) -> str:
    """The key sibling uniqueness is enforced on.

    Case folding runs over the *decomposed* form and the result is recomposed, which is
    Unicode's own recipe for caseless matching (UAX #15): folding a composed character can
    yield a decomposed sequence, so folding first and normalizing after is what makes the
    key stable — `key(key(x)) == key(x)` — and equal for the NFC and NFD spellings of one
    name.
    """
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", name).casefold())


#: The reserved names as keys, so the check is a lookup rather than a fold per call.
_RESERVED_ROOT_KEYS = frozenset(comparison_key(name) for name in RESERVED_ROOT_NAMES)


def normalize_api_name(name: str) -> str:
    """Normalize a name arriving through the API to NFC.

    Only the API side: a name found on disk is stored exactly as the filesystem reports it,
    because rewriting it would rename a user's file to suit our storage.
    """
    return unicodedata.normalize("NFC", name)


def validate_name(name: str, *, at_root: bool = False) -> None:
    """Refuse a name that may not be stored, naming the rule it broke.

    `at_root` adds the reserved names the app owns in a workspace root; the same names are
    ordinary further down the tree, because nothing of ours lives there.
    """
    if not name:
        raise InvalidNameError("a name must not be empty")
    if name in _TRAVERSAL_NAMES:
        raise InvalidNameError("'.' and '..' are not names")
    if "/" in name:
        raise InvalidNameError("a name must not contain '/'")
    if any(unicodedata.category(character) == "Cc" for character in name):
        raise InvalidNameError("a name must not contain control characters")
    if len(name.encode()) > MAX_NAME_BYTES:
        raise InvalidNameError(f"a name must be at most {MAX_NAME_BYTES} bytes")
    if at_root and comparison_key(name) in _RESERVED_ROOT_KEYS:
        raise InvalidNameError(f"'{name}' is reserved at a workspace root")
