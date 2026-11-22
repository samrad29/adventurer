"""Read-only home payload.

Journey lifecycle, credits, and invitations stay in their services. This
module only reads them and chooses the scene. It does not resume, restart,
accept, or complete anything.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from flask import current_app

from .db import get_db
from .expeditions import _journey_total, list_received_invitations
from .journeys import JourneyError, list_journeys
from .measures import feet_number
from .scene import select_scene
from .services import get_adventurer


def compose_home(user_id: int) -> dict:
    viewer = get_adventurer(user_id)
    if viewer is None:
        raise JourneyError("not_found", "That account does not exist.")
    journeys = list_journeys(user_id)
    current = [row for row in journeys if row["is_current"]]
    active = next((row for row in current if row["status"] == "active"), None)
    paused = sorted(
        (row for row in current if row["status"] == "paused"),
        key=lambda row: row["route_slug"],
    )
    finished = [
        row for row in current if row["status"] in ("completed", "abandoned")
    ]
    finished.sort(key=_finished_order)
    database = get_db()
    try:
        journey_body = None if active is None else _presentation(database, active)
        paused_bodies = [_presentation(database, row) for row in paused]
        if active is not None:
            state = "active"
            focus = {"journey_id": active["id"], "status": "active"}
        elif paused:
            state = "resume"
            focus = None
        elif finished:
            state = finished[0]["status"]
            focus = _presentation(database, finished[0], focus=True)
        else:
            state = "ready"
            focus = None
        actions = _actions(active, paused, finished, current)
        expeditions = _expeditions(
            database,
            user_id,
            None if active is None else active["id"],
        )
        invitations = _invitations(database, user_id)
    finally:
        database.rollback()
    return {
        "you": {"id": viewer["id"], "name": viewer["name"]},
        "state": state,
        "next_actions": actions,
        "journey": journey_body,
        "focus": focus,
        "paused_journeys": paused_bodies,
        "expeditions": expeditions,
        "incoming_invitations": invitations,
    }


def _actions(active, paused, finished, current) -> list[dict]:
    actions = []
    if active is not None:
        actions.append(
            {
                "type": "continue_journey",
                "journey_id": active["id"],
                "route_slug": active["route_slug"],
            }
        )
    for row in paused:
        actions.append(
            {
                "type": "resume_journey",
                "journey_id": row["id"],
                "route_slug": row["route_slug"],
            }
        )
    for row in sorted(finished, key=lambda item: item["route_slug"]):
        actions.append(
            {
                "type": "start_new_attempt",
                "journey_id": row["id"],
                "route_slug": row["route_slug"],
                "status": row["status"],
            }
        )
    if not current:
        actions.append({"type": "choose_route"})
    return actions


def _finished_order(journey: dict):
    stamp = journey["completed_at"] or journey["abandoned_at"]
    return (stamp is None, -(stamp.timestamp() if stamp else 0), journey["route_slug"])


def _presentation(database, journey: dict, *, focus: bool = False) -> dict:
    route = database.execute(
        """
        SELECT id, slug, name, region, target_elevation_m
        FROM routes
        WHERE id = ?
        """,
        (journey["route_id"],),
    ).fetchone()
    checkpoints = list(
        database.execute(
            """
            SELECT slug, name, position, elevation_m
            FROM route_checkpoints
            WHERE route_id = ?
            ORDER BY elevation_m, position
            """,
            (route["id"],),
        ).fetchall()
    )
    total_m, _last = _journey_total(database, journey["id"], None)
    target = route["target_elevation_m"]
    capped = total_m if total_m <= target else target
    current, nxt = _checkpoints(checkpoints, capped)
    body = {
        "status": journey["status"],
        "attempt_number": journey["attempt_number"],
        "progress_version": journey["progress_version"],
        "total_ft": feet_number(capped),
        "remaining_ft": feet_number(target - capped),
        "percent": _percent(capped, target),
        "summited": capped >= target and target > 0,
        "route": {
            "slug": route["slug"],
            "name": route["name"],
            "region": route["region"],
            "summit_ft": feet_number(target),
        },
        "checkpoint": {"current": current, "next": nxt},
        "scene": select_scene(route["slug"], checkpoints, capped, _photos_root()),
    }
    if focus:
        body["journey_id"] = journey["id"]
    else:
        body["id"] = journey["id"]
    return body


def _checkpoints(checkpoints, total_m: Decimal):
    current = None
    nxt = None
    for checkpoint in checkpoints:
        item = {
            "slug": checkpoint["slug"],
            "name": checkpoint["name"],
            "elevation_ft": feet_number(checkpoint["elevation_m"]),
        }
        if checkpoint["elevation_m"] <= total_m:
            current = item
        elif nxt is None:
            nxt = item
    return current, nxt


def _percent(capped: Decimal, target: Decimal) -> float:
    if target <= 0:
        return 0.0
    ratio = (capped / target * Decimal("100")).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return float(ratio)


def _expeditions(database, user_id: int, active_journey_id: int | None) -> list[dict]:
    rows = database.execute(
        """
        SELECT expedition.slug, expedition.name, expedition.kind, expedition.status,
               expedition.accent_color, expedition.welcome_override, expedition.image_ref,
               route.slug AS route_slug, route.target_elevation_m,
               participation.journey_id, participation.display_credits_after,
               organization.slug AS organization_slug,
               organization.name AS organization_name,
               organization.logo_ref, organization.image_ref AS organization_image_ref,
               organization.colors, organization.welcome_message
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        JOIN routes AS route ON route.id = expedition.route_id
        LEFT JOIN organizations AS organization
          ON organization.id = expedition.organization_id
        WHERE participation.user_id = ?
          AND participation.status = 'active'
        ORDER BY expedition.slug
        """,
        (user_id,),
    ).fetchall()
    cards = []
    for row in rows:
        total_m, _last = _journey_total(
            database, row["journey_id"], row["display_credits_after"]
        )
        target = row["target_elevation_m"]
        capped = total_m if total_m <= target else target
        card = {
            "slug": row["slug"],
            "name": row["name"],
            "kind": row["kind"],
            "status": row["status"],
            "route_slug": row["route_slug"],
            "contributing": row["journey_id"] == active_journey_id,
            "displayed_total_ft": feet_number(capped),
            "organization": None,
        }
        if row["kind"] == "private":
            card["accent_color"] = row["accent_color"]
            card["welcome_override"] = row["welcome_override"]
            card["image_ref"] = row["image_ref"]
        elif row["kind"] == "organization":
            card["organization"] = {
                "slug": row["organization_slug"],
                "name": row["organization_name"],
                "logo_ref": row["logo_ref"],
                "image_ref": row["organization_image_ref"],
                "colors": row["colors"],
                "welcome_message": row["welcome_message"],
            }
        cards.append(card)
    return cards


def _invitations(database, user_id: int) -> list[dict]:
    received = list_received_invitations(user_id)
    if not received:
        return []
    ids = [row["expedition_id"] for row in received]
    placeholders = ", ".join("?" for _ in ids)
    found = {
        row["id"]: row
        for row in database.execute(
            f"""
            SELECT expedition.id, expedition.slug, expedition.name, route.slug AS route_slug
            FROM expeditions AS expedition
            JOIN routes AS route ON route.id = expedition.route_id
            WHERE expedition.id IN ({placeholders})
            """,
            tuple(ids),
        ).fetchall()
    }
    rows = []
    for invitation in received:
        expedition = found.get(invitation["expedition_id"])
        if expedition is None:
            continue
        actions = [{"type": "accept_invitation", "invitation_id": invitation["id"]}]
        if invitation["kind"] == "username":
            actions.append(
                {"type": "decline_invitation", "invitation_id": invitation["id"]}
            )
        rows.append(
            {
                "id": invitation["id"],
                "kind": invitation["kind"],
                "expires_at": invitation["expires_at"],
                "expedition": {
                    "slug": expedition["slug"],
                    "name": expedition["name"],
                    "route_slug": expedition["route_slug"],
                },
                "actions": actions,
            }
        )
    return rows


def _photos_root() -> Path:
    configured = current_app.config.get("PHOTOS_DIR")
    if configured:
        return Path(configured)
    return Path(current_app.static_folder) / "photos"
