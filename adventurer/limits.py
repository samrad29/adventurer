"""Process-local failure limit for login, registration, and bad bearer tokens.

One worker is the boundary the architecture describes. The counts are not
stored in the database and do not survive a restart.
"""

from __future__ import annotations

import time

WINDOW_SECONDS = 15 * 60
LIMIT = 5
_failures: dict[str, list[float]] = {}


def too_many(key: str) -> bool:
    return len(_recent(key)) >= LIMIT


def record(key: str) -> None:
    hits = _recent(key)
    hits.append(time.time())
    _failures[key] = hits


def clear(key: str | None = None) -> None:
    if key is None:
        _failures.clear()
        return
    _failures.pop(key, None)


def _recent(key: str) -> list[float]:
    now = time.time()
    hits = [stamp for stamp in _failures.get(key, []) if now - stamp < WINDOW_SECONDS]
    _failures[key] = hits
    return hits
