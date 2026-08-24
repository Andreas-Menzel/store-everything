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
import zlib
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


#: The image fixture's shape and halves. A 2:1 image, red on the left and blue on the right:
#: the aspect ratio is what a thumbnail must preserve, and the two flat halves are what a
#: placeholder must still show after being squeezed into a few dozen bytes.
IMAGE_WIDTH = 800
IMAGE_HEIGHT = 400
IMAGE_LEFT = (203, 32, 39)
IMAGE_RIGHT = (30, 66, 159)

#: One line per page, each with something a later assertion can find: page 2 carries the pangram
#: (Latin script, every letter), page 3 the distinctive phrase the text fixtures also use — so
#: "which page is this phrase on" has an exact answer.
PDF_PAGES = (
    "Store Everything fixture page one.",
    "The quick brown fox jumps over the lazy dog.",
    "This page mentions xylophone marmalade exactly once.",
)

#: The one page of `mixed-text.pdf` that has a text layer. Its second page has none, which is
#: what makes that fixture worth having: the routing decision is per page.
MIXED_TEXT_PAGE = "This page carries its own text; the next one carries none."


def write(relative: str, payload: bytes) -> Path:
    path = FIXTURES / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )


def two_tone_png() -> bytes:
    """A valid 8-bit RGB PNG, written by hand so its truth is exact.

    Generated rather than curated (ADR-0015): a photograph would need a licence and would make
    every assertion approximate, while two flat halves make the interesting questions — did the
    aspect survive, are the colours where they should be — answerable by looking at one pixel.
    """
    header = struct.pack(">II", IMAGE_WIDTH, IMAGE_HEIGHT) + bytes([8, 2, 0, 0, 0])
    row = bytes(IMAGE_LEFT) * (IMAGE_WIDTH // 2) + bytes(IMAGE_RIGHT) * (IMAGE_WIDTH // 2)
    # Filter byte 0 (none) per scanline: the smallest thing a decoder must still handle.
    raw = (b"\x00" + row) * IMAGE_HEIGHT
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def _text_page(line: str) -> bytes:
    """A content stream that draws one line of Helvetica near the top of the page."""
    return f"BT /F1 24 Tf 72 700 Td ({line}) Tj ET\n".encode("ascii")


def three_page_pdf() -> bytes:
    """A three-page PDF with one line of text per page, assembled object by object.

    Hand-written for the same reason as the PNG, and because the *positions* are the point: a
    page anchor is only checkable against a document whose pages are known one by one. Letter
    size, one built-in font, no compression — a fixture a person can read in a hex dump.
    """
    return _assemble_pdf([_text_page(line) for line in PDF_PAGES])


def mixed_text_pdf() -> bytes:
    """One page with a text layer, one page without — the shape that routes to OCR.

    The decision `pdf-text` makes is per page, and a document where every page agrees cannot
    show that: `three-pages.pdf` proves text is read, and a fully scanned document would prove
    OCR is reached, but neither proves the *choice*. This one does — page 1 is extracted, page 2
    appears in `ocr_pages`, from one upload.

    Page 2 is a filled rectangle rather than a photograph of paper: what makes it interesting is
    the absence of text operators, and a real scan would add a licence, a megabyte and an
    approximate assertion to say the same thing (ADR-0015).
    """
    return _assemble_pdf([_text_page(MIXED_TEXT_PAGE), b"0.5 g 72 480 468 240 re f\n"])


def _assemble_pdf(streams: list[bytes]) -> bytes:
    """A minimal PDF around one content stream per page.

    Uncompressed, no metadata, cross-reference table written by hand — so the bytes depend on
    nothing but the streams handed in, and two runs of this script produce the same file.
    """
    pages = len(streams)
    # Object numbers: 1 catalog, 2 page tree, 3 font, then a page and a stream per page.
    page_ids = [4 + index * 2 for index in range(pages)]
    stream_ids = [identifier + 1 for identifier in page_ids]

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Count %d /Kids [%s] >>"
            % (pages, " ".join(f"{identifier} 0 R" for identifier in page_ids))
        ).encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, content in enumerate(streams):
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
                % stream_ids[index]
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(content)).encode("ascii")
            + b" >>\nstream\n"
            + content
            + b"endstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    start_xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start_xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


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
    write("images/two-tone.png", two_tone_png())
    write("documents/three-pages.pdf", three_page_pdf())
    write("documents/mixed-text.pdf", mixed_text_pdf())
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
