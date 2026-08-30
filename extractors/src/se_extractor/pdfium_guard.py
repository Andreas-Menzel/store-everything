"""PDFium, used from more than one thread without crashing the process.

PDFium is a single global library with global state, and it is not thread-safe: two threads
loading pages at the same time do not raise, they segfault. In the deployment that never comes
up — one extractor per container, one worker, one thread touching PDFium — so it would be easy to
believe the problem does not exist.

It exists in two places that matter. A test process runs several workers side by side to exercise
the seams (`server/tests/test_phase_two_walkthrough.py` runs six), and Python's garbage collector
closes a document whenever it feels like it, on whichever thread happens to trigger the collection.
Either is enough for a crash that no traceback explains, and a crash in the middle of a suite is
the kind of failure people learn to re-run instead of read.

So every PDFium call in this package happens under this one process-wide, re-entrant lock. In a
container it is an uncontended acquisition per page — nothing next to rendering one — and in a
process with several workers it is the difference between a queue and a core dump.

    with pdfium_guard.LOCK:
        document = pdfium.PdfDocument(path)
"""

from __future__ import annotations

import threading

#: Re-entrant, because the natural way to write this is a locked section that calls another one —
#: rendering a page inside a locked "open, count, render" block. A plain lock would deadlock the
#: first time somebody did the obvious thing.
LOCK = threading.RLock()

__all__ = ["LOCK"]
