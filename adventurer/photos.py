"""Credits for the photos bundled with each stop."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def photo_credits() -> dict:
    path = Path(__file__).with_name("photo_credits.json")
    return json.loads(path.read_text(encoding="utf-8"))


def credit_for(slug: str) -> dict | None:
    return photo_credits().get(slug)
