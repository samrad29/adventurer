"""Personal activities and the journey credits they may earn.

An activity stores the elevation and distance that were recorded. A journey
credit is the part of one required metric that still fits that route. One
activity has at most one assignment. Elevation and distance are credited
separately, and neither one finishes the journey while the other is short.

Preview reads are not writes. A save that would finish every required metric
recalculates the credits under the journey lock and checks a signed token
against that result. The token is not a source of amounts.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import date, datetime
from decimal import Decimal

from flask import current_app

from .allocation import MeasuredAssignment, allocate, allocation_fingerprint, measurement_token, totals_for
from .db import get_db
from .journey_progress import CheckpointMark, assess_checkpoints, read_journey_progress
from .metrics import FOOT_IN_METERS, MILE_IN_METERS, STORAGE_QUANTUM, MetricError, parse_measurement
from .narrative import arrival_reveals

MANUAL_EVENT_REASON = "Manual entry. Manual entries are not independently verified."
TOKEN_TTL_SECONDS = 600
MAX_ELEVATION_M = Decimal("10000") * FOOT_IN_METERS
MAX_DISTANCE_M = Decimal("75") * MILE_IN_METERS
MAX_CYCLE_DISTANCE_M = Decimal("200") * MILE_IN_METERS
MAX_NOTE = 280
ACTIVITY_TYPES = frozenset(
    {
        "run",
        "hike",
        "walk",
        "stair_climb",
        "other",
        "unspecified",
        "cycle",
        "indoor_cycle",
    }
)
CYCLING_TYPES = frozenset({"cycle", "indoor_cycle"})
_FIELDS = frozenset(
    {
        "elevation_ft",
        "distance_mi",
        "happened_on",
        "note",
        "activity_type",
        "duration_s",
        "started_at",
        "ended_at",
    }
)
_COMPLETED_JOURNEY = (
    "credits on a completed journey cannot be changed",
    "an activity credited to a completed journey cannot be deleted",
    "an assignment on a completed journey cannot be changed",
    "elevation credited to a completed journey cannot change",
    "distance credited to a completed journey cannot change",
)


class ActivityError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **details) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def translate_database_error(exc: BaseException) -> ActivityError | None:
    sqlstate = getattr(exc, "sqlstate", None)
    message = str(exc)
    if sqlstate == "23505":
        return ActivityError(
            "conflict",
            "The activity changed during this update. Try again.",
            status=409,
        )
    if sqlstate == "23514":
        return ActivityError(
            "invalid",
            "That activity does not meet the recording rules.",
            status=400,
        )
    if sqlstate == "P0001" and any(text in message for text in _COMPLETED_JOURNEY):
        return ActivityError(
            "invalid_state",
            "A completed journey cannot be changed that way.",
            status=409,
        )
    if sqlstate == "P0001" and (
        "credited amount cannot exceed the activity measurement" in message
        or "cannot fall below the credited amount" in message
        or "credit exceeds the route target" in message
    ):
        return ActivityError(
            "invalid",
            "The credited amount is not valid for this activity.",
            status=400,
        )
    return None


def preview_activity(user_id: int, fields: dict) -> dict:
    """Read-only preview of a new activity and the credits it would add."""
    changes = _changes(fields, creating=True)
    database = get_db()
    try:
        _require_user(database, user_id, lock=False)
        _validate_distance(changes["activity_type"], changes.get("distance_m"))
        return _preview_new(database, user_id, changes)
    except ActivityError:
        database.rollback()
        raise
    finally:
        database.rollback()


def create_activity(
    user_id: int,
    fields: dict,
    *,
    confirmation_token: str | None = None,
) -> dict:
    """Store one activity and, when the active journey can take it, its credits."""
    changes = _changes(fields, creating=True)

    def write(database):
        _require_user(database, user_id, lock=True)
        journey = _lock_active_journey(database, user_id)
        measurements = _measurements_from_changes(changes)
        _validate_distance(changes["activity_type"], measurements.get("distance"))
        if journey is None:
            if confirmation_token:
                raise _confirmation(_empty_preview(measurements))
            activity_id = _insert_activity(database, user_id, changes)
            saved = _with_arrivals(_saved(database, activity_id, None), [])
            saved["no_credit_reason"] = "no_active_journey"
            return saved, True
        assigned = _positive_required(journey, measurements)
        proposal = _plan(
            database,
            journey,
            operation="create",
            activity_id=None,
            assignment_id=None,
            activity_type=changes["activity_type"],
            measurements=measurements,
            previous_credits={},
            allocate_now=assigned,
        )
        if proposal["resume_required"]:
            raise ActivityError(
                "resume_required",
                "Resume this journey before it can be completed.",
                status=409,
                journey_id=journey["id"],
                journey_status=journey["status"],
            )
        _require_confirmation(confirmation_token, user_id, journey, proposal)
        activity_id = _insert_activity(database, user_id, changes)
        if assigned:
            _bind_new_assignment(
                proposal["slots"],
                _insert_assignment(database, activity_id, user_id, journey),
            )
            _sync_shares(database, journey, proposal["slots"], proposal["shares"], "reduce")
            _sync_shares(database, journey, proposal["slots"], proposal["shares"], "increase")
        reached = []
        if proposal["credits"] or proposal["will_complete"] or assigned:
            _touch_journey(
                database,
                journey["id"],
                credit_changed=bool(proposal["credits"]) or proposal["will_complete"],
                complete=proposal["will_complete"],
                inputs_changed=assigned and not proposal["credits"] and not proposal["will_complete"],
            )
        if proposal["credits"] or proposal["will_complete"]:
            reached = _newly_reached(
                database,
                journey["route_id"],
                _totals_map(journey),
                proposal["resulting"],
            )
            _write_create_feed(database, user_id, activity_id, journey, proposal, reached)
        saved = _with_arrivals(
            _saved(database, activity_id, journey["id"] if assigned else None),
            arrival_reveals(database, journey["id"], reached),
        )
        saved["no_credit_reason"] = _no_credit_reason(
            journey, measurements, proposal["credits"]
        )
        return saved, True

    return _commit(write)


def preview_activity_update(user_id: int, activity_id: int, fields: dict) -> dict:
    """Read-only preview of an edit, including replacement credits.

    For each required metric, ``current_m`` is the journey total minus this
    activity's existing credit. ``remaining_m``, ``credit_m``, and
    ``resulting_m`` use that same baseline. ``existing_credit_m`` is the
    credit the edit would replace. A new activity has no credit to subtract,
    so its baseline is the journey total.

    A paused or abandoned journey that would reach every target sets
    ``resume_required`` and does not issue a token. A completing save uses
    the activity summit token, which binds the allocation the same walk
    would commit.
    """
    _require_id(activity_id, "activity")
    changes = _changes(fields, creating=False)
    database = get_db()
    try:
        _require_user(database, user_id, lock=False)
        loaded = _load_owned(database, user_id, activity_id)
        measurements = _merged_measurements(loaded, changes)
        _validate_distance(
            changes.get("activity_type", loaded["activity_type"]),
            measurements.get("distance"),
        )
        return _preview_edit(database, user_id, loaded, changes)
    except ActivityError:
        database.rollback()
        raise
    finally:
        database.rollback()


def update_activity(
    user_id: int,
    activity_id: int,
    fields: dict,
    *,
    confirmation_token: str | None = None,
) -> dict:
    """Change the caller's activity and recalculate each metric credit."""
    _require_id(activity_id, "activity")
    changes = _changes(fields, creating=False)

    def write(database):
        _require_user(database, user_id, lock=True)
        loaded, journey = _lock_for_update(database, user_id, activity_id, changes)
        measurements = _merged_measurements(loaded, changes)
        _validate_distance(
            changes.get("activity_type", loaded["activity_type"]),
            measurements.get("distance"),
        )
        credit_journey = journey if _credits_can_change(loaded, journey, changes) else None
        if credit_journey is not None and credit_journey["status"] == "completed":
            _reject_completed_progress_edit(loaded, changes)
            _apply_changes(database, activity_id, loaded, changes)
            saved = _with_arrivals(
                _saved(database, activity_id, loaded["journey_id"]), []
            )
            saved["no_credit_reason"] = _no_credit_reason(
                credit_journey, measurements, loaded["credits"]
            )
            return saved, True
        measurement_changed = _measurement_inputs_changed(loaded, changes)
        if credit_journey is None or not measurement_changed:
            if confirmation_token:
                raise _confirmation(_preview_edit(database, user_id, loaded, changes))
            _apply_changes(database, activity_id, loaded, changes)
            saved = _with_arrivals(
                _saved(database, activity_id, loaded["journey_id"]), []
            )
            saved["no_credit_reason"] = _no_credit_reason(
                credit_journey, measurements, loaded["credits"]
            )
            return saved, True
        activity_type = changes.get("activity_type", loaded["activity_type"])
        proposal = _plan(
            database,
            credit_journey,
            operation="update",
            activity_id=activity_id,
            assignment_id=loaded["assignment_id"],
            activity_type=activity_type,
            measurements=measurements,
            previous_credits=loaded["credits"],
            allocate_now=True,
        )
        if proposal["resume_required"]:
            raise ActivityError(
                "resume_required",
                "Resume this journey before it can be completed.",
                status=409,
                journey_id=credit_journey["id"],
                journey_status=credit_journey["status"],
            )
        _require_confirmation(confirmation_token, user_id, credit_journey, proposal)
        _sync_shares(database, credit_journey, proposal["slots"], proposal["shares"], "reduce")
        _apply_changes(database, activity_id, loaded, changes)
        assigning = loaded["assignment_id"] is not None or _positive_required(
            credit_journey, measurements
        )
        if assigning and loaded["assignment_id"] is None:
            _bind_new_assignment(
                proposal["slots"],
                _insert_assignment(database, activity_id, user_id, credit_journey),
            )
        elif assigning:
            _bind_existing_assignment(proposal["slots"], activity_id, loaded["assignment_id"])
        if assigning:
            _sync_shares(
                database, credit_journey, proposal["slots"], proposal["shares"], "increase"
            )
        journey_id = credit_journey["id"] if assigning else None
        progress_changed = (
            proposal["allocation_changed"]
            or measurement_changed
            or (assigning and loaded["assignment_id"] is None)
        )
        if proposal["will_complete"] or progress_changed:
            _touch_journey(
                database,
                credit_journey["id"],
                credit_changed=proposal["allocation_changed"] or proposal["will_complete"],
                complete=proposal["will_complete"],
                inputs_changed=progress_changed and not proposal["allocation_changed"],
            )
        reached = _newly_reached(
            database,
            credit_journey["route_id"],
            _totals_map(credit_journey),
            proposal["resulting"],
        )
        if proposal["will_complete"]:
            _write_summit_feed(
                database,
                user_id,
                activity_id,
                credit_journey,
                proposal["resulting"],
            )
        saved = _with_arrivals(
            _saved(database, activity_id, journey_id),
            arrival_reveals(database, credit_journey["id"], reached),
        )
        saved["no_credit_reason"] = _no_credit_reason(
            credit_journey, measurements, proposal["credits"]
        )
        return saved, True

    return _commit(write)


def delete_activity(user_id: int, activity_id: int) -> dict:
    """Delete the caller's activity and every credit on its assignment."""
    _require_id(activity_id, "activity")

    def write(database):
        _require_user(database, user_id, lock=True)
        owner = _owner_row(database, user_id, activity_id)
        if owner is None:
            raise ActivityError("not_found", "That activity does not exist.", status=404)
        hint = _assignment_journey_id(database, activity_id)
        journey = None
        if hint is not None:
            journey = _lock_credit_journey(database, user_id, hint)
        database.execute(
            "SELECT id FROM activities WHERE id = ? FOR UPDATE",
            (activity_id,),
        )
        loaded = _load_owned(database, user_id, activity_id)
        if loaded["journey_id"] != hint:
            raise ActivityError(
                "conflict",
                "The activity changed during this update. Try again.",
                status=409,
            )
        if (
            loaded["assignment_id"] is not None
            and journey is not None
            and journey["status"] == "completed"
        ):
            raise ActivityError(
                "invalid_state",
                "An activity credited to a completed journey cannot be deleted.",
                status=409,
            )
        if loaded["assignment_id"] is not None and journey is not None:
            requirements = _requirement_pairs(journey)
            slots = [
                slot
                for slot in _load_slots(database, journey["id"])
                if slot.activity_id != activity_id
            ]
            shares = allocate(slots, requirements)
            _sync_shares(database, journey, slots, shares, "reduce")
            _touch_journey(
                database,
                journey["id"],
                credit_changed=True,
                complete=False,
                inputs_changed=True,
            )
        database.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
        if loaded["assignment_id"] is not None and journey is not None:
            _sync_shares(database, journey, slots, shares, "increase")
        summary = None if journey is None else _saved_journey(database, journey["id"])
        return {"activity_id": activity_id, "journey": summary, "arrivals": []}, True

    return _commit(write)


def list_activities(user_id: int) -> list[dict]:
    """The caller's activities, newest first, with any metric credits beside them."""
    database = get_db()
    try:
        _require_user(database, user_id, lock=False)
        rows = database.execute(
            f"""
            SELECT {_ACTIVITY_COLUMNS},
                   assignment.journey_id,
                   journey.status AS journey_status,
                   journey.route_id AS credit_route_id,
                   route.slug AS route_slug,
                   credit.metric_code,
                   credit.credited_amount
            FROM activities AS activity
            LEFT JOIN activity_journey_assignments AS assignment
                ON assignment.activity_id = activity.id
            LEFT JOIN journey_credits AS credit
                ON credit.assignment_id = assignment.id
            LEFT JOIN route_journeys AS journey ON journey.id = assignment.journey_id
            LEFT JOIN routes AS route ON route.id = journey.route_id
            WHERE activity.user_id = ?
            ORDER BY activity.activity_date DESC, activity.id DESC, credit.metric_code
            """,
            (user_id,),
        ).fetchall()
        return _group_history(rows)
    except ActivityError:
        database.rollback()
        raise
    finally:
        database.rollback()


def get_activity(user_id: int, activity_id: int) -> dict:
    """One of the caller's activities, including metric credits when they exist."""
    _require_id(activity_id, "activity")
    database = get_db()
    try:
        _require_user(database, user_id, lock=False)
        rows = _history_rows(database, activity_id)
        if not rows:
            raise ActivityError("not_found", "That activity does not exist.", status=404)
        if rows[0]["user_id"] != user_id:
            raise ActivityError(
                "forbidden",
                "You cannot open another person's activity.",
                status=403,
            )
        return _group_history(rows)[0]
    except ActivityError:
        database.rollback()
        raise
    finally:
        database.rollback()


def _commit(write) -> dict:
    database = get_db()
    try:
        result, changed = write(database)
        if changed:
            database.commit()
        else:
            database.rollback()
    except Exception as exc:
        _rollback_preserving(database, exc)
        if isinstance(exc, ActivityError):
            raise
        translated = translate_database_error(exc)
        if translated is not None:
            raise translated from exc
        raise
    return result


def _rollback_preserving(database, original: BaseException) -> None:
    try:
        database.rollback()
    except Exception as rollback_error:
        raise original from rollback_error


def _confirmation(preview: dict) -> ActivityError:
    return ActivityError(
        "confirmation_required",
        "Confirm the summit before saving this activity.",
        status=409,
        preview=preview,
    )


def _require_confirmation(token, user_id: int, journey: dict, proposal: dict) -> None:
    if token is None and not proposal["will_complete"]:
        return
    if proposal["will_complete"] and _token_matches(token, user_id, journey, proposal):
        return
    raise _confirmation(_preview_from_proposal(journey, proposal))


def _require_user(database, user_id: int, *, lock: bool) -> None:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ActivityError("not_found", "That account does not exist.", status=404)
    locking = " FOR UPDATE" if lock else ""
    row = database.execute(
        f"SELECT id, deletion_state FROM users WHERE id = ?{locking}",
        (user_id,),
    ).fetchone()
    if row is None:
        raise ActivityError("not_found", "That account does not exist.", status=404)
    if row["deletion_state"] == "deleting":
        raise ActivityError(
            "forbidden",
            "This account is being deleted.",
            status=403,
        )


def _require_id(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ActivityError("not_found", f"That {label} does not exist.", status=404)


def _lock_active_journey(database, user_id: int) -> dict | None:
    """Lock the caller's active journey after the expeditions that can hear about it."""
    found = database.execute(
        """
        SELECT id
        FROM route_journeys
        WHERE user_id = ? AND status = 'active'
        """,
        (user_id,),
    ).fetchone()
    if found is None:
        return None
    journey = _lock_credit_journey(database, user_id, found["id"])
    if journey["status"] != "active" or journey["user_id"] != user_id:
        raise ActivityError(
            "conflict",
            "The journey changed during this update. Try again.",
            status=409,
        )
    return journey


def _lock_credit_journey(database, user_id: int, journey_id: int) -> dict:
    """Lock expeditions, then this journey, then its active participations.

    The caller already holds the user row. Metric totals are read after the
    journey lock and before any credit write.
    """
    found = database.execute(
        """
        SELECT id, user_id, route_id
        FROM route_journeys
        WHERE id = ?
        """,
        (journey_id,),
    ).fetchone()
    if found is None or found["user_id"] != user_id:
        raise ActivityError(
            "conflict",
            "The activity changed during this update. Try again.",
            status=409,
        )
    _lock_expeditions(database, user_id, found["id"], found["route_id"])
    locked = database.execute(
        """
        SELECT id, user_id, route_id, status, progress_version
        FROM route_journeys
        WHERE id = ?
        FOR UPDATE
        """,
        (journey_id,),
    ).fetchone()
    if locked is None or locked["user_id"] != user_id:
        raise ActivityError(
            "conflict",
            "The journey changed during this update. Try again.",
            status=409,
        )
    participations = _lock_participations(database, user_id, locked["id"], locked["route_id"])
    state = _journey_state(database, locked)
    state["participations"] = participations
    return state


def _lock_expeditions(database, user_id: int, journey_id: int, route_id: int) -> None:
    database.execute(
        """
        SELECT expedition.id
        FROM expeditions AS expedition
        WHERE expedition.status = 'active'
          AND expedition.route_id = ?
          AND EXISTS (
              SELECT 1
              FROM expedition_participations AS participation
              WHERE participation.expedition_id = expedition.id
                AND participation.user_id = ?
                AND participation.status = 'active'
                AND participation.journey_id = ?
          )
        ORDER BY expedition.id
        FOR UPDATE
        """,
        (route_id, user_id, journey_id),
    )


def _lock_participations(database, user_id: int, journey_id: int, route_id: int) -> list[dict]:
    database.execute(
        """
        SELECT participation.id
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ?
          AND participation.journey_id = ?
          AND participation.status = 'active'
          AND expedition.status = 'active'
          AND expedition.route_id = ?
        ORDER BY participation.id
        FOR UPDATE OF participation
        """,
        (user_id, journey_id, route_id),
    )
    rows = database.execute(
        """
        SELECT participation.id, participation.expedition_id
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ?
          AND participation.journey_id = ?
          AND participation.status = 'active'
          AND expedition.status = 'active'
          AND expedition.route_id = ?
        ORDER BY participation.id
        """,
        (user_id, journey_id, route_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _journey_state(database, locked: dict) -> dict:
    route = database.execute(
        "SELECT slug FROM routes WHERE id = ?",
        (locked["route_id"],),
    ).fetchone()
    requirements = database.execute(
        """
        SELECT metric_code, target_amount
        FROM route_requirements
        WHERE route_id = ?
        ORDER BY metric_code
        """,
        (locked["route_id"],),
    ).fetchall()
    totals = {
        row["metric_code"]: row["credited_total"]
        for row in database.execute(
            """
            SELECT metric_code, credited_total
            FROM journey_metric_totals
            WHERE journey_id = ?
            """,
            (locked["id"],),
        ).fetchall()
    }
    return {
        "id": locked["id"],
        "user_id": locked["user_id"],
        "route_id": locked["route_id"],
        "route_slug": route["slug"],
        "status": locked["status"],
        "progress_version": locked["progress_version"],
        "requirements": [dict(row) for row in requirements],
        "totals": totals,
        "participations": [],
    }


def _lock_for_update(database, user_id: int, activity_id: int, changes: dict) -> tuple[dict, dict | None]:
    """Lock the assigned or active journey before the activity row."""
    if _owner_row(database, user_id, activity_id) is None:
        raise ActivityError("not_found", "That activity does not exist.", status=404)
    hint = _assignment_journey_id(database, activity_id)
    journey = None
    if hint is not None:
        journey = _lock_credit_journey(database, user_id, hint)
    elif "elevation_m" in changes or "distance_m" in changes:
        journey = _lock_active_journey(database, user_id)
    database.execute(
        "SELECT id FROM activities WHERE id = ? FOR UPDATE",
        (activity_id,),
    )
    loaded = _load_owned(database, user_id, activity_id)
    if loaded["journey_id"] != hint:
        raise ActivityError(
            "conflict",
            "The activity changed during this update. Try again.",
            status=409,
        )
    if journey is not None and loaded["journey_id"] == journey["id"]:
        journey["participations"] = journey.get("participations") or []
    return loaded, journey


def _credits_can_change(loaded: dict, journey: dict | None, changes: dict) -> bool:
    if journey is None:
        return False
    if loaded["journey_id"] is not None:
        return loaded["journey_id"] == journey["id"]
    return "elevation_m" in changes or "distance_m" in changes


def _owner_row(database, user_id: int, activity_id: int):
    row = database.execute(
        "SELECT id, user_id FROM activities WHERE id = ?",
        (activity_id,),
    ).fetchone()
    if row is None:
        return None
    if row["user_id"] != user_id:
        raise ActivityError(
            "forbidden",
            "You cannot change another person's activity.",
            status=403,
        )
    return row


def _assignment_journey_id(database, activity_id: int) -> int | None:
    row = database.execute(
        """
        SELECT journey_id
        FROM activity_journey_assignments
        WHERE activity_id = ?
        """,
        (activity_id,),
    ).fetchone()
    if row is None:
        return None
    return row["journey_id"]


def _load_owned(database, user_id: int, activity_id: int) -> dict:
    row = database.execute(
        f"""
        SELECT {_ACTIVITY_COLUMNS}
        FROM activities AS activity
        WHERE activity.id = ?
        """,
        (activity_id,),
    ).fetchone()
    if row is None:
        raise ActivityError("not_found", "That activity does not exist.", status=404)
    if row["user_id"] != user_id:
        raise ActivityError(
            "forbidden",
            "You cannot change another person's activity.",
            status=403,
        )
    assignment = database.execute(
        """
        SELECT id, journey_id
        FROM activity_journey_assignments
        WHERE activity_id = ?
        """,
        (activity_id,),
    ).fetchone()
    credits: dict[str, Decimal] = {}
    journey_id = None
    assignment_id = None
    if assignment is not None:
        assignment_id = assignment["id"]
        journey_id = assignment["journey_id"]
        for credit in database.execute(
            """
            SELECT metric_code, credited_amount
            FROM journey_credits
            WHERE assignment_id = ?
            ORDER BY metric_code
            """,
            (assignment_id,),
        ).fetchall():
            credits[credit["metric_code"]] = credit["credited_amount"]
    return {
        "id": row["id"],
        "elevation_m": row["elevation_gain_m"],
        "distance_m": row["distance_m"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "activity_type": row["activity_type"],
        "assignment_id": assignment_id,
        "journey_id": journey_id,
        "credits": credits,
    }


def _preview_new(database, user_id: int, changes: dict) -> dict:
    found = database.execute(
        """
        SELECT id, user_id, route_id, status, progress_version
        FROM route_journeys
        WHERE user_id = ? AND status = 'active'
        """,
        (user_id,),
    ).fetchone()
    measurements = _measurements_from_changes(changes)
    if found is None:
        return _empty_preview(measurements)
    journey = _journey_state(database, found)
    return _preview_from_proposal(
        journey,
        _plan(
            database,
            journey,
            operation="create",
            activity_id=None,
            assignment_id=None,
            activity_type=changes["activity_type"],
            measurements=measurements,
            previous_credits={},
            allocate_now=_positive_required(journey, measurements),
        ),
    )


def _preview_edit(database, user_id: int, loaded: dict, changes: dict) -> dict:
    measurements = _merged_measurements(loaded, changes)
    journey = _preview_journey(database, user_id, loaded, changes)
    if journey is None:
        return _empty_preview(measurements)
    if journey["status"] == "completed":
        _reject_completed_progress_edit(loaded, changes)
    allocate_now = (
        journey["status"] != "completed"
        and _measurement_inputs_changed(loaded, changes)
    )
    if loaded["journey_id"] is None and not allocate_now:
        return _empty_preview(measurements)
    proposal = _plan(
        database,
        journey,
        operation="update",
        activity_id=loaded["id"],
        assignment_id=loaded["assignment_id"],
        activity_type=changes.get("activity_type", loaded["activity_type"]),
        measurements=measurements,
        previous_credits=loaded["credits"],
        allocate_now=allocate_now,
    )
    if journey["status"] == "completed":
        proposal["will_complete"] = False
        proposal["resume_required"] = False
    return _preview_from_proposal(journey, proposal)


def _preview_journey(database, user_id: int, loaded: dict, changes: dict) -> dict | None:
    journey_id = loaded["journey_id"]
    if journey_id is None and ("elevation_m" in changes or "distance_m" in changes):
        found = database.execute(
            """
            SELECT id
            FROM route_journeys
            WHERE user_id = ? AND status = 'active'
            """,
            (user_id,),
        ).fetchone()
        if found is None:
            return None
        journey_id = found["id"]
    if journey_id is None:
        return None
    locked = database.execute(
        """
        SELECT id, user_id, route_id, status, progress_version
        FROM route_journeys
        WHERE id = ?
        """,
        (journey_id,),
    ).fetchone()
    return _journey_state(database, locked)


def _plan(
    database,
    journey: dict,
    *,
    operation: str,
    activity_id: int | None,
    assignment_id: int | None,
    activity_type: str,
    measurements: dict[str, Decimal | None],
    previous_credits: dict[str, Decimal],
    allocate_now: bool,
) -> dict:
    """One walk over the assignments this save would leave on the journey."""
    requirements = _requirement_pairs(journey)
    slots = _load_slots(database, journey["id"])
    if allocate_now:
        include = assignment_id is not None or _positive_required(journey, measurements)
        if include:
            slots = _replace_slot(
                slots,
                activity_id,
                activity_type,
                measurements,
            )
        shares = allocate(slots, requirements)
        resulting = totals_for(shares, requirements)
        allocation_changed = _allocation_differs(
            database, journey["id"], slots, shares, requirements
        )
    else:
        shares = {activity_id: dict(previous_credits)} if activity_id is not None else {}
        resulting = {
            code: journey["totals"].get(code, Decimal("0")) for code, _target in requirements
        }
        allocation_changed = False
    credits = {
        metric: amount
        for metric, amount in shares.get(activity_id, {}).items()
        if amount > 0
    }
    metrics = []
    for code, target in requirements:
        stored = journey["totals"].get(code, Decimal("0"))
        prior = previous_credits.get(code, Decimal("0"))
        current = stored - prior
        if current < 0:
            current = Decimal("0")
        credit = credits.get(code, Decimal("0"))
        amount = resulting.get(code, Decimal("0"))
        metrics.append(
            {
                "metric": code,
                "current_m": current,
                "existing_credit_m": prior,
                "target_m": target,
                "remaining_m": target - current if target > current else Decimal("0"),
                "measurement_m": measurements.get(code),
                "credit_m": credit,
                "resulting_m": amount,
                "complete": amount >= target,
            }
        )
    finished_before = _targets_met(journey["requirements"], journey["totals"])
    finished_after = _targets_met(requirements, resulting)
    status = journey["status"]
    return {
        "operation": operation,
        "activity_token": "-" if activity_id is None else str(activity_id),
        "measurements_token": measurement_token(
            measurements.get("elevation"), measurements.get("distance")
        ),
        "fingerprint": allocation_fingerprint(slots, shares),
        "slots": slots,
        "shares": shares,
        "metrics": metrics,
        "credits": credits,
        "resulting": resulting,
        "measurements": measurements,
        "credit_changed": credits != {
            code: amount for code, amount in previous_credits.items() if amount > 0
        },
        "allocation_changed": allocation_changed,
        "will_complete": status == "active" and finished_after and not finished_before,
        "resume_required": status in ("paused", "abandoned") and finished_after and not finished_before,
    }


def _requirement_pairs(journey: dict) -> list[tuple[str, Decimal]]:
    return [
        (item["metric_code"], item["target_amount"]) for item in journey["requirements"]
    ]


def _targets_met(requirements, totals: dict) -> bool:
    if requirements and isinstance(requirements[0], tuple):
        pairs = requirements
    else:
        pairs = [
            (item["metric_code"], item["target_amount"]) for item in requirements
        ]
    return bool(pairs) and all(
        totals.get(code, Decimal("0")) >= target for code, target in pairs
    )


def _totals_map(journey: dict) -> dict[str, Decimal]:
    return {
        item["metric_code"]: journey["totals"].get(item["metric_code"], Decimal("0"))
        for item in journey["requirements"]
    }


def _load_slots(database, journey_id: int) -> list[MeasuredAssignment]:
    rows = database.execute(
        """
        SELECT assignment.id AS assignment_id,
               assignment.created_at,
               activity.id AS activity_id,
               activity.activity_type,
               activity.elevation_gain_m,
               activity.distance_m
        FROM activity_journey_assignments AS assignment
        JOIN activities AS activity ON activity.id = assignment.activity_id
        WHERE assignment.journey_id = ?
        ORDER BY assignment.created_at, activity.id
        """,
        (journey_id,),
    ).fetchall()
    return [
        MeasuredAssignment(
            activity_id=row["activity_id"],
            assignment_id=row["assignment_id"],
            created_at=row["created_at"],
            sort_last=False,
            activity_type=row["activity_type"],
            elevation_m=row["elevation_gain_m"],
            distance_m=row["distance_m"],
        )
        for row in rows
    ]


def _replace_slot(
    slots: list[MeasuredAssignment],
    activity_id: int | None,
    activity_type: str,
    measurements: dict[str, Decimal | None],
) -> list[MeasuredAssignment]:
    updated: list[MeasuredAssignment] = []
    found = False
    for slot in slots:
        if activity_id is not None and slot.activity_id == activity_id:
            updated.append(
                MeasuredAssignment(
                    activity_id=activity_id,
                    assignment_id=slot.assignment_id,
                    created_at=slot.created_at,
                    sort_last=False,
                    activity_type=activity_type,
                    elevation_m=measurements.get("elevation"),
                    distance_m=measurements.get("distance"),
                )
            )
            found = True
        else:
            updated.append(slot)
    if not found:
        updated.append(
            MeasuredAssignment(
                activity_id=activity_id,
                assignment_id=None,
                created_at=datetime.min,
                sort_last=True,
                activity_type=activity_type,
                elevation_m=measurements.get("elevation"),
                distance_m=measurements.get("distance"),
            )
        )
    return updated


def _allocation_differs(database, journey_id: int, slots, shares, requirements) -> bool:
    stored: dict[int, dict[str, Decimal]] = {}
    for row in database.execute(
        """
        SELECT assignment_id, metric_code, credited_amount
        FROM journey_credits
        WHERE journey_id = ?
        """,
        (journey_id,),
    ).fetchall():
        stored.setdefault(row["assignment_id"], {})[row["metric_code"]] = row[
            "credited_amount"
        ]
    known: set[int] = set()
    for slot in slots:
        expected = shares.get(slot.activity_id, {})
        if slot.assignment_id is None:
            if any(amount > 0 for amount in expected.values()):
                return True
            continue
        known.add(slot.assignment_id)
        for metric, _target in requirements:
            current = stored.get(slot.assignment_id, {}).get(metric, Decimal("0"))
            amount = expected.get(metric, Decimal("0"))
            if current != amount:
                return True
    return bool(set(stored) - known)


def _bind_new_assignment(slots, assignment_id: int) -> None:
    for slot in slots:
        if slot.assignment_id is None:
            slot.assignment_id = assignment_id
            return
    raise RuntimeError("the new assignment was not in the allocation")


def _bind_existing_assignment(slots, activity_id: int, assignment_id: int) -> None:
    for slot in slots:
        if slot.activity_id == activity_id:
            slot.assignment_id = assignment_id
            return


def _sync_shares(database, journey: dict, slots, shares, phase: str) -> None:
    for metric, _target in _requirement_pairs(journey):
        desired: dict[int, Decimal] = {}
        for slot in slots:
            if slot.assignment_id is None:
                continue
            amount = shares.get(slot.activity_id, {}).get(metric, Decimal("0"))
            if amount > 0:
                desired[slot.assignment_id] = amount
        existing = {
            row["assignment_id"]: row["credited_amount"]
            for row in database.execute(
                """
                SELECT assignment_id, credited_amount
                FROM journey_credits
                WHERE journey_id = ? AND metric_code = ?
                """,
                (journey["id"], metric),
            ).fetchall()
        }
        if phase == "reduce":
            for assignment_id, current in sorted(existing.items()):
                updated = desired.get(assignment_id, Decimal("0"))
                if updated >= current:
                    continue
                if updated <= 0:
                    database.execute(
                        """
                        DELETE FROM journey_credits
                        WHERE assignment_id = ? AND metric_code = ?
                        """,
                        (assignment_id, metric),
                    )
                else:
                    database.execute(
                        """
                        UPDATE journey_credits
                        SET credited_amount = ?
                        WHERE assignment_id = ? AND metric_code = ?
                        """,
                        (updated, assignment_id, metric),
                    )
            continue
        for assignment_id, updated in sorted(desired.items()):
            current = existing.get(assignment_id, Decimal("0"))
            if updated <= current:
                continue
            if current <= 0:
                _insert_credit(database, assignment_id, journey, metric, updated)
            else:
                database.execute(
                    """
                    UPDATE journey_credits
                    SET credited_amount = ?
                    WHERE assignment_id = ? AND metric_code = ?
                    """,
                    (updated, assignment_id, metric),
                )


def _portion(measurement: Decimal | None, target: Decimal, base: Decimal) -> Decimal:
    if measurement is None or measurement <= 0:
        return Decimal("0")
    remaining = target - base
    if remaining <= 0:
        return Decimal("0")
    if measurement < remaining:
        return measurement
    return remaining


def _preview_from_proposal(journey: dict, proposal: dict) -> dict:
    token = None
    expires = None
    if proposal["will_complete"] and not proposal["resume_required"]:
        expires = _now() + TOKEN_TTL_SECONDS
        token = _issue_confirmation_token(
            journey["user_id"],
            journey["id"],
            proposal["operation"],
            proposal["activity_token"],
            journey["progress_version"],
            proposal["measurements_token"],
            proposal["fingerprint"],
            expires,
        )
    measurements = proposal["measurements"]
    return {
        "journey": _journey_public(journey),
        "elevation_m": measurements.get("elevation"),
        "distance_m": measurements.get("distance"),
        "metrics": proposal["metrics"],
        "will_complete": proposal["will_complete"],
        "credits_journey": bool(proposal["credits"]),
        "saved_without_credit": not proposal["credits"],
        "no_credit_reason": _no_credit_reason(
            journey, measurements, proposal["credits"]
        ),
        "resume_required": proposal["resume_required"],
        "confirmation_token": token,
        "confirmation_expires_at": expires,
    }


def _empty_preview(measurements: dict[str, Decimal | None]) -> dict:
    return {
        "journey": None,
        "elevation_m": measurements.get("elevation"),
        "distance_m": measurements.get("distance"),
        "metrics": [],
        "will_complete": False,
        "credits_journey": False,
        "saved_without_credit": True,
        "no_credit_reason": "no_active_journey",
        "resume_required": False,
        "confirmation_token": None,
        "confirmation_expires_at": None,
    }


def _journey_public(journey: dict) -> dict:
    metrics = []
    for requirement in journey["requirements"]:
        code = requirement["metric_code"]
        target = requirement["target_amount"]
        credited = journey["totals"].get(code, Decimal("0"))
        if credited > target:
            credited = target
        metrics.append(
            {
                "metric": code,
                "target_m": target,
                "credited_m": credited,
                "complete": credited >= target,
            }
        )
    return {
        "id": journey["id"],
        "status": journey["status"],
        "progress_version": journey["progress_version"],
        "route_id": journey["route_id"],
        "route_slug": journey["route_slug"],
        "metrics": metrics,
    }


def _positive_required(journey: dict, measurements: dict[str, Decimal | None]) -> bool:
    required = {item["metric_code"] for item in journey["requirements"]}
    for code in required:
        amount = measurements.get(code)
        if amount is not None and amount > 0:
            return True
    return False


def _no_credit_reason(journey: dict | None, measurements: dict, credits: dict) -> str | None:
    if credits:
        return None
    if journey is None:
        return "no_active_journey"
    if not _positive_required(journey, measurements):
        return "no_positive_measurement"
    return "metric_already_complete"


def _measurement_inputs_changed(loaded: dict, changes: dict) -> bool:
    for key in ("elevation_m", "distance_m"):
        if key in changes and changes[key] != loaded[key]:
            return True
    return False


def _validate_distance(activity_type: str, distance_m: Decimal | None) -> None:
    if distance_m is None:
        return
    cycling = activity_type in CYCLING_TYPES
    limit = MAX_CYCLE_DISTANCE_M if cycling else MAX_DISTANCE_M
    label = "200 mi" if cycling else "75 mi"
    if distance_m > limit:
        raise ActivityError(
            "invalid",
            f"Log one outing at a time, up to {label}.",
            status=400,
        )


def _retain_assignment(
    database,
    activity_id: int,
    user_id: int,
    journey: dict,
    loaded: dict,
    proposal: dict,
) -> bool:
    """Keep an assignment that still has a positive required measurement."""
    if proposal["credits"]:
        _increase_credits(
            database, activity_id, user_id, journey, loaded, proposal["credits"]
        )
        return True
    if _positive_required(journey, proposal["measurements"]):
        if loaded["assignment_id"] is None:
            loaded["assignment_id"] = _insert_assignment(
                database, activity_id, user_id, journey
            )
        return True
    return loaded["assignment_id"] is not None


def _reject_completed_progress_edit(loaded: dict, changes: dict) -> None:
    if loaded["assignment_id"] is not None:
        for key in ("elevation_m", "distance_m"):
            if key in changes and changes[key] != loaded[key]:
                raise ActivityError(
                    "invalid_state",
                    "Progress on a completed journey cannot change.",
                    status=409,
                )
        return
    for metric, key in (("elevation", "elevation_m"), ("distance", "distance_m")):
        if key not in changes or metric not in loaded["credits"]:
            continue
        if changes[key] != loaded[key]:
            raise ActivityError(
                "invalid_state",
                "Progress on a completed journey cannot change.",
                status=409,
            )


def _insert_activity(database, user_id: int, changes: dict) -> int:
    _check_times(changes.get("started_at"), changes.get("ended_at"))
    row = database.execute(
        """
        INSERT INTO activities (
            user_id, activity_type, activity_date,
            started_at, ended_at, duration_s,
            distance_m, elevation_gain_m,
            source, external_id, source_metadata,
            verification_status, note
        )
        VALUES (
            ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            'manual', NULL, '{}'::jsonb,
            'not_required', ?
        )
        RETURNING id
        """,
        (
            user_id,
            changes.get("activity_type", "unspecified"),
            changes.get("happened_on", date.today()),
            changes.get("started_at"),
            changes.get("ended_at"),
            changes.get("duration_s"),
            changes.get("distance_m"),
            changes.get("elevation_m"),
            changes.get("note", ""),
        ),
    ).fetchone()
    database.execute(
        """
        INSERT INTO activity_verification_events (
            activity_id, from_status, to_status, reason, actor_type
        )
        VALUES (?, NULL, 'not_required', ?, 'system')
        """,
        (row["id"], MANUAL_EVENT_REASON),
    )
    return row["id"]


def _insert_assignment_credits(database, activity_id: int, user_id: int, journey: dict, credits: dict[str, Decimal]) -> None:
    assignment_id = _insert_assignment(database, activity_id, user_id, journey)
    for code in sorted(credits):
        _insert_credit(database, assignment_id, journey, code, credits[code])


def _insert_assignment(database, activity_id: int, user_id: int, journey: dict) -> int:
    row = database.execute(
        """
        INSERT INTO activity_journey_assignments (
            activity_id, journey_id, user_id, route_id
        )
        VALUES (?, ?, ?, ?)
        RETURNING id
        """,
        (activity_id, journey["id"], user_id, journey["route_id"]),
    ).fetchone()
    return row["id"]


def _insert_credit(database, assignment_id: int, journey: dict, metric: str, amount: Decimal) -> None:
    database.execute(
        """
        INSERT INTO journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (assignment_id, journey["id"], journey["route_id"], metric, amount),
    )


def _reduce_credits(database, loaded: dict, new_credits: dict[str, Decimal]) -> None:
    if loaded["assignment_id"] is None:
        return
    for code in sorted(loaded["credits"]):
        updated = new_credits.get(code, Decimal("0"))
        current = loaded["credits"][code]
        if updated >= current:
            continue
        if updated <= 0:
            database.execute(
                """
                DELETE FROM journey_credits
                WHERE assignment_id = ? AND metric_code = ?
                """,
                (loaded["assignment_id"], code),
            )
        else:
            database.execute(
                """
                UPDATE journey_credits
                SET credited_amount = ?
                WHERE assignment_id = ? AND metric_code = ?
                """,
                (updated, loaded["assignment_id"], code),
            )


def _increase_credits(
    database,
    activity_id: int,
    user_id: int,
    journey: dict,
    loaded: dict,
    new_credits: dict[str, Decimal],
) -> None:
    if not new_credits:
        return
    assignment_id = loaded["assignment_id"]
    if assignment_id is None:
        assignment_id = _insert_assignment(database, activity_id, user_id, journey)
        loaded["assignment_id"] = assignment_id
    for code in sorted(new_credits):
        current = loaded["credits"].get(code, Decimal("0"))
        updated = new_credits[code]
        if updated <= current:
            continue
        if current <= 0:
            _insert_credit(database, assignment_id, journey, code, updated)
        else:
            database.execute(
                """
                UPDATE journey_credits
                SET credited_amount = ?
                WHERE assignment_id = ? AND metric_code = ?
                """,
                (updated, assignment_id, code),
            )


def _delete_assignment(database, assignment_id: int) -> None:
    database.execute(
        "DELETE FROM activity_journey_assignments WHERE id = ?",
        (assignment_id,),
    )


def _touch_journey(
    database,
    journey_id: int,
    *,
    credit_changed: bool,
    complete: bool,
    inputs_changed: bool = False,
) -> None:
    if complete:
        cursor = database.execute(
            """
            UPDATE route_journeys
            SET progress_version = progress_version + 1,
                status = 'completed',
                completed_at = now()
            WHERE id = ? AND status = 'active'
            """,
            (journey_id,),
        )
    elif credit_changed or inputs_changed:
        cursor = database.execute(
            """
            UPDATE route_journeys
            SET progress_version = progress_version + 1
            WHERE id = ?
            """,
            (journey_id,),
        )
    else:
        return
    if cursor.rowcount != 1:
        raise ActivityError(
            "conflict",
            "The journey changed during this update. Try again.",
            status=409,
        )


def _with_arrivals(saved: dict, arrivals: list[dict]) -> dict:
    saved["arrivals"] = arrivals
    return saved


def _write_create_feed(
    database,
    user_id: int,
    activity_id: int,
    journey: dict,
    proposal: dict,
    reached: list[CheckpointMark],
) -> None:
    participations = journey.get("participations") or []
    if not participations or not proposal["credits"]:
        return
    after = proposal["resulting"]
    for participation in participations:
        _feed_row(
            database,
            participation,
            user_id,
            "activity_logged",
            activity_id,
            None,
            proposal["credits"],
        )
        for checkpoint in reached:
            _feed_row(
                database,
                participation,
                user_id,
                "checkpoint_reached",
                activity_id,
                checkpoint.name,
                _positive_amounts(after),
            )
        if proposal["will_complete"]:
            _feed_row(
                database,
                participation,
                user_id,
                "summit_reached",
                activity_id,
                None,
                _positive_amounts(after),
            )


def _write_summit_feed(database, user_id: int, activity_id: int, journey: dict, resulting: dict[str, Decimal]) -> None:
    """An edit that finishes the journey records the summit and nothing else."""
    for participation in journey.get("participations") or []:
        _feed_row(
            database,
            participation,
            user_id,
            "summit_reached",
            activity_id,
            None,
            _positive_amounts(resulting),
        )


def _feed_row(
    database,
    participation: dict,
    user_id: int,
    kind: str,
    activity_id: int,
    checkpoint_name: str | None,
    amounts: dict[str, Decimal],
) -> None:
    positive = _positive_amounts(amounts)
    if not positive:
        return
    row = database.execute(
        """
        INSERT INTO expedition_updates (
            expedition_id, user_id, participation_id, kind,
            activity_id, checkpoint_name
        )
        VALUES (?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            participation["expedition_id"],
            user_id,
            participation["id"],
            kind,
            activity_id,
            checkpoint_name,
        ),
    ).fetchone()
    for code in sorted(positive):
        database.execute(
            """
            INSERT INTO expedition_update_metrics (
                update_id, metric_code, credited_amount
            )
            VALUES (?, ?, ?)
            """,
            (row["id"], code, positive[code]),
        )


def _bases(journey: dict, previous: dict[str, Decimal]) -> dict[str, Decimal]:
    bases = {}
    for requirement in journey["requirements"]:
        code = requirement["metric_code"]
        base = journey["totals"].get(code, Decimal("0")) - previous.get(code, Decimal("0"))
        bases[code] = base if base > 0 else Decimal("0")
    return bases


def _newly_reached(database, route_id: int, before: dict[str, Decimal], after: dict[str, Decimal]) -> list[CheckpointMark]:
    """Checkpoints the locked totals missed and this proposal reaches.

    ``before`` is the snapshot taken under the journey lock, before this
    mutation writes credits. On an edit that snapshot already includes the
    activity's current credit, so a checkpoint it had already opened is not
    attributed to the edit.
    """
    marks = _checkpoint_marks(database, route_id)
    previous = {mark.id for mark in assess_checkpoints(marks, before) if mark.reached}
    return [
        mark
        for mark in assess_checkpoints(marks, after)
        if mark.reached and mark.id not in previous
    ]


def _checkpoint_marks(database, route_id: int) -> tuple[CheckpointMark, ...]:
    rows = database.execute(
        """
        SELECT id, slug, name, position
        FROM route_checkpoints
        WHERE route_id = ?
        ORDER BY position, id
        """,
        (route_id,),
    ).fetchall()
    thresholds: dict[int, list[tuple[str, Decimal]]] = {row["id"]: [] for row in rows}
    for row in database.execute(
        """
        SELECT checkpoint_id, metric_code, threshold_amount
        FROM checkpoint_requirements
        WHERE route_id = ?
        ORDER BY checkpoint_id, metric_code
        """,
        (route_id,),
    ).fetchall():
        thresholds[row["checkpoint_id"]].append((row["metric_code"], row["threshold_amount"]))
    return tuple(
        CheckpointMark(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            position=row["position"],
            reached=False,
            thresholds=tuple(thresholds[row["id"]]),
        )
        for row in rows
    )


def _positive_amounts(amounts: dict[str, Decimal]) -> dict[str, Decimal]:
    return {code: amount for code, amount in amounts.items() if amount > 0}


def _apply_changes(database, activity_id: int, loaded: dict, changes: dict) -> None:
    started = loaded["started_at"]
    ended = loaded["ended_at"]
    if "started_at" in changes:
        started = changes["started_at"]
    if "ended_at" in changes:
        ended = changes["ended_at"]
    if "started_at" in changes or "ended_at" in changes:
        _check_times(started, ended)
    assignments = []
    params: list = []
    mapping = (
        ("activity_type", "activity_type"),
        ("happened_on", "activity_date"),
        ("note", "note"),
        ("distance_m", "distance_m"),
        ("elevation_m", "elevation_gain_m"),
        ("duration_s", "duration_s"),
        ("started_at", "started_at"),
        ("ended_at", "ended_at"),
    )
    for key, column in mapping:
        if key in changes:
            assignments.append(f"{column} = ?")
            params.append(changes[key])
    if not assignments:
        return
    params.append(activity_id)
    database.execute(
        f"UPDATE activities SET {', '.join(assignments)} WHERE id = ?",
        tuple(params),
    )


def _saved(database, activity_id: int, journey_id: int | None) -> dict:
    rows = _history_rows(database, activity_id)
    activity = _group_history(rows)[0]
    credits = [] if activity["credit"] is None else activity["credit"]["metrics"]
    journey = None if journey_id is None else _saved_journey(database, journey_id)
    return {"activity": activity, "credits": credits, "journey": journey}


def _saved_journey(database, journey_id: int) -> dict:
    progress = read_journey_progress(database, journey_id)
    slug = database.execute(
        "SELECT slug FROM routes WHERE id = ?",
        (progress.route_id,),
    ).fetchone()["slug"]
    final = progress.final_checkpoint_reached
    return {
        "id": progress.journey_id,
        "status": progress.status,
        "progress_version": progress.progress_version,
        "route_id": progress.route_id,
        "route_slug": slug,
        "requirements_met": progress.requirements_met,
        "final_checkpoint_reached": final,
        "highest_checkpoint": None if progress.highest_checkpoint is None else progress.highest_checkpoint.slug,
        "metrics": [
            {
                "metric": metric.code,
                "target_m": metric.target_m,
                "credited_m": metric.credited_m,
                "remaining_m": metric.remaining_m,
                "complete": metric.complete,
            }
            for metric in progress.metrics
        ],
    }


_ACTIVITY_COLUMNS = """
    activity.id, activity.user_id, activity.activity_type, activity.activity_date,
    activity.started_at, activity.ended_at, activity.duration_s, activity.distance_m,
    activity.elevation_gain_m, activity.note, activity.source,
    activity.verification_status, activity.created_at, activity.updated_at
"""


def _history_rows(database, activity_id: int) -> list[dict]:
    return database.execute(
        f"""
        SELECT {_ACTIVITY_COLUMNS},
               assignment.journey_id,
               journey.status AS journey_status,
               journey.route_id AS credit_route_id,
               route.slug AS route_slug,
               credit.metric_code,
               credit.credited_amount
        FROM activities AS activity
        LEFT JOIN activity_journey_assignments AS assignment
            ON assignment.activity_id = activity.id
        LEFT JOIN journey_credits AS credit
            ON credit.assignment_id = assignment.id
        LEFT JOIN route_journeys AS journey ON journey.id = assignment.journey_id
        LEFT JOIN routes AS route ON route.id = journey.route_id
        WHERE activity.id = ?
        ORDER BY credit.metric_code
        """,
        (activity_id,),
    ).fetchall()


def _group_history(rows: list[dict]) -> list[dict]:
    grouped: list[dict] = []
    index: dict[int, dict] = {}
    for row in rows:
        item = index.get(row["id"])
        if item is None:
            item = _activity_dict(row)
            item["credit"] = None
            item["association"] = None
            if row["journey_id"] is not None:
                item["association"] = {
                    "journey_id": row["journey_id"],
                    "journey_status": row["journey_status"],
                    "route_id": row["credit_route_id"],
                    "route_slug": row["route_slug"],
                }
            index[row["id"]] = item
            grouped.append(item)
        if row["metric_code"] is None:
            continue
        if item["credit"] is None:
            item["credit"] = {
                "journey_id": row["journey_id"],
                "journey_status": row["journey_status"],
                "route_id": row["credit_route_id"],
                "route_slug": row["route_slug"],
                "metrics": [],
            }
        item["credit"]["metrics"].append(
            {"metric": row["metric_code"], "credited_m": row["credited_amount"]}
        )
    return grouped


def _activity_dict(row: dict) -> dict:
    record = {
        "id": row["id"],
        "user_id": row["user_id"],
        "activity_type": row["activity_type"],
        "activity_date": row["activity_date"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_s": row["duration_s"],
        "distance_m": row["distance_m"],
        "elevation_gain_m": row["elevation_gain_m"],
        "note": row["note"],
        "source": row["source"],
        "verification_status": row["verification_status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    return record


def _measurements_from_changes(changes: dict) -> dict[str, Decimal | None]:
    return {
        "elevation": changes.get("elevation_m"),
        "distance": changes.get("distance_m"),
    }


def _merged_measurements(loaded: dict, changes: dict) -> dict[str, Decimal | None]:
    return {
        "elevation": changes["elevation_m"] if "elevation_m" in changes else loaded["elevation_m"],
        "distance": changes["distance_m"] if "distance_m" in changes else loaded["distance_m"],
    }


def _changes(fields: dict, *, creating: bool) -> dict:
    if not isinstance(fields, dict):
        raise ActivityError("invalid", "Activity details have to be an object.", status=400)
    unknown = set(fields) - _FIELDS
    if unknown:
        raise ActivityError("invalid", "That activity field is not supported.", status=400)
    if creating and "elevation_ft" not in fields and "distance_mi" not in fields:
        raise ActivityError("invalid", "Add elevation or distance.", status=400)
    if not creating and not fields:
        raise ActivityError("invalid", "Choose something to change.", status=400)
    changes: dict = {}
    if "elevation_ft" in fields:
        changes["elevation_m"] = _measurement("elevation", fields["elevation_ft"])
    if "distance_mi" in fields:
        changes["distance_m"] = _measurement("distance", fields["distance_mi"])
    if "happened_on" in fields:
        changes["happened_on"] = _date(fields["happened_on"])
    elif creating:
        changes["happened_on"] = date.today()
    if "note" in fields:
        changes["note"] = _note(fields["note"])
    elif creating:
        changes["note"] = ""
    if "activity_type" in fields:
        changes["activity_type"] = _activity_type(fields["activity_type"])
    elif creating:
        changes["activity_type"] = "unspecified"
    if "duration_s" in fields:
        changes["duration_s"] = _duration(fields["duration_s"])
    if "started_at" in fields:
        changes["started_at"] = _timestamp(fields["started_at"], "start time")
    if "ended_at" in fields:
        changes["ended_at"] = _timestamp(fields["ended_at"], "end time")
    if creating:
        _check_times(changes.get("started_at"), changes.get("ended_at"))
    return changes


def _measurement(metric: str, value) -> Decimal | None:
    try:
        meters = parse_measurement(metric, value)
    except MetricError as exc:
        raise ActivityError("invalid", str(exc), status=400) from exc
    if meters is None:
        return None
    if metric == "elevation" and meters > MAX_ELEVATION_M:
        raise ActivityError(
            "invalid",
            "Log one outing at a time, up to 10,000 ft.",
            status=400,
        )
    return meters


def _date(value) -> date:
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ActivityError("invalid", "Use a real date.", status=400) from exc
    else:
        raise ActivityError("invalid", "Use a real date.", status=400)
    if parsed > date.today():
        raise ActivityError("invalid", "That date is still in the future.", status=400)
    if parsed.year < 2000:
        raise ActivityError("invalid", "Start at the year 2000 or later.", status=400)
    return parsed


def _note(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ActivityError("invalid", "The note should be text.", status=400)
    note = " ".join(value.split())
    if len(note) > MAX_NOTE:
        raise ActivityError("invalid", "Keep the note under 280 characters.", status=400)
    return note


def _activity_type(value) -> str:
    if not isinstance(value, str) or value not in ACTIVITY_TYPES:
        raise ActivityError("invalid", "Choose a supported activity type.", status=400)
    return value


def _duration(value) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ActivityError("invalid", "Duration should be a whole number of seconds.", status=400)
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ActivityError(
                "invalid",
                "Duration should be a whole number of seconds.",
                status=400,
            )
        value = int(text)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > 2_147_483_647:
        raise ActivityError(
            "invalid",
            "Duration should be a whole number of seconds.",
            status=400,
        )
    return value


def _timestamp(value, label: str) -> datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ActivityError("invalid", f"Use a real {label}.", status=400) from exc
    raise ActivityError("invalid", f"Use a real {label}.", status=400)


def _is_aware(value: datetime) -> bool:
    tzinfo = value.tzinfo
    return tzinfo is not None and tzinfo.utcoffset(value) is not None


def _check_times(started, ended) -> None:
    if (started is None) != (ended is None):
        raise ActivityError(
            "invalid",
            "Start and end times have to be set together.",
            status=400,
        )
    if started is not None and _is_aware(started) != _is_aware(ended):
        raise ActivityError(
            "invalid",
            "Start and end times have to be both timezone-aware or both timezone-naive.",
            status=400,
        )
    if started is not None and ended < started:
        raise ActivityError("invalid", "The end time cannot be before the start.", status=400)


def _now() -> int:
    return int(time.time())


def _canonical_meters(value: Decimal) -> str:
    return format(Decimal(value).quantize(STORAGE_QUANTUM), "f")


def _canonical_credits(credits: dict[str, Decimal]) -> str:
    return ",".join(
        f"{code}={_canonical_meters(credits[code])}"
        for code in sorted(credits)
        if credits[code] > 0
    )


def _issue_confirmation_token(
    user_id: int,
    journey_id: int,
    operation: str,
    activity_token: str,
    progress_version: int,
    measurements: str,
    fingerprint: str,
    exp_unix: int,
) -> str:
    payload = (
        f"v3|{user_id}|{journey_id}|{operation}|{activity_token}|"
        f"{progress_version}|{measurements}|{fingerprint}|{exp_unix}"
    )
    signature = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{signature}"


def _token_matches(token, user_id: int, journey: dict, proposal: dict) -> bool:
    if not isinstance(token, str):
        return False
    payload, separator, signature = token.rpartition("|")
    if not separator or payload.count("|") != 8:
        return False
    expected = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if len(signature) != len(expected) or not hmac.compare_digest(expected, signature):
        return False
    (
        version,
        token_user,
        token_journey,
        token_operation,
        token_activity,
        token_progress,
        token_measurements,
        token_allocation,
        token_exp,
    ) = payload.split("|")
    if version != "v3":
        return False
    if token_exp.startswith("-") or not token_exp.isdigit():
        return False
    if int(token_exp) <= _now():
        return False
    return (
        token_user == str(user_id)
        and token_journey == str(journey["id"])
        and token_operation == proposal["operation"]
        and token_activity == proposal["activity_token"]
        and token_progress == str(journey["progress_version"])
        and token_measurements == proposal["measurements_token"]
        and token_allocation == proposal["fingerprint"]
    )


def _secret() -> bytes:
    secret = current_app.config.get("SECRET_KEY")
    if isinstance(secret, str) and secret:
        return secret.encode("utf-8")
    if isinstance(secret, bytes) and secret:
        return secret
    raise RuntimeError("SECRET_KEY is not configured.")
