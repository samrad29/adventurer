"""Process-local failure limit for login, registration, and bad bearer tokens.

One worker is the boundary the architecture describes. The counts are not
stored in the database and do not survive a restart. Expired timestamps are
discarded, and a key with no failures left is removed.
"""

from __future__ import annotations

import threading
import time

WINDOW_SECONDS = 15 * 60
LIMIT = 5
_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


def too_many(key: str) -> bool:
    with _lock:
        return len(_recent(key)) >= LIMIT


def record(key: str) -> None:
    with _lock:
        hits = _recent(key)
        hits.append(time.time())
        _failures[key] = hits


def clear(key: str | None = None) -> None:
    with _lock:
        if key is None:
            _failures.clear()
            return
        _failures.pop(key, None)


def _recent(key: str) -> list[float]:
    """Return the live failures for a key. Caller must hold _lock."""
    now = time.time()
    hits = [stamp for stamp in _failures.get(key, ()) if now - stamp < WINDOW_SECONDS]
    if hits:
        _failures[key] = hits
    else:
        _failures.pop(key, None)
    return hits
