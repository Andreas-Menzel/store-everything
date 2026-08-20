"""Regenerate every generated corpus fixture, byte for byte.

ADR-0015: generate where the truth must be exact, curate where realism matters. Anything
this script writes is reproducible — no timestamps, no randomness — so `make corpus`
either changes nothing or shows exactly what changed.

Committed fixtures must survive a checkout on every developer's filesystem. Names that
collide under case-folding or unicode normalisation therefore cannot be files here; they
are described in `hostile-names.json` and materialised by the test that needs them.

    python corpus/generate.py
"""

from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: Fixed so archives are reproducible; zipfile would otherwise stamp "now".
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

KNOWN_PHRASES = """Store Everything corpus fixture.
This line is the second line of the file.
The quick brown fox jumps over the lazy dog.
Ein Satz auf Deutsch, damit die Sprachtrennung etwas zu tun hat.
The final line mentions a distinctive phrase: xylophone marmalade.
"""

MARKDOWN_SAMPLE = """# Fixture heading

An ordinary paragraph.

## Second heading

- a list item
- another list item

A closing paragraph that mentions xylophone marmalade exactly once.
"""

# Name pairs a filesystem may refuse to hold side by side. Data, not files: committing
# both members would break checkout on macOS and Windows (see this file's docstring).
HOSTILE_NAMES = {
    "case_collisions": [
        {
            "names": ["README.txt", "readme.txt"],
            "asserts": "Two entries differing only by case must not be merged silently (Q25).",
        },
        {
            "names": ["Photo.JPG", "photo.jpg"],
            "asserts": "Case-only differences in extensions collide on case-insensitive volumes.",
        },
    ],
    "unicode_normalisation": [
        {
            "names": ["Grüße.txt", "Grüße.txt"],
            "asserts": "NFC and NFD spellings of the same name are one file on APFS, two on ext4.",
        }
    ],
}


def write(relative: str, payload: bytes) -> Path:
    path = FIXTURES / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def zip_slip_archive() -> bytes:
    """An archive whose entry escapes the extraction directory."""
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("harmless.txt", "../escaped.txt", "nested/../../escaped-too.txt"):
            info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, "fixture payload\n")
    return buffer.getvalue()


def truncated_png() -> bytes:
    """A PNG that stops in the middle of its header chunk."""
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 640, 480)
    return signature + header  # bit depth, colour type and the CRC never arrive


def oversized_dimensions_png() -> bytes:
    """A complete, tiny header declaring an image too large to decode."""
    signature = b"\x89PNG\r\n\x1a\n"
    body = struct.pack(">II", 100_000, 100_000) + bytes([8, 6, 0, 0, 0])
    chunk = struct.pack(">I", len(body)) + b"IHDR" + body + struct.pack(">I", 0)
    return signature + chunk


def main() -> int:
    write("text/known-phrases.txt", KNOWN_PHRASES.encode("utf-8"))
    write("text/sample.md", MARKDOWN_SAMPLE.encode("utf-8"))
    write("adversarial/zero-byte.bin", b"")
    write("adversarial/truncated.png", truncated_png())
    write("adversarial/oversized-dimensions.png", oversized_dimensions_png())
    write("adversarial/zip-slip.zip", zip_slip_archive())
    write("adversarial/mislabeled-extension.png", b"This is plain text, not a PNG.\n")
    write(
        "adversarial/hostile-names.json",
        json.dumps(HOSTILE_NAMES, indent=2, ensure_ascii=False).encode("utf-8") + b"\n",
    )
    print(f"regenerated {len(list(FIXTURES.rglob('*')))} fixture paths under {FIXTURES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
