"""Primary keys: time-ordered UUIDs.

Every entity id in this system is a UUID because ids are handed to clients and must
survive rename, move and re-scan (02-domain-model.md § file). Random (version 4) UUIDs
make B-tree inserts land in random leaf pages, which stops being free once a table holds
millions of rows — and the tables coming in later phases (files, segments, embeddings) are
exactly that size. **Version 7** keeps the same 128-bit opaque shape but prefixes a
millisecond timestamp, so inserts append to the right-hand edge of the index.

Generated in the application rather than by the database, because the write protocol needs
the id *before* the row exists: staging paths are derived from operation ids
(12-reliability.md § filesystem write protocol), and a deterministic path is what makes a
retry converge instead of leaking a second file.

Ids from one process are **strictly increasing**, including within a single millisecond
(RFC 9562's counter method): `created_at` comes from `now()`, which in PostgreSQL is the
*transaction's* start time, so rows written together share it exactly and the id is what
breaks the tie — for cursors, for stable sorting, and for anyone reading a table by eye.

The timestamp is observable in the id. That is not a leak here: every row carrying one
also stores its `created_at`. Ids are not credentials — the things that must be
unguessable are minted in `tokens.py`.
"""

from __future__ import annotations

import os
import threading
import time
from uuid import UUID

_VERSION_7 = 0x7000
_VARIANT_RFC4122 = 0x8000

#: The 12 bits beside the version nibble, used as a within-millisecond counter.
_COUNTER_BITS = 12
_COUNTER_LIMIT = 1 << _COUNTER_BITS

_lock = threading.Lock()
_last_milliseconds = 0
_counter = 0


def _next_position() -> tuple[int, int]:
    """The (millisecond, counter) pair for the next id, monotonic under concurrency."""
    global _last_milliseconds, _counter

    with _lock:
        now = time.time_ns() // 1_000_000
        if now > _last_milliseconds:
            _last_milliseconds, _counter = now, 0
        elif _counter + 1 < _COUNTER_LIMIT:
            _counter += 1
        else:
            # More than 4096 ids inside one millisecond: borrow from the next one rather
            # than sleep or repeat. Ordering survives; the timestamp runs at most a few
            # milliseconds ahead under a burst nothing in this system produces.
            _last_milliseconds += 1
            _counter = 0
        return _last_milliseconds, _counter


def new_id() -> UUID:
    """A UUID version 7 (RFC 9562): 48-bit millisecond timestamp, counter, then random."""
    milliseconds, counter = _next_position()
    random_bytes = os.urandom(8)

    time_high = counter | _VERSION_7
    # 62 random bits share the field carrying the two variant bits; masking keeps it legal.
    clock_seq = (random_bytes[0] << 8 | random_bytes[1]) & 0x3FFF | _VARIANT_RFC4122
    node = int.from_bytes(random_bytes[2:], "big")

    value = (milliseconds & 0xFFFFFFFFFFFF) << 80 | time_high << 64 | clock_seq << 48 | node
    return UUID(int=value)
