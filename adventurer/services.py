"""Accounts, activities, and the casual expedition leaderboard."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from flask import g, session
from werkzeug.security import check_password_hash, generate_password_hash

from .challenges import FEATURED_SLUG, PLACE_LABELS, get_challenge
from .db import get_db, is_unique_violation
from .measures import (
    feet_number,
    feet_to_meters,
    kilometers_number,
    kilometers_to_meters,
)
from .progress import view_model

MANUAL_EVENT_REASON = "Manual entry. Manual entries are not independently verified."


class AccountError(ValueError):
    pass


def clean_name(name: str) -> str | None:
    cleaned = " ".join((name or "").split())
    if not 2 <= len(cleaned) <= 24:
        return None
    if not cleaned[0].isalpha():
        return None
    if not all(char.isalpha() or char.isdigit() or char in " .'-" for char in cleaned):
        return None
    return cleaned


def create_adventurer(name: str, password: str) -> dict:
    cleaned = clean_name(name)
    if cleaned is None:
        raise AccountError(
            "Use 2 to 24 letters. Spaces, apostrophes, and hyphens are fine."
        )
    if not 6 <= len(password) <= 128:
        raise AccountError("Use a password of at least 6 characters.")
    database = get_db()
    try:
        cursor = database.execute(
            """
            INSERT INTO users (username, username_key, password_hash)
            VALUES (?, ?, ?)
            RETURNING id
            """,
            (cleaned, cleaned.casefold(), generate_password_hash(password)),
        )
        new_id = cursor.fetchone()["id"]
        database.execute(
            """
            INSERT INTO user_profiles (user_id, display_name, unit_system)
            VALUES (?, ?, 'imperial')
            """,
            (new_id, cleaned),
        )
        enrolled = database.execute(
            """
            INSERT INTO expedition_participations (user_id, expedition_id, status)
            SELECT ?, id, 'active'
            FROM expeditions
            WHERE slug = ?
            RETURNING id
            """,
            (new_id, FEATURED_SLUG),
        ).fetchone()
        if enrolled is None:
            raise AccountError("The featured expedition is not available.")
        database.commit()
    except AccountError:
        database.rollback()
        raise
    except Exception as exc:
        database.rollback()
        if not is_unique_violation(exc):
            raise
        raise AccountError("That name is already on the mountain. Try logging in.") from exc
    return get_adventurer(new_id)


def authenticate(name: str, password: str) -> dict | None:
    key = " ".join((name or "").split()).casefold()
    if not key or not password:
        return None
    row = get_db().execute(
        """
        SELECT users.id, users.password_hash, users.created_at,
               profiles.display_name AS name
        FROM users
        JOIN user_profiles AS profiles ON profiles.user_id = users.id
        WHERE users.username_key = ?
        """,
        (key,),
    ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return None
    return _viewer(row)


def current_viewer() -> dict | None:
    if "viewer" in g:
        return g.viewer
    adventurer_id = session.get("adventurer_id")
    g.viewer = get_adventurer(adventurer_id) if adventurer_id else None
    if adventurer_id and g.viewer is None:
        session.clear()
    return g.viewer


def get_adventurer(adventurer_id: int) -> dict | None:
    row = get_db().execute(
        """
        SELECT users.id, users.created_at, profiles.display_name AS name
        FROM users
        JOIN user_profiles AS profiles ON profiles.user_id = users.id
        WHERE users.id = ?
        """,
        (adventurer_id,),
    ).fetchone()
    if row is None:
        return None
    return _viewer(row)


def load_route(slug: str) -> dict:
    """Checkpoint structure from the database, narrative from challenges.py."""
    cached = getattr(g, "routes", None)
    if cached is not None and slug in cached:
        return cached[slug]
    route = _assemble_route(slug)
    if cached is None:
        g.routes = {}
    g.routes[slug] = route
    return route


def list_routes() -> list[dict]:
    rows = get_db().execute(
        """
        SELECT slug, name, target_elevation_m
        FROM expeditions
        WHERE status <> 'retired'
        ORDER BY id
        """
    ).fetchall()
    summaries = []
    for row in rows:
        try:
            region = get_challenge(row["slug"])["region"]
        except KeyError:
            region = ""
        summaries.append(
            {
                "slug": row["slug"],
                "name": row["name"],
                "region": region,
                "summit_ft": feet_number(row["target_elevation_m"]),
            }
        )
    return summaries


def total_elevation(adventurer_id: int, challenge_slug: str) -> float:
    row = get_db().execute(
        """
        SELECT COALESCE(SUM(credit.credited_elevation_m), 0) AS total_m
        FROM activity_credits AS credit
        JOIN expedition_participations AS participation
            ON participation.id = credit.participation_id
           AND participation.user_id = credit.user_id
        JOIN expeditions AS expedition
            ON expedition.id = participation.expedition_id
        WHERE credit.user_id = ? AND expedition.slug = ?
        """,
        (adventurer_id, challenge_slug),
    ).fetchone()
    return float(feet_number(row["total_m"]))


def add_log(adventurer_id: int, challenge_slug: str, entry: dict) -> float:
    database = get_db()
    participation = database.execute(
        """
        SELECT participation.id
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition
            ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ?
          AND participation.status = 'active'
          AND expedition.slug = ?
        """,
        (adventurer_id, challenge_slug),
    ).fetchone()
    if participation is None:
        raise AccountError("You are not on this expedition.")
    elevation_m = feet_to_meters(Decimal(str(entry["elevation_ft"])))
    distance_m = None
    if entry["distance_km"] is not None:
        distance_m = kilometers_to_meters(Decimal(str(entry["distance_km"])))
    try:
        activity = database.execute(
            """
            INSERT INTO activities (
                user_id, activity_type, activity_date,
                started_at, ended_at, duration_s,
                distance_m, elevation_gain_m,
                source, external_id, source_metadata,
                verification_status, note
            )
            VALUES (
                ?, 'unspecified', ?,
                NULL, NULL, NULL,
                ?, ?,
                'manual', NULL, '{}'::jsonb,
                'not_required', ?
            )
            RETURNING id
            """,
            (
                adventurer_id,
                entry["happened_on"],
                distance_m,
                elevation_m,
                entry["note"],
            ),
        ).fetchone()
        database.execute(
            """
            INSERT INTO activity_credits (
                activity_id, participation_id, user_id, credited_elevation_m
            )
            VALUES (?, ?, ?, ?)
            """,
            (activity["id"], participation["id"], adventurer_id, elevation_m),
        )
        database.execute(
            """
            INSERT INTO activity_verification_events (
                activity_id, from_status, to_status, reason, actor_type, actor_user_id
            )
            VALUES (?, NULL, 'not_required', ?, 'user', ?)
            """,
            (activity["id"], MANUAL_EVENT_REASON, adventurer_id),
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    return total_elevation(adventurer_id, challenge_slug)


def delete_log(adventurer_id: int, challenge_slug: str, log_id: int) -> bool:
    """Remove this expedition's credit. The activity row stays."""
    database = get_db()
    deleted = database.execute(
        """
        DELETE FROM activity_credits AS credit
        USING expedition_participations AS participation,
              expeditions AS expedition
        WHERE credit.activity_id = ?
          AND credit.user_id = ?
          AND participation.id = credit.participation_id
          AND participation.user_id = credit.user_id
          AND expedition.id = participation.expedition_id
          AND expedition.slug = ?
        RETURNING credit.id
        """,
        (log_id, adventurer_id, challenge_slug),
    ).fetchone()
    database.commit()
    return deleted is not None


def list_logs(adventurer_id: int, challenge_slug: str) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT activity.id,
               credit.credited_elevation_m,
               activity.distance_m,
               activity.activity_date,
               activity.note,
               activity.created_at
        FROM activity_credits AS credit
        JOIN activities AS activity
            ON activity.id = credit.activity_id
           AND activity.user_id = credit.user_id
        JOIN expedition_participations AS participation
            ON participation.id = credit.participation_id
           AND participation.user_id = credit.user_id
        JOIN expeditions AS expedition
            ON expedition.id = participation.expedition_id
        WHERE credit.user_id = ? AND expedition.slug = ?
        ORDER BY activity.activity_date DESC, activity.id DESC
        """,
        (adventurer_id, challenge_slug),
    ).fetchall()
    return [_journal_entry(row) for row in rows]


def leaderboard(challenge: dict) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT account.id,
               profile.display_name AS name,
               SUM(credit.credited_elevation_m) AS total_m,
               MAX(activity.activity_date) AS last_on,
               MAX(activity.created_at) AS last_logged
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition
            ON expedition.id = participation.expedition_id
        JOIN users AS account ON account.id = participation.user_id
        JOIN user_profiles AS profile ON profile.user_id = account.id
        JOIN activity_credits AS credit
            ON credit.participation_id = participation.id
           AND credit.user_id = participation.user_id
        JOIN activities AS activity ON activity.id = credit.activity_id
        WHERE expedition.slug = ?
        GROUP BY account.id, profile.display_name
        ORDER BY SUM(credit.credited_elevation_m) DESC,
                 MAX(activity.created_at) ASC,
                 lower(profile.display_name) ASC
        """,
        (challenge["slug"],),
    ).fetchall()
    items = []
    for row in rows:
        total_ft = feet_number(row["total_m"])
        model = view_model(challenge, total_ft, row["id"])
        if model["summited"]:
            camp = "Summit"
        elif model["current"] is None:
            camp = "Approach"
        else:
            camp = model["current"]["name"]
        items.append(
            {
                "id": row["id"],
                "name": row["name"],
                "total_ft": float(total_ft),
                "last_on": _date_text(row["last_on"]),
                "camp": camp,
                "percent": model["percent"],
            }
        )
    return items


def place_label(place: str) -> str:
    return PLACE_LABELS.get(place, place)


def _assemble_route(slug: str) -> dict:
    narrative = get_challenge(slug)
    expedition = get_db().execute(
        """
        SELECT id, slug, name, target_elevation_m
        FROM expeditions
        WHERE slug = ?
        """,
        (slug,),
    ).fetchone()
    if expedition is None:
        raise KeyError(slug)
    checkpoints = get_db().execute(
        """
        SELECT slug, name, position, elevation_m
        FROM expedition_checkpoints
        WHERE expedition_id = ?
        ORDER BY position
        """,
        (expedition["id"],),
    ).fetchall()
    story_by_slug = {stop["slug"]: stop for stop in narrative["stops"]}
    stops = []
    for checkpoint in checkpoints:
        story = story_by_slug.get(checkpoint["slug"], {})
        stops.append(
            {
                "slug": checkpoint["slug"],
                "name": checkpoint["name"],
                "elevation_ft": feet_number(checkpoint["elevation_m"]),
                "place": story.get("place", "checkpoint"),
                "summary": story.get("summary", ""),
                "unlocks": story.get("unlocks", []),
                "names": story.get("names"),
            }
        )
    return {
        "slug": expedition["slug"],
        "name": expedition["name"],
        "local_names": list(narrative["local_names"]),
        "region": narrative["region"],
        "route": narrative["route"],
        "summit_ft": feet_number(expedition["target_elevation_m"]),
        "summary": narrative["summary"],
        "stops": stops,
    }


def _journal_entry(row: dict) -> dict:
    distance = row["distance_m"]
    return {
        "id": row["id"],
        "elevation_ft": feet_number(row["credited_elevation_m"]),
        "distance_km": None if distance is None else kilometers_number(distance),
        "happened_on": _date_text(row["activity_date"]),
        "note": row["note"],
        "created_at": _timestamp_text(row["created_at"]),
    }


def _viewer(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": _timestamp_text(row["created_at"]),
    }


def _date_text(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _timestamp_text(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)
