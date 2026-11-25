"""Dispatch committed candidate completions in the progress writer's transaction."""

from dataclasses import dataclass
from datetime import datetime

from psycopg.errors import CheckViolation

from .trophy_case import award_summary


@dataclass(frozen=True)
class RouteCompleted:
    event_key: str
    user_id: int
    route_key: int
    route_id: int | None
    completed_at: datetime
    completion_number: int


def _route_completion(event, route_key, criteria):
    return route_key == event.route_key and criteria.get(
        "completion_number", event.completion_number
    ) == event.completion_number


_EVALUATORS = {("route_completed", "route_completion", 1): _route_completion}


def finalize_route_completion(database, journey_id):
    """Record and evaluate a new finish after progress and feed writes."""
    event_key = f"route_completed:{journey_id}"
    row = database.execute(
        """
        INSERT INTO route_completions (
            event_key, user_id, journey_id, route_key, route_id, route_slug, route_title,
            completed_at, completion_number
        )
        SELECT ?, journey.user_id, journey.id, route.id, route.id, route.slug, route.name,
               journey.completed_at, COALESCE(history.last_number, 0) + 1
        FROM route_journeys AS journey
        JOIN routes AS route ON route.id = journey.route_id
        CROSS JOIN LATERAL (
            SELECT max(completion_number) AS last_number FROM route_completions
            WHERE user_id = journey.user_id AND route_key = route.id
        ) AS history
        WHERE journey.id = ? AND journey.status = 'completed'
        ON CONFLICT (event_key) DO NOTHING
        RETURNING event_key, user_id, route_key, route_id, completed_at, completion_number
        """,
        (event_key, journey_id),
    ).fetchone()
    if row is None:
        if not database.execute(
            "SELECT 1 FROM route_completions WHERE event_key = ?", (event_key,)
        ).fetchone():
            raise RuntimeError("Completion finalizer requires a completed journey.")
        return []
    return _dispatch(database, RouteCompleted(**row))


def _dispatch(database, event):
    definitions = database.execute(
        """
        SELECT id, evaluator_key, evaluator_version, evaluator_route_id, criteria, issuance_rule
        FROM achievement_definitions
        WHERE status = 'active' AND evaluator_route_id = ?
        ORDER BY id FOR SHARE
        """,
        (event.route_key,),
    ).fetchall()
    awards = []
    for definition in definitions:
        evaluator = _EVALUATORS[("route_completed", definition["evaluator_key"], definition["evaluator_version"])]
        if not evaluator(event, definition["evaluator_route_id"], definition["criteria"]):
            continue
        rule = definition["issuance_rule"]
        if rule == "once_per_account_per_edition":
            editions = database.execute(
                """
                SELECT id FROM achievement_editions WHERE definition_id = ?
                ORDER BY id FOR NO KEY UPDATE
                """,
                (definition["id"],),
            ).fetchall()
            cohorts = [(edition["id"], f"edition:{edition['id']}") for edition in editions]
        else:
            cohorts = [(None, "account" if rule == "once_per_account" else event.event_key)]
        for edition_id, key in cohorts:
            award = _issue(database, event, definition["id"], edition_id, key)
            if award is not None:
                awards.append(award)
    return awards


def _issue(database, event, definition_id, edition_id, key):
    if database.execute(
        """
        SELECT 1 FROM earned_achievements
        WHERE user_id = ? AND definition_id = ? AND deduplication_key = ?
        """,
        (event.user_id, definition_id, key),
    ).fetchone():
        return None
    try:
        # The existing issuance trigger checks the window at the instant of issue.
        with database.raw.transaction():
            award = database.execute(
                """
                INSERT INTO earned_achievements (
                    user_id, definition_id, edition_id, source, event_key, event_at,
                    route_id, completion_event_key
                )
                VALUES (?, ?, ?, 'route_completion', ?, ?, ?, ?)
                RETURNING *
                """,
                (event.user_id, definition_id, edition_id, event.event_key,
                 event.completed_at, event.route_id, event.event_key),
            ).fetchone()
    except CheckViolation as error:
        if edition_id is not None and error.diag.message_primary in {
            "achievement edition is exhausted",
            "achievement edition is outside its issuance window",
        }:
            return None
        raise
    database.execute(
        "INSERT INTO achievement_reveals (earned_achievement_id) VALUES (?)", (award["id"],)
    )
    return award_summary(award, reveal_pending=True)
