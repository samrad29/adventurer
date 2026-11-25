"""Issue an already-qualified award in the caller's transaction."""

from datetime import datetime

from psycopg.types.json import Jsonb

from .db import DB


class AchievementError(ValueError):
    """The award request cannot be applied."""


def issue_achievement(
    database: DB,
    *,
    user_id: int,
    definition_id: int,
    source: str,
    event_key: str,
    edition_id: int | None = None,
    repeat_key: str = "",
    event_at: datetime | None = None,
    route_id: int | None = None,
    expedition_id: int | None = None,
    activity_id: int | None = None,
    organization_id: int | None = None,
) -> dict:
    """Return the earned row, including on a retry of the same issuance.

    The trusted caller checks eligibility or grant authorization first. It owns
    commit and rollback and writes any progress before calling this function.
    """
    recipient = database.execute(
        "SELECT deletion_state FROM users WHERE id = ? FOR UPDATE", (user_id,)
    ).fetchone()
    if recipient is None or recipient["deletion_state"] is not None:
        raise AchievementError("Achievement recipient is unavailable.")

    existing = database.execute(
        """
        SELECT * FROM earned_achievements
        WHERE user_id = ? AND definition_id = ? AND edition_id IS NOT DISTINCT FROM ?
          AND source = ? AND event_key = ? AND repeat_key = ?
        """,
        (user_id, definition_id, edition_id, source, event_key, repeat_key),
    ).fetchone()
    if existing is not None:
        return existing

    # Resolve and lock contextual references before the edition lock so FK
    # checks cannot invert the lock order with a source deletion.
    context = {}
    references = (
        ("organization", organization_id, "SELECT slug, name FROM organizations WHERE id = ? FOR KEY SHARE"),
        ("route", route_id, "SELECT slug, name FROM routes WHERE id = ? FOR KEY SHARE"),
        ("expedition", expedition_id, "SELECT slug, name FROM expeditions WHERE id = ? FOR KEY SHARE"),
        ("activity", activity_id, """
            SELECT activity_type, activity_date::text AS activity_date
            FROM activities WHERE id = ? AND user_id = ? FOR KEY SHARE
        """),
    )
    for kind, reference_id, query in references:
        if reference_id is None:
            continue
        params = (reference_id, user_id) if kind == "activity" else (reference_id,)
        row = database.execute(query, params).fetchone()
        if row is None:
            raise AchievementError(f"Achievement {kind} context is unavailable.")
        context[kind] = dict(row)

    return database.execute(
        """
        INSERT INTO earned_achievements (
            user_id, definition_id, edition_id, source, event_key, repeat_key, event_at,
            route_id, expedition_id, activity_id, organization_id, context_snapshot
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING *
        """,
        (
            user_id, definition_id, edition_id, source, event_key, repeat_key, event_at,
            route_id, expedition_id, activity_id, organization_id, Jsonb(context),
        ),
    ).fetchone()


def delete_earned_achievements(database: DB, user_id: int) -> None:
    """Delete personal awards during the marked account deletion batch."""
    database.execute("DELETE FROM earned_achievements WHERE user_id = ?", (user_id,))
