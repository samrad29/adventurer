"""Accounts, logs, and the leaderboard."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from flask import g, session
from werkzeug.security import check_password_hash, generate_password_hash

from .challenges import PLACE_LABELS
from .db import get_db
from .progress import view_model


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
    now = _now()
    try:
        cursor = get_db().execute(
            """
            INSERT INTO adventurers (name, name_key, password_hash, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (cleaned, cleaned.casefold(), generate_password_hash(password), now),
        )
        get_db().commit()
    except sqlite3.IntegrityError as exc:
        raise AccountError("That name is already on the mountain. Try logging in.") from exc
    return get_adventurer(cursor.lastrowid)


def authenticate(name: str, password: str) -> dict | None:
    key = " ".join((name or "").split()).casefold()
    if not key or not password:
        return None
    row = get_db().execute(
        "SELECT * FROM adventurers WHERE name_key = ?",
        (key,),
    ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return None
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"]}


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
        "SELECT id, name, created_at FROM adventurers WHERE id = ?",
        (adventurer_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def total_elevation(adventurer_id: int, challenge_slug: str) -> float:
    row = get_db().execute(
        """
        SELECT COALESCE(SUM(elevation_m), 0) AS total
        FROM logs
        WHERE adventurer_id = ? AND challenge_slug = ?
        """,
        (adventurer_id, challenge_slug),
    ).fetchone()
    return float(row["total"])


def add_log(adventurer_id: int, challenge_slug: str, entry: dict) -> float:
    get_db().execute(
        """
        INSERT INTO logs (
            adventurer_id, challenge_slug, elevation_m, distance_km,
            happened_on, note, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            adventurer_id,
            challenge_slug,
            entry["elevation_m"],
            entry["distance_km"],
            entry["happened_on"],
            entry["note"],
            _now(),
        ),
    )
    get_db().commit()
    return total_elevation(adventurer_id, challenge_slug)


def delete_log(adventurer_id: int, challenge_slug: str, log_id: int) -> bool:
    cursor = get_db().execute(
        """
        DELETE FROM logs
        WHERE id = ? AND adventurer_id = ? AND challenge_slug = ?
        """,
        (log_id, adventurer_id, challenge_slug),
    )
    get_db().commit()
    return cursor.rowcount == 1


def list_logs(adventurer_id: int, challenge_slug: str) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT id, elevation_m, distance_km, happened_on, note, created_at
        FROM logs
        WHERE adventurer_id = ? AND challenge_slug = ?
        ORDER BY happened_on DESC, id DESC
        """,
        (adventurer_id, challenge_slug),
    ).fetchall()
    return [dict(row) for row in rows]


def leaderboard(challenge: dict) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT a.id, a.name,
               COALESCE(SUM(l.elevation_m), 0) AS total_m,
               MAX(l.happened_on) AS last_on,
               MAX(l.created_at) AS last_logged
        FROM adventurers a
        JOIN logs l ON l.adventurer_id = a.id AND l.challenge_slug = ?
        GROUP BY a.id
        ORDER BY total_m DESC, last_logged ASC, a.name COLLATE NOCASE ASC
        """,
        (challenge["slug"],),
    ).fetchall()
    items = []
    for row in rows:
        model = view_model(challenge, row["total_m"])
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
                "total_m": float(row["total_m"]),
                "last_on": row["last_on"],
                "camp": camp,
                "percent": model["percent"],
            }
        )
    return items


def place_label(place: str) -> str:
    return PLACE_LABELS.get(place, place)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
