"""Account records used by the JSON API and the cookie session."""

from __future__ import annotations

from datetime import datetime

from flask import g, session
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, is_unique_violation


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


def _viewer(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": _timestamp_text(row["created_at"]),
    }


def _timestamp_text(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)
