"""Route scene selection for the home payload.

Published route_media rows are the only photographs. A checkpoint photograph
is eligible only after that stop's own thresholds are met by the journey's
capped totals. This module reads the caller's transaction and does not
commit, roll back, or start another one.
"""

from __future__ import annotations

from decimal import Decimal

from .narrative import _asset_is_readable

_SCENE_ROLES = ("route_hero", "checkpoint_scene")


def select_scene(
    database,
    route_id: int,
    checkpoints: list[dict],
    credited_m: dict[str, Decimal],
) -> dict:
    """Choose the reached place, then the nearest published photograph behind it."""
    ordered = sorted(checkpoints, key=lambda row: (row["position"], row.get("id", 0)))
    reached = [checkpoint for checkpoint in ordered if _reached(checkpoint, credited_m)]
    library = _published_scenes(database, route_id)
    hero = _usable(library["hero"])
    if not reached:
        if hero is not None:
            return {
                "role": "route_hero",
                "checkpoint_slug": None,
                "image": hero,
                "fallback": None,
            }
        return {
            "role": "none",
            "checkpoint_slug": None,
            "image": None,
            "fallback": "none",
        }
    highest = reached[-1]
    for checkpoint in reversed(reached):
        image = _usable(library["scenes"].get(checkpoint["id"]))
        if image is None:
            continue
        return {
            "role": "checkpoint",
            "checkpoint_slug": highest["slug"],
            "image": image,
            "fallback": None if checkpoint["id"] == highest["id"] else "lower_checkpoint",
        }
    if hero is not None:
        return {
            "role": "route_hero",
            "checkpoint_slug": highest["slug"],
            "image": hero,
            "fallback": "route_hero",
        }
    return {
        "role": "none",
        "checkpoint_slug": highest["slug"],
        "image": None,
        "fallback": "none",
    }


def _published_scenes(database, route_id: int) -> dict:
    """One read for the route's published hero and checkpoint scenes."""
    rows = database.execute(
        """
        SELECT checkpoint_id, role, asset_path, alt_text, rights_basis,
               author, license, license_url, source_url
        FROM route_media
        WHERE route_id = ?
          AND status = 'published'
          AND role IN ('route_hero', 'checkpoint_scene')
        ORDER BY position, id
        """,
        (route_id,),
    ).fetchall()
    hero = None
    scenes: dict[int, dict] = {}
    for row in rows:
        if row["role"] == "route_hero":
            if hero is None:
                hero = row
            continue
        if row["role"] in _SCENE_ROLES and row["checkpoint_id"] not in scenes:
            scenes[row["checkpoint_id"]] = row
    return {"hero": hero, "scenes": scenes}


def _usable(row: dict | None) -> dict | None:
    if row is None or not _asset_is_readable(row["asset_path"]):
        return None
    return {
        "path": row["asset_path"],
        "alt": row["alt_text"],
        "credit": {
            "author": row["author"],
            "license": row["license"],
            "license_url": row["license_url"],
            "source_url": row["source_url"],
            "rights_basis": row["rights_basis"],
        },
    }


def _reached(checkpoint: dict, credited_m: dict[str, Decimal]) -> bool:
    thresholds = checkpoint.get("thresholds") or ()
    return bool(thresholds) and all(
        credited_m.get(metric, Decimal("0")) >= amount for metric, amount in thresholds
    )
