"""Who may read or change an expedition.

The application connects as a database role that bypasses row level security.
Private expeditions stay private only when every read uses these checks.
"""

from __future__ import annotations

from datetime import datetime, timezone


def is_owner(expedition: dict, user_id: int | None) -> bool:
    owner = expedition.get("owner_user_id")
    return user_id is not None and owner is not None and owner == user_id


def can_read(expedition: dict, user_id: int | None, participation_status: str | None) -> bool:
    if expedition["visibility"] == "public":
        return True
    if is_owner(expedition, user_id):
        return True
    return participation_status == "active"


def can_manage(expedition: dict, user_id: int | None) -> bool:
    return is_owner(expedition, user_id)


def can_join(expedition: dict) -> bool:
    return (
        expedition["visibility"] == "public"
        and expedition["status"] == "active"
        and expedition["route_status"] == "published"
    )


def can_add_credit(
    expedition: dict,
    participation_status: str | None,
    moment: datetime | None = None,
) -> bool:
    if participation_status != "active" or expedition["status"] != "active":
        return False
    if expedition["route_status"] != "published":
        return False
    moment = moment or datetime.now(timezone.utc)
    starts = expedition.get("starts_at")
    ends = expedition.get("ends_at")
    if starts is not None and moment < starts:
        return False
    if ends is not None and moment > ends:
        return False
    return True
