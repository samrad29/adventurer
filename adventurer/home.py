"""Read-only home payload.

Journey lifecycle, credits, and invitations stay in their services. This
module only reads them and chooses the scene. It does not resume, restart,
accept, or complete anything.

One home response is one repeatable-read, read-only transaction, so journey
status, progress version, metric totals, and pending arrivals come from the
same snapshot. That setting ends when the transaction rolls back. Later work
on the connection stays read committed. Helpers that commit or roll back are
not called after the snapshot starts.
"""

from __future__ import annotations

from decimal import Decimal

from .branding import resolve_branding
from .db import get_db
from .expeditions import board_metrics, read_received_invitations
from .journey_progress import CheckpointMark, read_journey_progress
from .journeys import JourneyError, read_journeys
from .metrics import MetricProgress, assess_metric
from .narrative import pending_arrivals
from .scene import select_scene
from .services import read_adventurer


def compose_home(user_id: int) -> dict:
    database = get_db()
    try:
        _begin_consistent_read(database)
        viewer = read_adventurer(database, user_id)
        if viewer is None:
            raise JourneyError("not_found", "That account does not exist.")
        journeys = read_journeys(database, user_id)
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
        actions = _policy_actions(database, user_id) + _actions(active, paused, finished, current)
        expeditions = _expeditions(
            database,
            user_id,
            None if active is None else active["id"],
        )
        invitations = _invitations(database, user_id)
    except JourneyError:
        database.rollback()
        raise
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


def progress_metrics(database, journey_id: int) -> list[dict]:
    """Display metrics for one journey. The caller owns the transaction."""
    progress = read_journey_progress(database, journey_id)
    return [metric_payload(metric) for metric in progress.metrics]


def metric_payload(metric: MetricProgress) -> dict:
    return {
        "metric": metric.code,
        "unit": metric.display_unit,
        "target": plain_display(metric.target_display),
        "credited": plain_display(metric.credited_display),
        "remaining": plain_display(metric.remaining_display),
        "complete": metric.complete,
    }


def plain_display(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _policy_actions(database, user_id: int) -> list[dict]:
    from .activity_policy import expansion_state

    rows = database.execute(
        """
        SELECT choice.journey_id, choice.reason, route.slug AS route_slug,
               former.slug AS former_slug
        FROM journey_policy_choices AS choice
        JOIN route_journeys AS journey ON journey.id = choice.journey_id
        JOIN routes AS route ON route.id = journey.route_id
        LEFT JOIN expeditions AS former ON former.id = choice.former_expedition_id
        WHERE journey.user_id = ?
        ORDER BY choice.journey_id
        """,
        (user_id,),
    ).fetchall()
    actions = []
    for row in rows:
        state = expansion_state(database, row["journey_id"])
        action = {
            "type": "resolve_activity_policy",
            "journey_id": row["journey_id"],
            "route_slug": row["route_slug"],
            "reason": row["reason"],
            "former_expedition_slug": row["former_slug"],
            "choices": ["keep"],
        }
        if state["available"]:
            action["choices"].append("expand")
        elif state["blocked_by"]:
            action["blocked_by"] = state["blocked_by"]
        actions.append(action)
    return actions


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
        SELECT id, slug, name, region
        FROM routes
        WHERE id = ?
        """,
        (journey["route_id"],),
    ).fetchone()
    progress = read_journey_progress(database, journey["id"])
    credited = {metric.code: metric.credited_m for metric in progress.metrics}
    current = _checkpoint_body(progress.highest_checkpoint)
    nxt = _checkpoint_body(progress.next_checkpoint)
    body = {
        "status": progress.status,
        "attempt_number": journey["attempt_number"],
        "progress_version": progress.progress_version,
        "metrics": [metric_payload(metric) for metric in progress.metrics],
        "route": {
            "slug": route["slug"],
            "name": route["name"],
            "region": route["region"],
            "requirements": [
                {
                    "metric": metric.code,
                    "unit": metric.display_unit,
                    "target": plain_display(metric.target_display),
                }
                for metric in progress.metrics
            ],
        },
        "checkpoint": {"current": current, "next": nxt},
        "scene": select_scene(
            database,
            route["id"],
            [_scene_checkpoint(mark) for mark in progress.checkpoints],
            credited,
        ),
    }
    if not focus and progress.status == "active":
        body["pending_arrivals"] = pending_arrivals(
            database, progress.journey_id, progress.checkpoints
        )
    if focus:
        body["journey_id"] = journey["id"]
    else:
        body["id"] = journey["id"]
    return body


def _checkpoint_body(mark: CheckpointMark | None) -> dict | None:
    if mark is None:
        return None
    return {
        "slug": mark.slug,
        "name": mark.name,
        "position": mark.position,
        "requirements": _threshold_payload(mark.thresholds),
    }


def _scene_checkpoint(mark: CheckpointMark) -> dict:
    return {
        "id": mark.id,
        "slug": mark.slug,
        "position": mark.position,
        "thresholds": mark.thresholds,
    }


def _threshold_payload(thresholds) -> list[dict]:
    rows = []
    for code, amount in sorted(thresholds, key=lambda item: item[0]):
        shown = assess_metric(code, amount, Decimal("0"))
        rows.append(
            {
                "metric": code,
                "unit": shown.display_unit,
                "threshold": plain_display(shown.target_display),
            }
        )
    return rows


def _metric_from_amounts(code: str, target_m: Decimal, credited_m: Decimal) -> dict:
    return metric_payload(assess_metric(code, target_m, credited_m))


def _expeditions(database, user_id: int, active_journey_id: int | None) -> list[dict]:
    rows = database.execute(
        """
        SELECT expedition.slug, expedition.name, expedition.kind, expedition.status,
               expedition.accent_color, expedition.welcome_override, expedition.image_ref,
               route.id AS route_id, route.slug AS route_slug,
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
        visible = board_metrics(
            database, row["journey_id"], row["route_id"], row["display_credits_after"]
        )
        card = {
            "slug": row["slug"],
            "name": row["name"],
            "kind": row["kind"],
            "status": row["status"],
            "route_slug": row["route_slug"],
            "contributing": row["journey_id"] == active_journey_id,
            "displayed_metrics": [
                _metric_from_amounts(item["metric"], item["target_m"], item["credited_m"])
                for item in visible
            ],
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
        else:
            card["organization"] = None
        card["branding"] = _card_branding(row)
        cards.append(card)
    return cards


def _card_branding(row) -> dict:
    organization = None
    if row["kind"] == "organization":
        organization = {
            "logo_ref": row["logo_ref"],
            "image_ref": row["organization_image_ref"],
            "colors": row["colors"],
            "welcome_message": row["welcome_message"],
        }
    return resolve_branding(kind=row["kind"], organization=organization, expedition=row)


def _begin_consistent_read(database) -> None:
    """One snapshot for every select in this read. Does not change the session default."""
    database.rollback()
    database.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")


def _invitations(database, user_id: int) -> list[dict]:
    received = read_received_invitations(database, user_id)
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
                "branding": invitation["branding"],
                "actions": actions,
            }
        )
    return rows

