"""Route-journey lifecycle.

A journey is one person's progress on one route. These operations pause,
resume, abandon, and restart that record. They do not write activities,
credits, or expedition memberships. Restart repoints participations that
already exist on active expeditions, because that pointer is part of the
restart transaction.
"""

from __future__ import annotations

from .db import get_db

_JOURNEY = """
    journey.id, journey.user_id, journey.route_id, route.slug AS route_slug,
    journey.status, journey.attempt_number, journey.progress_version,
    journey.is_current, journey.started_at, journey.paused_at,
    journey.completed_at, journey.abandoned_at, journey.created_at,
    journey.activity_policy, journey.governing_expedition_id
"""


class JourneyError(Exception):
    def __init__(self, code: str, message: str, **details) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def translate_database_error(exc: BaseException) -> JourneyError | None:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "23505":
        return JourneyError(
            "conflict",
            "The journey changed during this update. Try again.",
        )
    if sqlstate == "P0001" and "journey status cannot change" in str(exc):
        return JourneyError("invalid_state", "That journey transition is not allowed.")
    return None


def active_journey(user_id: int) -> dict | None:
    database = get_db()
    try:
        _require_user(database, user_id, lock=False)
        return _active(database, user_id)
    except JourneyError:
        database.rollback()
        raise
    finally:
        database.rollback()


def read_journeys(database, user_id: int, route_id: int | None = None) -> list[dict]:
    """The caller's journeys. Selects only. The caller owns the transaction."""
    _require_user(database, user_id, lock=False)
    if route_id is not None:
        _require_route(database, route_id)
    rows = database.execute(
        f"""
        SELECT {_JOURNEY}
        FROM route_journeys AS journey
        JOIN routes AS route ON route.id = journey.route_id
        WHERE journey.user_id = ?
          AND (?::bigint IS NULL OR journey.route_id = ?)
        ORDER BY route.slug, journey.attempt_number
        """,
        (user_id, route_id, route_id),
    ).fetchall()
    return [dict(row) for row in rows]


def list_journeys(user_id: int, route_id: int | None = None) -> list[dict]:
    database = get_db()
    try:
        return read_journeys(database, user_id, route_id)
    except JourneyError:
        database.rollback()
        raise
    finally:
        database.rollback()


def get_journey(actor_id: int, journey_id: int) -> dict:
    database = get_db()
    try:
        _require_user(database, actor_id, lock=False)
        if not isinstance(journey_id, int) or isinstance(journey_id, bool) or journey_id <= 0:
            raise JourneyError("not_found", "That journey does not exist.")
        journey = _load(database, journey_id)
        if journey is None:
            raise JourneyError("not_found", "That journey does not exist.")
        if journey["user_id"] != actor_id:
            raise JourneyError("forbidden", "You cannot open another person's journey.")
        return journey
    except JourneyError:
        database.rollback()
        raise
    finally:
        database.rollback()


def start_journey(user_id: int, route_id: int) -> dict:
    def write(database):
        _require_user(database, user_id, lock=True)
        route = _require_route(database, route_id)
        _lock_active_expeditions(database, route["id"])
        _lock_journeys(database, user_id, route["id"])
        current = _current(database, user_id, route["id"])
        if current is not None:
            if current["status"] in ("completed", "abandoned"):
                raise _restart_required(database, user_id, route, current["status"])
            raise JourneyError("invalid_state", "This route already has a journey.")
        if route["status"] != "published":
            raise JourneyError("not_found", "That route does not exist.")
        _pause_other(database, user_id, route["id"])
        return _insert_attempt(database, user_id, route["id"], 1), True

    return _commit(write)


def switch_journey(user_id: int, route_id: int) -> dict:
    def write(database):
        return _activate(database, user_id, route_id, allow_first=True)

    return _commit(write)


def resume_journey(user_id: int, route_id: int) -> dict:
    def write(database):
        return _activate(database, user_id, route_id, allow_first=False)

    return _commit(write)


def abandon_journey(user_id: int, route_id: int) -> dict:
    def write(database):
        _require_user(database, user_id, lock=True)
        route = _require_route(database, route_id)
        _lock_journeys(database, user_id, route["id"])
        current = _current(database, user_id, route["id"])
        if current is None:
            raise JourneyError("not_found", "This route does not have a journey.")
        if current["status"] == "completed":
            raise JourneyError("invalid_state", "A completed journey cannot be abandoned.")
        if current["status"] == "abandoned":
            raise JourneyError("invalid_state", "This journey is already abandoned.")
        if current["status"] not in ("active", "paused"):
            raise JourneyError("invalid_state", "That journey transition is not allowed.")
        database.execute(
            """
            UPDATE route_journeys
            SET status = 'abandoned', abandoned_at = now()
            WHERE id = ?
            """,
            (current["id"],),
        )
        return _load(database, current["id"]), True

    return _commit(write)


def preview_activity_policy(user_id: int, journey_id: int, operation: str) -> dict:
    """Read-only expansion preview. Nothing is written."""
    if operation != "expand":
        raise JourneyError("invalid", "Send operation expand.")
    database = get_db()
    try:
        _require_user(database, user_id, lock=False)
        journey = _owned(database, user_id, journey_id)
        from .activity_policy import PolicyError, assert_join

        try:
            assert_join(
                database,
                user_id=user_id,
                expedition_id=None,
                journey_id=journey["id"],
                route_id=journey["route_id"],
                prior_status=journey["status"],
                confirmation_token=None,
                operation="expand",
            )
        except PolicyError as exc:
            if exc.code != "confirmation_required":
                raise _policy_as_journey(exc) from exc
            return {
                "policy_preview": exc.details["policy_preview"],
                "confirmation_token": exc.details["confirmation_token"],
            }
        raise JourneyError("invalid_state", "This journey cannot change activity rules.")
    except JourneyError:
        database.rollback()
        raise
    finally:
        database.rollback()


def choose_activity_policy(user_id: int, journey_id: int, choice: str, confirmation_token: str | None) -> dict:
    if choice not in ("keep", "expand"):
        raise JourneyError("invalid", "Choose keep or expand.")

    def write(database):
        _require_user(database, user_id, lock=True)
        journey = _owned_locked(database, user_id, journey_id)
        from .activities import apply_eligibility
        from .activity_policy import (
            PolicyError,
            UNRESTRICTED,
            assert_join,
            clear_choice,
            store_journey_policy,
        )

        if choice == "keep":
            pending = database.execute(
                "SELECT journey_id FROM journey_policy_choices WHERE journey_id = ?",
                (journey["id"],),
            ).fetchone()
            if pending is None:
                raise JourneyError("invalid_state", "There is no activity-rule choice waiting.")
            clear_choice(database, journey["id"])
            return _load(database, journey["id"]), True
        try:
            decision = assert_join(
                database,
                user_id=user_id,
                expedition_id=None,
                journey_id=journey["id"],
                route_id=journey["route_id"],
                prior_status=journey["status"],
                confirmation_token=confirmation_token,
                operation="expand",
            )
        except PolicyError as exc:
            raise _policy_as_journey(exc) from exc
        if journey["status"] == "paused":
            database.execute(
                """
                UPDATE route_journeys
                SET status = 'active'
                WHERE id = ? AND status = 'paused'
                """,
                (journey["id"],),
            )
        store_journey_policy(database, journey["id"], UNRESTRICTED, None)
        clear_choice(database, journey["id"])
        apply_eligibility(
            database,
            user_id,
            journey["id"],
            policy_changed=True,
            complete=bool(decision.get("complete")),
        )
        return _load(database, journey["id"]), True

    return _commit(write)


def _policy_as_journey(exc) -> JourneyError:
    error = JourneyError(exc.code, str(exc), **exc.details)
    return error


def _owned(database, user_id: int, journey_id: int) -> dict:
    if not isinstance(journey_id, int) or isinstance(journey_id, bool) or journey_id <= 0:
        raise JourneyError("not_found", "That journey does not exist.")
    row = database.execute(
        f"""
        SELECT {_JOURNEY}
        FROM route_journeys AS journey
        JOIN routes AS route ON route.id = journey.route_id
        WHERE journey.id = ? AND journey.user_id = ?
        """,
        (journey_id, user_id),
    ).fetchone()
    if row is None:
        raise JourneyError("not_found", "That journey does not exist.")
    return row


def _owned_locked(database, user_id: int, journey_id: int) -> dict:
    database.execute(
        """
        SELECT id FROM expeditions
        WHERE route_id = (
            SELECT route_id FROM route_journeys WHERE id = ? AND user_id = ?
        ) AND status = 'active'
        ORDER BY id
        FOR UPDATE
        """,
        (journey_id, user_id),
    )
    database.execute(
        """
        SELECT id FROM route_journeys
        WHERE id = ? AND user_id = ?
        FOR UPDATE
        """,
        (journey_id, user_id),
    )
    return _owned(database, user_id, journey_id)


def restart_journey(user_id: int, route_id: int) -> dict:
    def write(database):
        _require_user(database, user_id, lock=True)
        route = _require_route(database, route_id)
        _lock_active_expeditions(database, route["id"])
        return _restart_locked(database, user_id, route["id"]), True

    return _commit(write)


def _restart_locked(database, user_id: int, route_id: int, adopt=None) -> dict:
    """Restart one route. The user and this route's active expeditions are locked."""
    route = _require_route(database, route_id)
    _lock_journeys(database, user_id, route["id"])
    current = _current(database, user_id, route["id"])
    if current is None:
        raise JourneyError("not_found", "This route does not have a journey.")
    paused = _pause_other(database, user_id, route["id"])
    database.execute(
        """
        UPDATE route_journeys
        SET is_current = false,
            status = CASE
                WHEN status IN ('active', 'paused') THEN 'abandoned'
                ELSE status
            END,
            abandoned_at = CASE
                WHEN status IN ('active', 'paused') THEN now()
                ELSE abandoned_at
            END
        WHERE id = ?
        """,
        (current["id"],),
    )
    attempt = _next_attempt(database, user_id, route["id"])
    created = _insert_attempt(database, user_id, route["id"], attempt)
    reset = _repoint_active_participations(database, user_id, route["id"], created["id"])
    from .activity_policy import clear_choice, copy_journey_policy, refresh_governor, store_journey_policy

    if adopt is None:
        copy_journey_policy(database, current["id"], created["id"])
    else:
        policy, governor_id = adopt
        store_journey_policy(database, created["id"], policy, governor_id)
        clear_choice(database, current["id"])
        refresh_governor(database, created["id"], reason=None, former_id=None)
    return {
        "journey": created,
        "paused_journey": None if paused is None else _load(database, paused["id"]),
        "reset_expedition_slugs": reset,
    }


def activate_for_participation(
    database,
    user_id: int,
    route_id: int,
    *,
    confirm_new_attempt: bool,
    attach_completed: bool = False,
    adopt=None,
) -> dict:
    """Choose the journey a participation should point at.

    The caller is inside an open transaction and has already locked the user
    and the active expeditions on this route. This locks that user's journeys
    and, on a confirmed restart, the participations restart repoints. It does
    not commit. A completed or abandoned current journey raises restart_required
    unless confirm_new_attempt is true.
    """
    route = _require_route(database, route_id)
    _lock_journeys(database, user_id, route["id"])
    current = _current(database, user_id, route["id"])
    if current is None:
        if route["status"] != "published":
            raise JourneyError("not_found", "That route does not exist.")
        _pause_other(database, user_id, route["id"])
        return _insert_attempt(database, user_id, route["id"], 1)
    if current["status"] == "active":
        return current
    if current["status"] == "paused":
        _pause_other(database, user_id, route["id"])
        database.execute(
            """
            UPDATE route_journeys
            SET status = 'active'
            WHERE id = ? AND status = 'paused'
            """,
            (current["id"],),
        )
        return _load(database, current["id"])
    if current["status"] in ("completed", "abandoned"):
        if (
            current["status"] == "completed"
            and attach_completed
            and not confirm_new_attempt
        ):
            return current
        if not confirm_new_attempt:
            raise _restart_required(database, user_id, route, current["status"])
        restarted = _restart_locked(database, user_id, route["id"], adopt=adopt)
        return restarted["journey"]
    raise JourneyError("invalid_state", "That journey transition is not allowed.")


def _activate(database, user_id: int, route_id: int, allow_first: bool) -> tuple[dict, bool]:
    _require_user(database, user_id, lock=True)
    route = _require_route(database, route_id)
    _lock_active_expeditions(database, route["id"])
    _lock_journeys(database, user_id, route["id"])
    current = _current(database, user_id, route["id"])
    if current is None:
        if not allow_first:
            raise JourneyError("not_found", "This route does not have a journey.")
        if route["status"] != "published":
            raise JourneyError("not_found", "That route does not exist.")
        _pause_other(database, user_id, route["id"])
        return _insert_attempt(database, user_id, route["id"], 1), True
    if current["status"] in ("completed", "abandoned"):
        raise _restart_required(database, user_id, route, current["status"])
    if current["status"] == "active":
        if allow_first:
            return current, False
        raise JourneyError("invalid_state", "This route does not have a paused journey.")
    if current["status"] != "paused":
        raise JourneyError("invalid_state", "That journey transition is not allowed.")
    _pause_other(database, user_id, route["id"])
    database.execute(
        """
        UPDATE route_journeys
        SET status = 'active'
        WHERE id = ? AND status = 'paused'
        """,
        (current["id"],),
    )
    return _load(database, current["id"]), True


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
        if isinstance(exc, JourneyError):
            raise
        translated = translate_database_error(exc)
        if translated is not None:
            raise translated from exc
        raise
    return result


def _rollback_preserving(database, original: BaseException) -> None:
    """Roll back a write that did not commit.

    A rollback failure stays attached to the original exception so the
    caller's error code and type are the ones that propagate.
    """
    try:
        database.rollback()
    except Exception as rollback_error:
        raise original from rollback_error


def _require_user(database, user_id: int, *, lock: bool) -> None:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise JourneyError("not_found", "That account does not exist.")
    locking = " FOR UPDATE" if lock else ""
    row = database.execute(
        f"SELECT id, deletion_state FROM users WHERE id = ?{locking}",
        (user_id,),
    ).fetchone()
    if row is None:
        raise JourneyError("not_found", "That account does not exist.")
    if row["deletion_state"] == "deleting":
        raise JourneyError("forbidden", "This account is being deleted.")


def _require_route(database, route_id: int) -> dict:
    if not isinstance(route_id, int) or isinstance(route_id, bool) or route_id <= 0:
        raise JourneyError("not_found", "That route does not exist.")
    row = database.execute(
        "SELECT id, slug, status FROM routes WHERE id = ?",
        (route_id,),
    ).fetchone()
    if row is None:
        raise JourneyError("not_found", "That route does not exist.")
    return row


def _lock_active_expeditions(database, route_id: int) -> None:
    database.execute(
        """
        SELECT id
        FROM expeditions
        WHERE route_id = ? AND status = 'active'
        ORDER BY id
        FOR UPDATE
        """,
        (route_id,),
    ).fetchall()


def _lock_journeys(database, user_id: int, route_id: int) -> None:
    database.execute(
        """
        SELECT id
        FROM route_journeys
        WHERE user_id = ?
          AND (route_id = ? OR status = 'active')
        ORDER BY id
        FOR UPDATE
        """,
        (user_id, route_id),
    ).fetchall()


def _pause_other(database, user_id: int, route_id: int) -> dict | None:
    row = database.execute(
        """
        UPDATE route_journeys
        SET status = 'paused', paused_at = now()
        WHERE user_id = ? AND status = 'active' AND route_id <> ?
        RETURNING id
        """,
        (user_id, route_id),
    ).fetchone()
    if row is None:
        return None
    return _load(database, row["id"])


def _insert_attempt(database, user_id: int, route_id: int, attempt: int) -> dict:
    row = database.execute(
        """
        INSERT INTO route_journeys (
            user_id, route_id, status, attempt_number, progress_version,
            is_current, started_at
        )
        VALUES (?, ?, 'active', ?, 1, true, now())
        RETURNING id
        """,
        (user_id, route_id, attempt),
    ).fetchone()
    return _load(database, row["id"])


def _next_attempt(database, user_id: int, route_id: int) -> int:
    row = database.execute(
        """
        SELECT COALESCE(MAX(attempt_number), 0) + 1 AS attempt_number
        FROM route_journeys
        WHERE user_id = ? AND route_id = ?
        """,
        (user_id, route_id),
    ).fetchone()
    return row["attempt_number"]


def _repoint_active_participations(database, user_id: int, route_id: int, journey_id: int) -> list[str]:
    database.execute(
        """
        SELECT participation.id
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ?
          AND participation.status = 'active'
          AND expedition.route_id = ?
          AND expedition.status = 'active'
        ORDER BY participation.id
        FOR UPDATE OF participation
        """,
        (user_id, route_id),
    ).fetchall()
    rows = database.execute(
        """
        UPDATE expedition_participations AS participation
        SET journey_id = ?
        FROM expeditions AS expedition
        WHERE expedition.id = participation.expedition_id
          AND participation.user_id = ?
          AND participation.status = 'active'
          AND expedition.route_id = ?
          AND expedition.status = 'active'
        RETURNING expedition.slug
        """,
        (journey_id, user_id, route_id),
    ).fetchall()
    return sorted(row["slug"] for row in rows)


def _restart_required(database, user_id: int, route: dict, status: str) -> JourneyError:
    rows = database.execute(
        """
        SELECT expedition.slug
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ?
          AND participation.status = 'active'
          AND expedition.route_id = ?
          AND expedition.status = 'active'
        ORDER BY expedition.slug
        """,
        (user_id, route["id"]),
    ).fetchall()
    return JourneyError(
        "restart_required",
        "This route needs an explicit restart.",
        route_slug=route["slug"],
        status=status,
        reset_expedition_slugs=[row["slug"] for row in rows],
    )


def _current(database, user_id: int, route_id: int) -> dict | None:
    return database.execute(
        f"""
        SELECT {_JOURNEY}
        FROM route_journeys AS journey
        JOIN routes AS route ON route.id = journey.route_id
        WHERE journey.user_id = ? AND journey.route_id = ? AND journey.is_current
        """,
        (user_id, route_id),
    ).fetchone()


def _active(database, user_id: int) -> dict | None:
    return database.execute(
        f"""
        SELECT {_JOURNEY}
        FROM route_journeys AS journey
        JOIN routes AS route ON route.id = journey.route_id
        WHERE journey.user_id = ? AND journey.status = 'active'
        """,
        (user_id,),
    ).fetchone()


def _load(database, journey_id: int) -> dict | None:
    return database.execute(
        f"""
        SELECT {_JOURNEY}
        FROM route_journeys AS journey
        JOIN routes AS route ON route.id = journey.route_id
        WHERE journey.id = ?
        """,
        (journey_id,),
    ).fetchone()
