"""Route scene selection for the home payload.

Checkpoint reach uses each stop's requirement thresholds and the journey's
capped storage totals. A photograph is used only after that stop is reached.
This module does not read or write the database.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from .photos import credit_for

PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def select_scene(
    route_slug: str,
    checkpoints: list[dict],
    credited_m: dict[str, Decimal],
    root: Path,
) -> dict:
    """Choose the reached place, then the nearest usable photograph behind it."""
    ordered = sorted(checkpoints, key=lambda row: (row["position"], row.get("id", 0)))
    reached_index = None
    for index, checkpoint in enumerate(ordered):
        if _reached(checkpoint, credited_m):
            reached_index = index
        else:
            break
    if reached_index is None:
        image = _hero(route_slug, root)
        return {
            "role": "route_hero" if image is not None else "none",
            "checkpoint_slug": None,
            "image": image,
            "fallback": None if image is not None else "none",
        }
    reached = ordered[reached_index]
    for index in range(reached_index, -1, -1):
        image = _checkpoint_image(route_slug, ordered[index]["slug"], root)
        if image is not None:
            fallback = None if index == reached_index else "lower_checkpoint"
            return {
                "role": "checkpoint",
                "checkpoint_slug": reached["slug"],
                "image": image,
                "fallback": fallback,
            }
    image = _hero(route_slug, root)
    if image is not None:
        return {
            "role": "route_hero",
            "checkpoint_slug": reached["slug"],
            "image": image,
            "fallback": "route_hero",
        }
    return {
        "role": "none",
        "checkpoint_slug": reached["slug"],
        "image": None,
        "fallback": "none",
    }


def _reached(checkpoint: dict, credited_m: dict[str, Decimal]) -> bool:
    thresholds = checkpoint.get("thresholds") or ()
    return bool(thresholds) and all(
        credited_m.get(metric, Decimal("0")) >= amount for metric, amount in thresholds
    )


def _checkpoint_image(route_slug: str, checkpoint_slug: str, root: Path) -> dict | None:
    path = _find(root, f"{route_slug}/{checkpoint_slug}", checkpoint_slug)
    if path is None:
        return None
    return _image(path, credit_for(checkpoint_slug), checkpoint_slug)


def _hero(route_slug: str, root: Path) -> dict | None:
    path = _find(root, f"{route_slug}/hero", route_slug)
    if path is None:
        return None
    return _image(path, credit_for(route_slug), route_slug)


def _image(path: str, credit: dict | None, slug: str) -> dict:
    alt = None if credit is None else credit.get("alt")
    if not alt:
        alt = slug.replace("-", " ")
    public_credit = None
    if credit is not None:
        public_credit = {
            "author": credit.get("author"),
            "license": credit.get("license"),
            "license_url": credit.get("license_url"),
            "source_url": credit.get("source_url"),
        }
    return {"path": path, "alt": alt, "credit": public_credit}


def _find(root: Path, *stems: str) -> str | None:
    for stem in stems:
        for ext in PHOTO_EXTS:
            if (root / f"{stem}{ext}").is_file():
                return f"/static/photos/{stem}{ext}"
    return None
