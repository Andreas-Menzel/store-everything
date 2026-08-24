"""`text-plain`: text, markdown, code and CSV, with the line numbers to find them by.

The simplest extractor and the one with the most files: notes, READMEs, source, exports. What it
has to get right is small and easy to get wrong:

- **the encoding**, because a file is bytes and "text" is an interpretation. UTF-8 first, then
  whatever `charset-normalizer` concludes, and a file that decodes to nothing legible is a
  *reported* failure rather than a table full of mojibake;
- **the anchors**, because "found in this file" is not an answer for a 4000-line log. Segments
  carry line ranges ([F-004/FR-3](../../../features/F-004-document-text-extraction.md)), so a hit
  points at the lines it is in;
- **the split**, because a segment is what a search result shows. Paragraph boundaries where
  there are any, a bounded length where there are not — a whole file in one segment makes every
  hit look identical.

Language detection is shared with the other text producers
([language.py](language.py)) so `language` means the same thing whoever wrote it (FR-4).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from types import FrameType
from typing import Any

from charset_normalizer import from_bytes

from se_extractor.client import ExtractorClient
from se_extractor.language import detect_language
from se_extractor.loop import JobContext, PermanentFailure, Worker
from se_extractor.models import Job

_logger = logging.getLogger("se_extractor.text_plain")

EXTRACTOR_ID = "text-plain"
VERSION = "1.0.0"

MANIFEST: dict[str, Any] = {
    "id": EXTRACTOR_ID,
    "version": VERSION,
    "api_version": "v1",
    # The types this can read as text. `text/*` covers markdown, code and CSV; the JSON and XML
    # families arrive with an `application/` prefix and are text all the same.
    "accepts": {
        "mime_types": [
            "text/*",
            "application/json",
            "application/xml",
            "application/x-yaml",
            "application/yaml",
            "application/toml",
        ]
    },
    "produces": ["text_segments", "metadata"],
    "cost_class": "light",
    "gpu": "none",
    "network": "none",
}

#: How long a segment may get before it is split anyway. Long enough to hold a paragraph or a
#: function, short enough that a search result is a snippet rather than a page.
MAX_SEGMENT_CHARACTERS = 1500

#: A file bigger than this is truncated rather than refused: a 200 MB log is still worth its first
#: pages, and holding it all in memory to index the end serves nobody.
MAX_BYTES = 8 * 1024 * 1024

#: How much of the decoded text has to be printable for the decoding to be believed. Below this,
#: the file is binary that happened to carry a text media type.
MIN_PRINTABLE_RATIO = 0.85


def handle(job: Job, context: JobContext) -> dict[str, Any] | None:
    """Decode, split, and say what language it is in."""
    original = job.original
    if original is None:
        return None

    payload = bytearray()
    with context.client.stream_input(job) as chunks:
        for chunk in chunks:
            context.raise_if_cancelled()
            payload += chunk
            if len(payload) >= MAX_BYTES:
                _logger.info("job %s truncated at %d bytes", job.id, MAX_BYTES)
                break

    text, encoding = decode(bytes(payload[:MAX_BYTES]))
    segments = split(text)
    language = detect_language(text)
    if language is not None:
        for segment in segments:
            segment["language"] = language

    facts: list[dict[str, Any]] = [{"key": "encoding", "type": "string", "value": encoding}]
    if language is not None:
        facts.append({"key": "language", "type": "string", "value": language})
    facts.append({"key": "line_count", "type": "integer", "value": text.count("\n") + 1})

    _logger.info("job %s produced %d segment(s) from %s", job.id, len(segments), encoding)
    return {"text_segments": segments, "metadata": facts}


def decode(payload: bytes) -> tuple[str, str]:
    """The text, and the encoding it turned out to be in.

    UTF-8 is tried first because it is nearly always the answer and guessing is slower than
    knowing. Everything else goes to `charset-normalizer`, which is honest about failing — and a
    failure here is *permanent*: the same bytes will not decode differently next time
    (F-004/FR-6).
    """
    if not payload:
        return "", "utf-8"
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        best = from_bytes(payload).best()
        if best is None:
            raise PermanentFailure("this file is not text in any encoding we recognise") from None
        decoded, encoding = str(best), best.encoding
    else:
        encoding = "utf-8"

    printable = sum(1 for character in decoded if character.isprintable() or character in "\n\r\t")
    if decoded and printable / len(decoded) < MIN_PRINTABLE_RATIO:
        # It decoded, and it is still not text: binary with a text media type. Reporting that is
        # more useful than indexing control characters (FR-6).
        raise PermanentFailure("this file carries a text type but is not text")
    return decoded, encoding


def split(text: str) -> list[dict[str, Any]]:
    """Segments with line ranges: paragraph boundaries where there are any, length otherwise."""
    lines = text.splitlines()
    segments: list[dict[str, Any]] = []
    buffer: list[str] = []
    start = 1

    def flush(end: int) -> None:
        body = "\n".join(buffer).strip()
        if body:
            segments.append(
                {"text": body, "anchor": {"kind": "line", "start_line": start, "end_line": end}}
            )

    for number, line in enumerate(lines, start=1):
        blank = not line.strip()
        buffer.append(line)
        long_enough = sum(len(one) + 1 for one in buffer) >= MAX_SEGMENT_CHARACTERS
        if (blank and any(one.strip() for one in buffer)) or long_enough:
            flush(number)
            buffer = []
            start = number + 1
        elif blank:
            # Leading blank lines belong to nothing; move the start past them.
            buffer = []
            start = number + 1

    if buffer:
        flush(len(lines))
    return segments


def main() -> int:
    """Run `text-plain` from the environment. The image's entrypoint."""
    logging.basicConfig(
        level=os.environ.get("SE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = os.environ.get("SE_EXTRACTOR_TOKEN", "")
    if not token:
        print("SE_EXTRACTOR_TOKEN is not set", file=sys.stderr)
        return 2

    client = ExtractorClient(os.environ.get("SE_CORE_URL", "http://api:8000"), token)
    worker = Worker(
        client,
        MANIFEST,
        handle,
        claim_wait=int(os.environ.get("SE_CLAIM_WAIT_SECONDS", "30") or 30),
        worker_name=os.environ.get("HOSTNAME"),
    )

    def stop(_signal: int, _frame: FrameType | None) -> None:
        _logger.info("stopping")
        worker.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        worker.run()
    finally:
        client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - the image's entrypoint
    raise SystemExit(main())
