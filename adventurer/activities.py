"""Personal activities and the one journey credit each activity may earn.

An activity stores the full elevation the person recorded. A journey credit
is the part of that elevation that advances exactly one route journey. This
module does not serve HTTP, choose photos, or manage expedition membership.

The future HTTP layer should call ``preview_activity`` before creating an
activity and ``preview_activity_update`` before changing an existing one.
When either preview says the active journey would be completed, pass that
preview's confirmation token back into the matching save. The save locks
the current rows and checks the token against the credit it just calculated.
It does not trust the numbers from the preview.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask import current_app

from .db import get_db
from .measures import (
    ELEVATION_QUANTUM,
    feet_to_meters,
    kilometers_to_meters,
)

MANUAL_EVENT_REASON = "Manual entry. Manual entries are not independently verified."
TOKEN_TTL_SECONDS = 600
MAX_ELEVATION_FT = Decimal("10000")
MAX_DISTANCE_KM = Decimal("120")
MAX_NOTE = 280
ACTIVITY_TYPES = frozenset(
    {"run", "hike", "walk", "stair_climb", "other", "unspecified"}
)
_FIELDS = frozenset(
    {
        "elevation_ft",
        "distance_km",
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
    "elevation credited to a completed journey cannot change",
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
        "credited elevation cannot exceed the activity elevation" in message
        or "activity elevation cannot fall below the credited amount" in message
    ):
        return ActivityError(
            "invalid",
            "The credited elevation is not valid for this activity.",
            status=400,
        )
    return None


def preview_activity(user_id: int, fields: dict) -> dict:
    """Read-only preview of a new activity and its potential journey credit."""
    changes = _changes(fields, creating=True)
    database = get_db()
    try:
        _require_user(database, user_id, lock=False)
        return _preview_new(database, user_id, changes["elevation_m"])
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
    """Store one manual activity and, when a journey is active, its credit."""
    changes = _changes(fields, creating=True)

    def write(database):
        _require_user(database, user_id, lock=True)
        active = _lock_active_journey(database, user_id)
        if active is None:
            if confirmation_token:
                raise _confirmation(_empty_preview(changes["elevation_m"]))
            activity_id = _insert_activity(database, user_id, changes)
            return _saved(database, activity_id, None, None), True

        proposal = _proposal(
            active,
            changes["elevation_m"],
            previous_credit=Decimal("0.00000"),
        )
        if proposal["will_complete"] and not _token_matches(
            confirmation_token,
            user_id,
            active["id"],
            active["progress_version"],
            proposal["credited_m"],
        ):
            raise _confirmation(_preview_from_proposal(active, proposal))
        activity_id = _insert_activity(database, user_id, changes)
        credited = None
        if proposal["credited_m"] > 0:
            _insert_credit(
                database,
                activity_id,
                active["id"],
                user_id,
                proposal["credited_m"],
            )
            _touch_journey(
                database,
                active["id"],
                credit_changed=True,
                complete=proposal["will_complete"],
            )
            _write_feed(database, user_id, activity_id, active, proposal)
            credited = proposal["credited_m"]
        return _saved(database, activity_id, credited, active["id"]), True

    return _commit(write)


def preview_activity_update(user_id: int, activity_id: int, fields: dict) -> dict:
    """Read-only preview of an edit, including a replacement journey credit.

    Call this before ``update_activity`` when the edit may finish the active
    journey. The returned token is bound to that journey's current progress
    version and to the replacement credit, not to the activity's full elevation.
    A paused or abandoned journey that would reach its target sets
    ``resume_required`` and does not issue a token.
    """
    _require_id(activity_id, "activity")
    changes = _changes(fields, creating=False)
    database = get_db()
    try:
        _require_user(database, user_id, lock=False)
        loaded = _load_owned(database, user_id, activity_id)
        return _preview_edit(database, loaded, changes)
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
    """Change the caller's activity. Elevation edits recalculate one credit."""
    _require_id(activity_id, "activity")
    changes = _changes(fields, creating=False)

    def write(database):
        _require_user(database, user_id, lock=True)
        loaded = _lock_owned(
            database,
            user_id,
            activity_id,
            lock_feed="elevation_m" in changes,
        )
        credit = loaded["credit"]
        journey = loaded["journey"]
        elevation_changed = (
            "elevation_m" in changes
            and changes["elevation_m"] != loaded["elevation_m"]
        )
        proposal = None
        if elevation_changed and credit is not None:
            if journey["status"] == "completed":
                raise ActivityError(
                    "invalid_state",
                    "Elevation on a completed journey cannot change.",
                    status=409,
                )
            proposal = _proposal(
                journey,
                changes["elevation_m"],
                previous_credit=credit["credited_elevation_m"],
            )
            if proposal["resume_required"]:
                raise ActivityError(
                    "resume_required",
                    "Resume this journey before it can be completed.",
                    status=409,
                    journey_id=journey["id"],
                    journey_status=journey["status"],
                )
            if proposal["will_complete"] and not _token_matches(
                confirmation_token,
                user_id,
                journey["id"],
                journey["progress_version"],
                proposal["credited_m"],
            ):
                raise _confirmation(_preview_from_proposal(journey, proposal))
        _apply_metadata(database, activity_id, loaded, changes)
        if elevation_changed:
            _apply_elevation(database, activity_id, credit, changes["elevation_m"], proposal)
            if proposal is not None and (
                proposal["credit_changed"] or proposal["will_complete"]
            ):
                _touch_journey(
                    database,
                    journey["id"],
                    credit_changed=proposal["credit_changed"],
                    complete=proposal["will_complete"],
                )
            if proposal is not None and proposal["will_complete"]:
                _write_summit_updates(
                    database,
                    user_id,
                    activity_id,
                    journey,
                    proposal["resulting_total_m"],
                )
        credited, journey_id = _credit_link(database, activity_id)
        return _saved(database, activity_id, credited, journey_id), True

    return _commit(write)


def delete_activity(user_id: int, activity_id: int) -> dict:
    """Delete the caller's activity and its journey credit when that is allowed."""
    _require_id(activity_id, "activity")

    def write(database):
        _require_user(database, user_id, lock=True)
        loaded = _lock_owned(database, user_id, activity_id)
        credit = loaded["credit"]
        journey = loaded["journey"]
        if credit is not None and journey["status"] == "completed":
            raise ActivityError(
                "invalid_state",
                "An activity credited to a completed journey cannot be deleted.",
                status=409,
            )
        if credit is not None:
            database.execute(
                "DELETE FROM journey_credits WHERE id = ? AND activity_id = ?",
                (credit["id"], activity_id),
            )
            _touch_journey(
                database,
                journey["id"],
                credit_changed=True,
                complete=False,
            )
        database.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
        summary = None if journey is None else _journey_summary(database, journey["id"])
        return {"activity_id": activity_id, "journey": summary}, True

    return _commit(write)


def list_activities(user_id: int) -> list[dict]:
    """The caller's own activities, newest first, with any journey credit beside them."""
    database = get_db()
    try:
        _require_user(database, user_id, lock=False)
        rows = database.execute(
            f"""
            SELECT {_ACTIVITY_COLUMNS},
                   credit.credited_elevation_m,
                   credit.journey_id,
                   journey.status AS journey_status,
                   journey.route_id AS credit_route_id,
                   route.slug AS route_slug
            FROM activities AS activity
            LEFT JOIN journey_credits AS credit ON credit.activity_id = activity.id
            LEFT JOIN route_journeys AS journey ON journey.id = credit.journey_id
            LEFT JOIN routes AS route ON route.id = journey.route_id
            WHERE activity.user_id = ?
            ORDER BY activity.activity_date DESC, activity.id DESC
            """,
            (user_id,),
        ).fetchall()
        return [_history_row(row) for row in rows]
    except ActivityError:
        database.rollback()
        raise
    finally:
        database.rollback()


def get_activity(user_id: int, activity_id: int) -> dict:
    """One of the caller's activities, including a journey credit when one exists."""
    _require_id(activity_id, "activity")
    database = get_db()
    try:
        _require_user(database, user_id, lock=False)
        row = _history_query(database, activity_id)
        if row is None:
            raise ActivityError("not_found", "That activity does not exist.", status=404)
        if row["user_id"] != user_id:
            raise ActivityError(
                "forbidden",
                "You cannot open another person's activity.",
                status=403,
            )
        return _history_row(row)
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
    """Lock the caller's active journey after the expeditions that can hear about it.

    The user row is already locked. Expeditions come next, then the journey,
    then the participations that still point at it.
    """
    found = database.execute(
        """
        SELECT id, route_id
        FROM route_journeys
        WHERE user_id = ? AND status = 'active'
        """,
        (user_id,),
    ).fetchone()
    if found is None:
        return None
    expedition_ids = _lock_expeditions(database, user_id, found["id"], found["route_id"])
    locked = database.execute(
        """
        SELECT id, user_id, route_id, status, progress_version
        FROM route_journeys
        WHERE id = ?
        FOR UPDATE
        """,
        (found["id"],),
    ).fetchone()
    if locked is None or locked["status"] != "active" or locked["user_id"] != user_id:
        raise ActivityError(
            "conflict",
            "The journey changed during this update. Try again.",
            status=409,
        )
    participations = _lock_participations(
        database, user_id, locked["id"], expedition_ids
    )
    described = _describe_journey(database, locked)
    described["participations"] = participations
    return described


def _lock_expeditions(database, user_id: int, journey_id: int, route_id: int) -> list[int]:
    rows = database.execute(
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
    ).fetchall()
    return [row["id"] for row in rows]


def _lock_participations(
    database, user_id: int, journey_id: int, expedition_ids: list[int]
) -> list[dict]:
    if not expedition_ids:
        return []
    placeholders = ", ".join("?" for _ in expedition_ids)
    database.execute(
        f"""
        SELECT participation.id
        FROM expedition_participations AS participation
        WHERE participation.user_id = ?
          AND participation.journey_id = ?
          AND participation.status = 'active'
          AND participation.expedition_id IN ({placeholders})
        ORDER BY participation.id
        FOR UPDATE
        """,
        (user_id, journey_id, *expedition_ids),
    )
    rows = database.execute(
        f"""
        SELECT participation.id, participation.expedition_id
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ?
          AND participation.journey_id = ?
          AND participation.status = 'active'
          AND participation.expedition_id IN ({placeholders})
          AND expedition.status = 'active'
          AND expedition.route_id = (
              SELECT route_id FROM route_journeys WHERE id = ?
          )
        ORDER BY participation.id
        """,
        (user_id, journey_id, *expedition_ids, journey_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _describe_journey(database, locked: dict) -> dict:
    route = database.execute(
        """
        SELECT slug, target_elevation_m
        FROM routes
        WHERE id = ?
        """,
        (locked["route_id"],),
    ).fetchone()
    total = _journey_total(database, locked["id"])
    target = _meters(route["target_elevation_m"])
    return {
        "id": locked["id"],
        "user_id": locked["user_id"],
        "route_id": locked["route_id"],
        "route_slug": route["slug"],
        "status": locked["status"],
        "progress_version": locked["progress_version"],
        "target_m": target,
        "total_m": total,
        "remaining_m": _remaining(target, total),
    }


def _lock_owned(database, user_id: int, activity_id: int, *, lock_feed: bool = False) -> dict:
    """Lock the user, then its journey, then the activity. Re-read after the locks.

    The caller has already locked the user. A non-locking read discovers the
    journey so the activity row is not locked before that journey. An elevation
    edit also locks the expeditions and participations that could receive a
    summit update, and it locks those before the journey.
    """
    owner = database.execute(
        "SELECT id, user_id FROM activities WHERE id = ?",
        (activity_id,),
    ).fetchone()
    if owner is None:
        raise ActivityError("not_found", "That activity does not exist.", status=404)
    if owner["user_id"] != user_id:
        raise ActivityError(
            "forbidden",
            "You cannot change another person's activity.",
            status=403,
        )
    hinted = _credit_journey_id(database, activity_id)
    participations: list[dict] = []
    if hinted is not None and lock_feed:
        route = database.execute(
            "SELECT route_id FROM route_journeys WHERE id = ?",
            (hinted,),
        ).fetchone()
        if route is None:
            raise ActivityError(
                "conflict",
                "The activity changed during this update. Try again.",
                status=409,
            )
        expedition_ids = _lock_expeditions(database, user_id, hinted, route["route_id"])
        database.execute(
            """
            SELECT id
            FROM route_journeys
            WHERE id = ?
            FOR UPDATE
            """,
            (hinted,),
        )
        participations = _lock_participations(database, user_id, hinted, expedition_ids)
    elif hinted is not None:
        database.execute(
            """
            SELECT id
            FROM route_journeys
            WHERE id = ?
            FOR UPDATE
            """,
            (hinted,),
        )
    database.execute(
        "SELECT id FROM activities WHERE id = ? FOR UPDATE",
        (activity_id,),
    )
    loaded = _load_owned(database, user_id, activity_id)
    journey_id = None if loaded["journey"] is None else loaded["journey"]["id"]
    if journey_id != hinted:
        raise ActivityError(
            "conflict",
            "The activity changed during this update. Try again.",
            status=409,
        )
    if loaded["journey"] is not None:
        loaded["journey"]["participations"] = participations
    return loaded


def _credit_journey_id(database, activity_id: int) -> int | None:
    row = database.execute(
        """
        SELECT journey_id
        FROM journey_credits
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
    credit_row = database.execute(
        """
        SELECT id, credited_elevation_m, journey_id, user_id
        FROM journey_credits
        WHERE activity_id = ?
        """,
        (activity_id,),
    ).fetchone()
    journey = None
    credit = None
    if credit_row is not None:
        if credit_row["user_id"] != user_id:
            raise ActivityError(
                "forbidden",
                "You cannot change another person's activity.",
                status=403,
            )
        locked = database.execute(
            """
            SELECT id, user_id, route_id, status, progress_version
            FROM route_journeys
            WHERE id = ?
            """,
            (credit_row["journey_id"],),
        ).fetchone()
        journey = _describe_journey(database, locked)
        credit = {
            "id": credit_row["id"],
            "credited_elevation_m": _meters(credit_row["credited_elevation_m"]),
            "journey_id": credit_row["journey_id"],
        }
    return {
        "elevation_m": _meters(row["elevation_gain_m"]),
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "credit": credit,
        "journey": journey,
    }


def _preview_new(database, user_id: int, elevation_m: Decimal) -> dict:
    found = database.execute(
        """
        SELECT id, user_id, route_id, status, progress_version
        FROM route_journeys
        WHERE user_id = ? AND status = 'active'
        """,
        (user_id,),
    ).fetchone()
    if found is None:
        return _empty_preview(elevation_m)
    journey = _describe_journey(database, found)
    proposal = _proposal(journey, elevation_m, previous_credit=Decimal("0.00000"))
    return _preview_from_proposal(journey, proposal)


def _preview_edit(database, loaded: dict, changes: dict) -> dict:
    elevation = changes.get("elevation_m", loaded["elevation_m"])
    credit = loaded["credit"]
    journey = loaded["journey"]
    if credit is None or journey is None or "elevation_m" not in changes:
        preview = _empty_preview(elevation)
        preview["activity_elevation_m"] = elevation
        if credit is not None and journey is not None:
            preview = _preview_from_proposal(
                journey,
                _proposal(
                    journey,
                    loaded["elevation_m"],
                    previous_credit=credit["credited_elevation_m"],
                ),
            )
            preview["will_complete"] = False
            preview["resume_required"] = False
            preview["confirmation_token"] = None
            preview["confirmation_expires_at"] = None
        return preview
    if elevation == loaded["elevation_m"]:
        proposal = _proposal(
            journey,
            elevation,
            previous_credit=credit["credited_elevation_m"],
        )
        proposal["will_complete"] = False
        proposal["resume_required"] = False
        return _preview_from_proposal(journey, proposal)
    if journey["status"] == "completed":
        raise ActivityError(
            "invalid_state",
            "Elevation on a completed journey cannot change.",
            status=409,
        )
    proposal = _proposal(
        journey,
        elevation,
        previous_credit=credit["credited_elevation_m"],
    )
    return _preview_from_proposal(journey, proposal)


def _proposal(journey: dict, elevation_m: Decimal, *, previous_credit: Decimal) -> dict:
    """Credit math for one journey, excluding this activity's current credit."""
    base = journey["total_m"] - _meters(previous_credit)
    if base < 0:
        base = Decimal("0.00000")
    capacity = _remaining(journey["target_m"], base)
    credited = elevation_m if elevation_m < capacity else capacity
    credited = _meters(credited)
    if credited < 0:
        credited = Decimal("0.00000")
    resulting = _meters(base + credited)
    hits_target = credited > 0 and resulting >= journey["target_m"]
    status = journey["status"]
    return {
        "previous_credited_m": _meters(previous_credit),
        "credited_m": credited,
        "resulting_total_m": resulting,
        "activity_elevation_m": elevation_m,
        "will_complete": status == "active" and hits_target,
        "resume_required": status in ("paused", "abandoned") and hits_target,
        "credit_changed": credited != _meters(previous_credit),
    }


def _preview_from_proposal(journey: dict, proposal: dict) -> dict:
    token = None
    expires = None
    if proposal["will_complete"] and not proposal["resume_required"]:
        expires = _now() + TOKEN_TTL_SECONDS
        token = _issue_confirmation_token(
            journey["user_id"],
            journey["id"],
            journey["progress_version"],
            proposal["credited_m"],
            expires,
        )
    credited = proposal["credited_m"]
    return {
        "journey": _journey_public(journey),
        "activity_elevation_m": proposal["activity_elevation_m"],
        "previous_credited_elevation_m": proposal["previous_credited_m"],
        "credited_elevation_m": credited,
        "resulting_total_m": proposal["resulting_total_m"],
        "will_complete": proposal["will_complete"],
        "credits_journey": credited > 0,
        "saved_without_credit": credited <= 0,
        "resume_required": proposal["resume_required"],
        "confirmation_token": token,
        "confirmation_expires_at": expires,
    }


def _empty_preview(elevation_m: Decimal) -> dict:
    zero = Decimal("0.00000")
    return {
        "journey": None,
        "activity_elevation_m": elevation_m,
        "previous_credited_elevation_m": zero,
        "credited_elevation_m": zero,
        "resulting_total_m": None,
        "will_complete": False,
        "credits_journey": False,
        "saved_without_credit": True,
        "resume_required": False,
        "confirmation_token": None,
        "confirmation_expires_at": None,
    }


def _journey_public(journey: dict) -> dict:
    return {
        "id": journey["id"],
        "status": journey["status"],
        "progress_version": journey["progress_version"],
        "route_id": journey["route_id"],
        "route_slug": journey["route_slug"],
        "total_m": journey["total_m"],
        "remaining_m": journey["remaining_m"],
        "target_m": journey["target_m"],
    }


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
            changes["elevation_m"],
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


def _insert_credit(database, activity_id: int, journey_id: int, user_id: int, credited: Decimal) -> None:
    database.execute(
        """
        INSERT INTO journey_credits (
            activity_id, journey_id, user_id, credited_elevation_m
        )
        VALUES (?, ?, ?, ?)
        """,
        (activity_id, journey_id, user_id, credited),
    )


def _touch_journey(database, journey_id: int, *, credit_changed: bool, complete: bool) -> None:
    if complete and credit_changed:
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
    elif complete:
        cursor = database.execute(
            """
            UPDATE route_journeys
            SET status = 'completed', completed_at = now()
            WHERE id = ? AND status = 'active'
            """,
            (journey_id,),
        )
    elif credit_changed:
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


def _write_feed(database, user_id: int, activity_id: int, journey: dict, proposal: dict) -> None:
    """Activity, checkpoint, and summit rows for participations still on this journey."""
    participations = journey.get("participations") or []
    if not participations or proposal["credited_m"] <= 0:
        return
    previous = journey["total_m"]
    resulting = proposal["resulting_total_m"]
    checkpoints = database.execute(
        """
        SELECT name, elevation_m
        FROM route_checkpoints
        WHERE route_id = ?
          AND elevation_m > ?
          AND elevation_m <= ?
        ORDER BY elevation_m, position
        """,
        (journey["route_id"], previous, resulting),
    ).fetchall()
    for participation in participations:
        _feed_row(
            database,
            participation,
            user_id,
            "activity_logged",
            activity_id,
            None,
            proposal["credited_m"],
        )
        for checkpoint in checkpoints:
            _feed_row(
                database,
                participation,
                user_id,
                "checkpoint_reached",
                activity_id,
                checkpoint["name"],
                resulting,
            )
        if proposal["will_complete"]:
            _feed_row(
                database,
                participation,
                user_id,
                "summit_reached",
                activity_id,
                None,
                resulting,
            )


def _write_summit_updates(
    database,
    user_id: int,
    activity_id: int,
    journey: dict,
    resulting_total_m: Decimal,
) -> None:
    """Summit rows for an elevation edit that completes the active journey.

    Edits do not log the activity again and do not announce checkpoints.
    """
    for participation in journey.get("participations") or []:
        _feed_row(
            database,
            participation,
            user_id,
            "summit_reached",
            activity_id,
            None,
            resulting_total_m,
        )


def _feed_row(
    database,
    participation: dict,
    user_id: int,
    kind: str,
    activity_id: int,
    checkpoint_name: str | None,
    elevation_m: Decimal,
) -> None:
    database.execute(
        """
        INSERT INTO expedition_updates (
            expedition_id, user_id, participation_id, kind,
            activity_id, checkpoint_name, elevation_m
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            participation["expedition_id"],
            user_id,
            participation["id"],
            kind,
            activity_id,
            checkpoint_name,
            elevation_m,
        ),
    )


def _apply_metadata(database, activity_id: int, loaded: dict, changes: dict) -> None:
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


def _apply_elevation(database, activity_id: int, credit: dict | None, new_elevation: Decimal, proposal: dict | None) -> None:
    if credit is None or proposal is None:
        database.execute(
            "UPDATE activities SET elevation_gain_m = ? WHERE id = ?",
            (new_elevation, activity_id),
        )
        return
    new_credit = proposal["credited_m"]
    old_credit = credit["credited_elevation_m"]
    if new_credit <= 0:
        database.execute(
            "DELETE FROM journey_credits WHERE id = ? AND activity_id = ?",
            (credit["id"], activity_id),
        )
        database.execute(
            "UPDATE activities SET elevation_gain_m = ? WHERE id = ?",
            (new_elevation, activity_id),
        )
        return
    if new_credit < old_credit:
        database.execute(
            """
            UPDATE journey_credits
            SET credited_elevation_m = ?
            WHERE id = ? AND activity_id = ?
            """,
            (new_credit, credit["id"], activity_id),
        )
        database.execute(
            "UPDATE activities SET elevation_gain_m = ? WHERE id = ?",
            (new_elevation, activity_id),
        )
        return
    database.execute(
        "UPDATE activities SET elevation_gain_m = ? WHERE id = ?",
        (new_elevation, activity_id),
    )
    if new_credit > old_credit:
        database.execute(
            """
            UPDATE journey_credits
            SET credited_elevation_m = ?
            WHERE id = ? AND activity_id = ?
            """,
            (new_credit, credit["id"], activity_id),
        )


def _saved(database, activity_id: int, credited: Decimal | None, journey_id: int | None) -> dict:
    row = database.execute(
        f"SELECT {_ACTIVITY_COLUMNS} FROM activities AS activity WHERE activity.id = ?",
        (activity_id,),
    ).fetchone()
    journey = None if journey_id is None else _journey_summary(database, journey_id)
    return {
        "activity": _activity_dict(row),
        "credited_elevation_m": None if credited is None else _meters(credited),
        "journey": journey,
    }


def _journey_summary(database, journey_id: int) -> dict:
    row = database.execute(
        """
        SELECT id, user_id, route_id, status, progress_version
        FROM route_journeys
        WHERE id = ?
        """,
        (journey_id,),
    ).fetchone()
    return _journey_public(_describe_journey(database, row))


def _credit_link(database, activity_id: int) -> tuple[Decimal | None, int | None]:
    row = database.execute(
        """
        SELECT credited_elevation_m, journey_id
        FROM journey_credits
        WHERE activity_id = ?
        """,
        (activity_id,),
    ).fetchone()
    if row is None:
        return None, None
    return _meters(row["credited_elevation_m"]), row["journey_id"]


_ACTIVITY_COLUMNS = """
    activity.id, activity.user_id, activity.activity_type, activity.activity_date,
    activity.started_at, activity.ended_at, activity.duration_s, activity.distance_m,
    activity.elevation_gain_m, activity.note, activity.source,
    activity.verification_status, activity.created_at, activity.updated_at
"""


def _history_query(database, activity_id: int):
    return database.execute(
        f"""
        SELECT {_ACTIVITY_COLUMNS},
               credit.credited_elevation_m,
               credit.journey_id,
               journey.status AS journey_status,
               journey.route_id AS credit_route_id,
               route.slug AS route_slug
        FROM activities AS activity
        LEFT JOIN journey_credits AS credit ON credit.activity_id = activity.id
        LEFT JOIN route_journeys AS journey ON journey.id = credit.journey_id
        LEFT JOIN routes AS route ON route.id = journey.route_id
        WHERE activity.id = ?
        """,
        (activity_id,),
    ).fetchone()


def _history_row(row: dict) -> dict:
    record = _activity_dict(row)
    if row["journey_id"] is None:
        record["credit"] = None
    else:
        record["credit"] = {
            "credited_elevation_m": _meters(row["credited_elevation_m"]),
            "journey_id": row["journey_id"],
            "journey_status": row["journey_status"],
            "route_id": row["credit_route_id"],
            "route_slug": row["route_slug"],
        }
    return record


def _activity_dict(row: dict) -> dict:
    distance = row["distance_m"]
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "activity_type": row["activity_type"],
        "activity_date": row["activity_date"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_s": row["duration_s"],
        "distance_m": None if distance is None else _distance(distance),
        "elevation_gain_m": _meters(row["elevation_gain_m"]),
        "note": row["note"],
        "source": row["source"],
        "verification_status": row["verification_status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _changes(fields: dict, *, creating: bool) -> dict:
    if not isinstance(fields, dict):
        raise ActivityError("invalid", "Activity details have to be an object.", status=400)
    unknown = set(fields) - _FIELDS
    if unknown:
        raise ActivityError("invalid", "That activity field is not supported.", status=400)
    if creating and "elevation_ft" not in fields:
        raise ActivityError("invalid", "Add elevation.", status=400)
    if not creating and not fields:
        raise ActivityError("invalid", "Choose something to change.", status=400)
    changes: dict = {}
    if "elevation_ft" in fields:
        changes["elevation_m"] = _elevation(fields["elevation_ft"])
    if "distance_km" in fields:
        changes["distance_m"] = _distance_input(fields["distance_km"])
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


def _elevation(value) -> Decimal:
    feet = _decimal(value, "Elevation")
    if feet <= 0:
        raise ActivityError("invalid", "Elevation has to be more than zero.", status=400)
    if feet > MAX_ELEVATION_FT:
        raise ActivityError(
            "invalid",
            "Log one outing at a time, up to 10,000 ft. Split a bigger day.",
            status=400,
        )
    meters = feet_to_meters(feet)
    if meters <= 0:
        raise ActivityError("invalid", "Elevation has to be more than zero.", status=400)
    return meters


def _distance_input(value) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    kilometers = _decimal(value, "Distance")
    if kilometers <= 0:
        raise ActivityError(
            "invalid",
            "Distance has to be more than zero, or leave it blank.",
            status=400,
        )
    if kilometers > MAX_DISTANCE_KM:
        raise ActivityError(
            "invalid",
            "Distance looks too long for one outing (max 120 km).",
            status=400,
        )
    meters = kilometers_to_meters(kilometers)
    if meters <= 0:
        raise ActivityError(
            "invalid",
            "Distance has to be more than zero, or leave it blank.",
            status=400,
        )
    return meters


def _decimal(value, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ActivityError("invalid", f"{label} should be a number.", status=400)
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            raise ActivityError("invalid", f"Add {label.lower()}.", status=400)
        try:
            number = Decimal(text)
        except InvalidOperation as exc:
            raise ActivityError("invalid", f"{label} should be a number.", status=400) from exc
    else:
        raise ActivityError("invalid", f"{label} should be a number.", status=400)
    if not number.is_finite():
        raise ActivityError("invalid", f"{label} should be a number.", status=400)
    return number


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


def _journey_total(database, journey_id: int) -> Decimal:
    row = database.execute(
        """
        SELECT COALESCE(SUM(credited_elevation_m), 0) AS total_m
        FROM journey_credits
        WHERE journey_id = ?
        """,
        (journey_id,),
    ).fetchone()
    return _meters(row["total_m"])


def _remaining(target: Decimal, total: Decimal) -> Decimal:
    remaining = _meters(target - total)
    if remaining < 0:
        return Decimal("0.00000")
    return remaining


def _meters(value) -> Decimal:
    return Decimal(value).quantize(ELEVATION_QUANTUM, rounding=ROUND_HALF_UP)


def _distance(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _now() -> int:
    return int(time.time())


def _canonical_meters(value) -> str:
    return format(_meters(value), "f")


def _issue_confirmation_token(
    user_id: int,
    journey_id: int,
    progress_version: int,
    credited_elevation_m,
    exp_unix: int,
) -> str:
    payload = (
        f"v1|{user_id}|{journey_id}|{progress_version}|"
        f"{_canonical_meters(credited_elevation_m)}|{exp_unix}"
    )
    signature = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{signature}"


def _token_matches(token, user_id: int, journey_id: int, progress_version: int, credited) -> bool:
    if not isinstance(token, str):
        return False
    payload, separator, signature = token.rpartition("|")
    if not separator or payload.count("|") != 5:
        return False
    parts = payload.split("|")
    if len(parts) != 6:
        return False
    expected = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if len(signature) != len(expected) or not hmac.compare_digest(expected, signature):
        return False
    version, token_user, token_journey, token_progress, token_credit, token_exp = parts
    if version != "v1":
        return False
    if token_exp.startswith("-") or not token_exp.isdigit():
        return False
    if int(token_exp) <= _now():
        return False
    if token_user != str(user_id) or token_journey != str(journey_id):
        return False
    if token_progress != str(progress_version):
        return False
    return token_credit == _canonical_meters(credited)


def _secret() -> bytes:
    secret = current_app.config.get("SECRET_KEY")
    if isinstance(secret, str) and secret:
        return secret.encode("utf-8")
    if isinstance(secret, bytes) and secret:
        return secret
    raise RuntimeError("SECRET_KEY is not configured.")
