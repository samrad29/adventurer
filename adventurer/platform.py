"""Routes, expeditions, invitations, and the personal activity library."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .access import (
    can_add_credit,
    can_join,
    can_manage,
    can_owner_rejoin,
    can_read,
    is_owner,
)
from .challenges import get_challenge
from .db import get_db, is_unique_violation
from .measures import feet_number, feet_to_meters, kilometers_number, kilometers_to_meters
from .progress import view_model

MANUAL_EVENT_REASON = "Manual entry. Manual entries are not independently verified."

INVITE_DAYS = 30
RESERVED_SLUGS = {
    "join",
    "login",
    "logout",
    "api",
    "static",
    "favicon.ico",
    "health",
    "routes",
    "expeditions",
    "activities",
    "invitations",
    "i",
}


class PlatformError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def list_published_routes() -> list[dict]:
    rows = get_db().execute(
        """
        SELECT slug, name, region, summary, target_elevation_m
        FROM routes
        WHERE status = 'published'
        ORDER BY name, slug
        """
    ).fetchall()
    return [_route_summary(row) for row in rows]


def published_route(slug: str) -> dict:
    row = get_db().execute(
        """
        SELECT id, slug, name, region, summary, target_elevation_m, status
        FROM routes
        WHERE slug = ? AND status = 'published'
        """,
        (slug,),
    ).fetchone()
    if row is None:
        raise KeyError("Not found")
    checkpoints = _checkpoint_rows(row["id"])
    public = get_db().execute(
        """
        SELECT slug, name, description
        FROM expeditions
        WHERE route_id = ? AND visibility = 'public' AND status = 'active'
        ORDER BY name, slug
        """,
        (row["id"],),
    ).fetchall()
    story = _story(row["slug"])
    return {
        "slug": row["slug"],
        "name": row["name"],
        "region": row["region"],
        "summary": story["summary"] if story else row["summary"],
        "route": story["route"] if story else row["name"],
        "local_names": list(story["local_names"]) if story else [],
        "summit_ft": feet_number(row["target_elevation_m"]),
        "stops": _merge_stops(checkpoints, story),
        "expeditions": [dict(item) for item in public],
    }


def list_public_expeditions() -> list[dict]:
    rows = get_db().execute(
        """
        SELECT expedition.slug, expedition.name, expedition.description,
               route.slug AS route_slug, route.name AS route_name,
               route.region, route.target_elevation_m
        FROM expeditions AS expedition
        JOIN routes AS route ON route.id = expedition.route_id
        WHERE expedition.visibility = 'public'
          AND expedition.status = 'active'
          AND route.status = 'published'
        ORDER BY expedition.name, expedition.slug
        """
    ).fetchall()
    return [
        {
            "slug": row["slug"],
            "name": row["name"],
            "description": row["description"],
            "region": row["region"],
            "route_slug": row["route_slug"],
            "route_name": row["route_name"],
            "summit_ft": feet_number(row["target_elevation_m"]),
        }
        for row in rows
    ]


def present_expedition(slug: str, user_id: int | None) -> dict:
    expedition = _expedition_row(slug)
    if expedition is None:
        raise KeyError("Not found")
    participation = _participation(user_id, expedition["id"]) if user_id else None
    status = participation["status"] if participation else None
    if not can_read(expedition, user_id, status):
        raise KeyError("Not found")
    story = _story(expedition["route_slug"])
    stops = _merge_stops(_checkpoint_rows(expedition["route_id"]), story)
    return {
        "id": expedition["id"],
        "slug": expedition["slug"],
        "name": expedition["name"],
        "description": expedition["description"],
        "visibility": expedition["visibility"],
        "status": expedition["status"],
        "owner_user_id": expedition["owner_user_id"],
        "starts_at": expedition["starts_at"],
        "ends_at": expedition["ends_at"],
        "route_id": expedition["route_id"],
        "route_slug": expedition["route_slug"],
        "route_name": expedition["route_name"],
        "route_status": expedition["route_status"],
        "region": expedition["region"],
        "summary": story["summary"] if story else expedition["summary"],
        "route": story["route"] if story else expedition["route_name"],
        "local_names": list(story["local_names"]) if story else [],
        "summit_ft": feet_number(expedition["target_elevation_m"]),
        "stops": stops,
        "participation_status": status,
        "participation_id": participation["id"] if participation else None,
        "is_owner": is_owner(expedition, user_id),
        "can_join": user_id is not None and status != "active" and _may_join(expedition, user_id),
        "can_log": can_add_credit(expedition, status),
        "can_manage": can_manage(expedition, user_id),
    }


def expedition_total_m(user_id: int, expedition_slug: str) -> Decimal:
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
        (user_id, expedition_slug),
    ).fetchone()
    return Decimal(row["total_m"])


def leaderboard_rows(expedition: dict) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT account.id,
               profile.display_name AS name,
               COALESCE(SUM(credit.credited_elevation_m), 0) AS total_m,
               MAX(activity.activity_date) AS last_on,
               MAX(activity.created_at) AS last_logged
        FROM expedition_participations AS participation
        JOIN users AS account ON account.id = participation.user_id
        JOIN user_profiles AS profile ON profile.user_id = account.id
        LEFT JOIN activity_credits AS credit
            ON credit.participation_id = participation.id
           AND credit.user_id = participation.user_id
        LEFT JOIN activities AS activity ON activity.id = credit.activity_id
        WHERE participation.expedition_id = ?
          AND participation.status = 'active'
        GROUP BY account.id, profile.display_name
        ORDER BY COALESCE(SUM(credit.credited_elevation_m), 0) DESC,
                 MAX(activity.created_at) ASC NULLS LAST,
                 lower(profile.display_name) ASC
        """,
        (expedition["id"],),
    ).fetchall()
    items = []
    for row in rows:
        total_ft = feet_number(row["total_m"])
        model = view_model(expedition, total_ft, row["id"])
        if model["summited"]:
            camp = "Summit"
        elif model["current"] is None:
            camp = "Approach"
        else:
            camp = model["current"]["name"]
        last_on = row["last_on"]
        items.append(
            {
                "id": row["id"],
                "name": row["name"],
                "total_ft": float(total_ft),
                "last_on": None if last_on is None else _date_text(last_on),
                "camp": camp,
                "percent": model["percent"],
            }
        )
    return items


def creditable_expeditions(user_id: int) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT expedition.slug, expedition.name, expedition.status,
               expedition.visibility, expedition.owner_user_id,
               expedition.starts_at, expedition.ends_at,
               route.status AS route_status
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        JOIN routes AS route ON route.id = expedition.route_id
        WHERE participation.user_id = ? AND participation.status = 'active'
        ORDER BY expedition.name, expedition.slug
        """,
        (user_id,),
    ).fetchall()
    return [
        {"slug": row["slug"], "name": row["name"]}
        for row in rows
        if can_add_credit(row, "active")
    ]


def join_public(user_id: int, slug: str) -> dict:
    expedition = _expedition_row(slug)
    if expedition is None:
        raise KeyError("Not found")
    participation = _participation(user_id, expedition["id"])
    status = participation["status"] if participation else None
    if not can_read(expedition, user_id, status):
        raise KeyError("Not found")
    if not _may_join(expedition, user_id):
        raise PlatformError("This expedition is not open for new members.", 403)
    _activate_participation(user_id, expedition["id"])
    return present_expedition(slug, user_id)


def _may_join(expedition, user_id: int | None) -> bool:
    if can_join(expedition):
        return True
    return can_owner_rejoin(expedition, user_id)


def withdraw(user_id: int, slug: str) -> None:
    expedition = present_expedition(slug, user_id)
    if expedition["participation_status"] != "active":
        raise PlatformError("You are not an active member of this expedition.", 404)
    get_db().execute(
        """
        UPDATE expedition_participations
        SET status = 'withdrawn'
        WHERE id = ? AND user_id = ? AND status = 'active'
        """,
        (expedition["participation_id"], user_id),
    )
    get_db().commit()


def create_private_expedition(user_id: int, route_slug: str, name: str, description: str) -> str:
    cleaned = _clean_expedition_name(name)
    description = _clean_description(description)
    route = get_db().execute(
        """
        SELECT id FROM routes WHERE slug = ? AND status = 'published'
        """,
        (route_slug,),
    ).fetchone()
    if route is None:
        raise PlatformError("Choose a published route.", 400)
    slug = _unique_slug(cleaned)
    database = get_db()
    try:
        created = database.execute(
            """
            INSERT INTO expeditions (
                slug, name, description, route_id, visibility, status, owner_user_id
            )
            VALUES (?, ?, ?, ?, 'private', 'active', ?)
            RETURNING id
            """,
            (slug, cleaned, description, route["id"], user_id),
        ).fetchone()
        database.execute(
            """
            INSERT INTO expedition_participations (user_id, expedition_id, status)
            VALUES (?, ?, 'active')
            """,
            (user_id, created["id"]),
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    return slug


def update_expedition(
    user_id: int,
    slug: str,
    *,
    name: str | None = None,
    description: str | None = None,
    owner_username: str | None = None,
) -> None:
    expedition = _managed(slug, user_id)
    assignments = []
    params: list = []
    if name is not None:
        assignments.append("name = ?")
        params.append(_clean_expedition_name(name))
    if description is not None:
        assignments.append("description = ?")
        params.append(_clean_description(description))
    if owner_username is not None:
        owner = _active_member_id(expedition["id"], owner_username)
        assignments.append("owner_user_id = ?")
        params.append(owner)
    if not assignments:
        return
    params.extend([expedition["id"], user_id])
    get_db().execute(
        f"""
        UPDATE expeditions
        SET {", ".join(assignments)}
        WHERE id = ? AND owner_user_id = ?
        """,
        tuple(params),
    )
    get_db().commit()


def archive_expedition(user_id: int, slug: str) -> None:
    expedition = _managed(slug, user_id)
    get_db().execute(
        """
        UPDATE expeditions
        SET status = 'archived'
        WHERE id = ? AND owner_user_id = ?
        """,
        (expedition["id"], user_id),
    )
    get_db().commit()


def create_username_invitation(user_id: int, slug: str, username: str) -> dict:
    expedition = _managed(slug, user_id)
    if expedition["status"] != "active":
        raise PlatformError("This expedition is archived.", 400)
    invited = get_db().execute(
        """
        SELECT id, username FROM users WHERE username_key = ?
        """,
        (" ".join((username or "").split()).casefold(),),
    ).fetchone()
    if invited is None:
        raise PlatformError("No account uses that name.", 404)
    if invited["id"] == user_id:
        raise PlatformError("You already belong to this expedition.", 400)
    existing = get_db().execute(
        """
        SELECT id, status, expires_at
        FROM expedition_invitations
        WHERE expedition_id = ? AND invited_user_id = ? AND status = 'pending'
        """,
        (expedition["id"], invited["id"]),
    ).fetchone()
    if existing is not None:
        return _invitation_public(existing, invited["username"], link=False)
    _raw, digest = _new_token()
    expires = datetime.now(timezone.utc) + timedelta(days=INVITE_DAYS)
    try:
        created = get_db().execute(
            """
            INSERT INTO expedition_invitations (
                expedition_id, invited_by_user_id, invited_user_id,
                token_hash, status, expires_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?)
            RETURNING id, status, expires_at
            """,
            (expedition["id"], user_id, invited["id"], digest, expires),
        ).fetchone()
        get_db().commit()
    except Exception as exc:
        get_db().rollback()
        if not is_unique_violation(exc):
            raise
        existing = get_db().execute(
            """
            SELECT id, status, expires_at
            FROM expedition_invitations
            WHERE expedition_id = ? AND invited_user_id = ? AND status = 'pending'
            """,
            (expedition["id"], invited["id"]),
        ).fetchone()
        return _invitation_public(existing, invited["username"], link=False)
    return _invitation_public(created, invited["username"], link=False)


def create_share_link(user_id: int, slug: str) -> dict:
    expedition = _managed(slug, user_id)
    if expedition["status"] != "active":
        raise PlatformError("This expedition is archived.", 400)
    existing = get_db().execute(
        """
        SELECT id FROM expedition_invitations
        WHERE expedition_id = ? AND invited_user_id IS NULL AND status = 'pending'
        """,
        (expedition["id"],),
    ).fetchone()
    if existing is not None:
        raise PlatformError("Revoke the current link before creating another.", 409)
    raw, digest = _new_token()
    expires = datetime.now(timezone.utc) + timedelta(days=INVITE_DAYS)
    try:
        created = get_db().execute(
            """
            INSERT INTO expedition_invitations (
                expedition_id, invited_by_user_id, invited_user_id,
                token_hash, status, expires_at
            )
            VALUES (?, ?, NULL, ?, 'pending', ?)
            RETURNING id, expires_at
            """,
            (expedition["id"], user_id, digest, expires),
        ).fetchone()
        get_db().commit()
    except Exception as exc:
        get_db().rollback()
        if is_unique_violation(exc):
            raise PlatformError("Revoke the current link before creating another.", 409) from exc
        raise
    return {
        "id": created["id"],
        "expires_at": _timestamp_text(created["expires_at"]),
        "token": raw,
        "kind": "link",
    }


def list_invitations(user_id: int, slug: str) -> list[dict]:
    expedition = _managed(slug, user_id)
    rows = get_db().execute(
        """
        SELECT invitation.id, invitation.status, invitation.expires_at,
               invitation.invited_user_id, account.username
        FROM expedition_invitations AS invitation
        LEFT JOIN users AS account ON account.id = invitation.invited_user_id
        WHERE invitation.expedition_id = ? AND invitation.status = 'pending'
        ORDER BY invitation.id
        """,
        (expedition["id"],),
    ).fetchall()
    return [
        _invitation_public(row, row["username"], link=row["invited_user_id"] is None)
        for row in rows
    ]


def revoke_invitation(user_id: int, slug: str, invitation_id: int) -> None:
    expedition = _managed(slug, user_id)
    revoked = get_db().execute(
        """
        UPDATE expedition_invitations
        SET status = 'revoked'
        WHERE id = ? AND expedition_id = ? AND status = 'pending'
        RETURNING id
        """,
        (invitation_id, expedition["id"]),
    ).fetchone()
    get_db().commit()
    if revoked is None:
        raise PlatformError("That invitation is not pending.", 404)


def preview_invitation(raw_token: str) -> dict | None:
    row = _pending_token(raw_token)
    if row is None:
        return None
    return {
        "expedition_slug": row["slug"],
        "expedition_name": row["name"],
        "route_name": row["route_name"],
        "kind": "link" if row["invited_user_id"] is None else "username",
    }


def accept_token(user_id: int, raw_token: str) -> str:
    row = _pending_token(raw_token)
    if row is None:
        raise PlatformError("This invitation is not available.", 404)
    if row["invited_user_id"] is not None and row["invited_user_id"] != user_id:
        raise PlatformError("This invitation is not available.", 404)
    if row["status_expedition"] != "active":
        raise PlatformError("This invitation is not available.", 404)
    _activate_participation(user_id, row["expedition_id"])
    if row["invited_user_id"] is not None:
        get_db().execute(
            """
            UPDATE expedition_invitations
            SET status = 'accepted'
            WHERE id = ? AND status = 'pending'
            """,
            (row["id"],),
        )
        get_db().commit()
    return row["slug"]


def accept_username_invitation(user_id: int, invitation_id: int) -> str:
    row = get_db().execute(
        """
        SELECT invitation.id, invitation.expires_at, expedition.id AS expedition_id,
               expedition.slug, expedition.status AS status_expedition
        FROM expedition_invitations AS invitation
        JOIN expeditions AS expedition ON expedition.id = invitation.expedition_id
        WHERE invitation.id = ?
          AND invitation.invited_user_id = ?
          AND invitation.status = 'pending'
        """,
        (invitation_id, user_id),
    ).fetchone()
    if row is None or row["expires_at"] <= datetime.now(timezone.utc):
        raise PlatformError("This invitation is not available.", 404)
    if row["status_expedition"] != "active":
        raise PlatformError("This invitation is not available.", 404)
    _activate_participation(user_id, row["expedition_id"])
    get_db().execute(
        """
        UPDATE expedition_invitations
        SET status = 'accepted'
        WHERE id = ? AND status = 'pending'
        """,
        (row["id"],),
    )
    get_db().commit()
    return row["slug"]


def decline_token(user_id: int, raw_token: str) -> None:
    row = _pending_token(raw_token)
    if row is None or row["invited_user_id"] != user_id:
        raise PlatformError("This invitation is not available.", 404)
    get_db().execute(
        """
        UPDATE expedition_invitations
        SET status = 'declined'
        WHERE id = ? AND invited_user_id = ? AND status = 'pending'
        """,
        (row["id"], user_id),
    )
    get_db().commit()


def decline_username_invitation(user_id: int, invitation_id: int) -> None:
    declined = get_db().execute(
        """
        UPDATE expedition_invitations
        SET status = 'declined'
        WHERE id = ? AND invited_user_id = ? AND status = 'pending'
        RETURNING id
        """,
        (invitation_id, user_id),
    ).fetchone()
    get_db().commit()
    if declined is None:
        raise PlatformError("This invitation is not available.", 404)


def incoming_invitations(user_id: int) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT invitation.id, expedition.name AS expedition_name,
               route.name AS route_name, invitation.expires_at
        FROM expedition_invitations AS invitation
        JOIN expeditions AS expedition ON expedition.id = invitation.expedition_id
        JOIN routes AS route ON route.id = expedition.route_id
        WHERE invitation.invited_user_id = ?
          AND invitation.status = 'pending'
          AND invitation.expires_at > now()
        ORDER BY invitation.id
        """,
        (user_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "expedition_name": row["expedition_name"],
            "route_name": row["route_name"],
            "expires_at": _timestamp_text(row["expires_at"]),
        }
        for row in rows
    ]


def list_activities(user_id: int) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT activity.id, activity.activity_date, activity.elevation_gain_m,
               activity.distance_m, activity.note, activity.created_at,
               activity.source, activity.activity_type, activity.verification_status,
               COALESCE(
                   json_agg(
                       json_build_object(
                           'expedition_slug', expedition.slug,
                           'expedition_name', expedition.name,
                           'credited_elevation_m', credit.credited_elevation_m::text
                       )
                       ORDER BY expedition.slug
                   ) FILTER (WHERE credit.id IS NOT NULL),
                   '[]'::json
               ) AS credits
        FROM activities AS activity
        LEFT JOIN activity_credits AS credit
            ON credit.activity_id = activity.id
           AND credit.user_id = activity.user_id
        LEFT JOIN expedition_participations AS participation
            ON participation.id = credit.participation_id
           AND participation.user_id = credit.user_id
        LEFT JOIN expeditions AS expedition
            ON expedition.id = participation.expedition_id
        WHERE activity.user_id = ?
        GROUP BY activity.id
        ORDER BY activity.activity_date DESC, activity.id DESC
        """,
        (user_id,),
    ).fetchall()
    return [_activity_public(row) for row in rows]


def activity_for_user(user_id: int, activity_id: int) -> dict:
    rows = [row for row in list_activities(user_id) if row["id"] == activity_id]
    if not rows:
        raise KeyError("Not found")
    return rows[0]


def create_activity(user_id: int, entry: dict, expedition_slug: str | None = None) -> int:
    database = get_db()
    participation = None
    if expedition_slug:
        participation = _credit_target(user_id, expedition_slug)
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
            (user_id, entry["happened_on"], distance_m, elevation_m, entry["note"]),
        ).fetchone()
        database.execute(
            """
            INSERT INTO activity_verification_events (
                activity_id, from_status, to_status, reason, actor_type, actor_user_id
            )
            VALUES (?, NULL, 'not_required', ?, 'user', ?)
            """,
            (activity["id"], MANUAL_EVENT_REASON, user_id),
        )
        if participation is not None:
            _insert_full_credit(database, activity["id"], participation, user_id, elevation_m)
        database.commit()
    except Exception:
        database.rollback()
        raise
    return activity["id"]


def update_activity(user_id: int, activity_id: int, entry: dict) -> None:
    database = get_db()
    current = database.execute(
        """
        SELECT id, elevation_gain_m FROM activities WHERE id = ? AND user_id = ?
        """,
        (activity_id, user_id),
    ).fetchone()
    if current is None:
        raise KeyError("Not found")
    elevation_m = feet_to_meters(Decimal(str(entry["elevation_ft"])))
    distance_m = None
    if entry["distance_km"] is not None:
        distance_m = kilometers_to_meters(Decimal(str(entry["distance_km"])))
    try:
        database.execute(
            """
            UPDATE activities
            SET activity_date = ?, distance_m = ?, elevation_gain_m = ?, note = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                entry["happened_on"],
                distance_m,
                elevation_m,
                entry["note"],
                activity_id,
                user_id,
            ),
        )
        if elevation_m != current["elevation_gain_m"]:
            database.execute(
                """
                UPDATE activity_credits
                SET credited_elevation_m = ?
                WHERE activity_id = ? AND user_id = ?
                """,
                (elevation_m, activity_id, user_id),
            )
        database.commit()
    except Exception:
        database.rollback()
        raise


def delete_activity(user_id: int, activity_id: int) -> bool:
    deleted = get_db().execute(
        "DELETE FROM activities WHERE id = ? AND user_id = ? RETURNING id",
        (activity_id, user_id),
    ).fetchone()
    get_db().commit()
    return deleted is not None


def add_full_credit(user_id: int, activity_id: int, expedition_slug: str) -> None:
    database = get_db()
    activity = database.execute(
        """
        SELECT id, elevation_gain_m FROM activities WHERE id = ? AND user_id = ?
        """,
        (activity_id, user_id),
    ).fetchone()
    if activity is None:
        raise KeyError("Not found")
    elevation_m = activity["elevation_gain_m"]
    if elevation_m is None or Decimal(elevation_m) <= 0:
        raise PlatformError("This outing has no elevation to count.", 400)
    participation = _credit_target(user_id, expedition_slug)
    try:
        _insert_full_credit(
            database, activity["id"], participation, user_id, elevation_m
        )
        database.commit()
    except Exception as exc:
        database.rollback()
        if is_unique_violation(exc):
            raise PlatformError("That outing is already counted on this expedition.", 409) from exc
        raise


def remove_credit(user_id: int, activity_id: int, expedition_slug: str) -> bool:
    deleted = get_db().execute(
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
        (activity_id, user_id, expedition_slug),
    ).fetchone()
    get_db().commit()
    return deleted is not None


def journal_entries(user_id: int, expedition_slug: str) -> list[dict]:
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
        (user_id, expedition_slug),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "elevation_ft": feet_number(row["credited_elevation_m"]),
            "distance_km": None if row["distance_m"] is None else kilometers_number(row["distance_m"]),
            "happened_on": _date_text(row["activity_date"]),
            "note": row["note"],
            "created_at": _timestamp_text(row["created_at"]),
        }
        for row in rows
    ]


def _expedition_row(slug: str):
    return get_db().execute(
        """
        SELECT expedition.id, expedition.slug, expedition.name, expedition.description,
               expedition.visibility, expedition.status, expedition.owner_user_id,
               expedition.starts_at, expedition.ends_at, expedition.route_id,
               route.slug AS route_slug, route.name AS route_name,
               route.region, route.summary, route.status AS route_status,
               route.target_elevation_m
        FROM expeditions AS expedition
        JOIN routes AS route ON route.id = expedition.route_id
        WHERE expedition.slug = ?
        """,
        (slug,),
    ).fetchone()


def _required_expedition(slug: str):
    row = _expedition_row(slug)
    if row is None:
        raise KeyError("Not found")
    return row


def _participation(user_id: int, expedition_id: int):
    return get_db().execute(
        """
        SELECT id, status FROM expedition_participations
        WHERE user_id = ? AND expedition_id = ?
        """,
        (user_id, expedition_id),
    ).fetchone()


def _activate_participation(user_id: int, expedition_id: int) -> None:
    database = get_db()
    existing = _participation(user_id, expedition_id)
    if existing is None:
        database.execute(
            """
            INSERT INTO expedition_participations (user_id, expedition_id, status)
            VALUES (?, ?, 'active')
            """,
            (user_id, expedition_id),
        )
    elif existing["status"] != "active":
        database.execute(
            """
            UPDATE expedition_participations
            SET status = 'active'
            WHERE id = ? AND user_id = ?
            """,
            (existing["id"], user_id),
        )
    database.commit()


def _credit_target(user_id: int, slug: str):
    expedition = _expedition_row(slug)
    if expedition is None:
        raise KeyError("Not found")
    participation = _participation(user_id, expedition["id"])
    status = participation["status"] if participation else None
    if not can_read(expedition, user_id, status):
        raise KeyError("Not found")
    if not can_add_credit(expedition, status):
        raise PlatformError("You cannot add elevation to this expedition now.", 403)
    return participation


def _insert_full_credit(database, activity_id, participation, user_id, elevation_m) -> None:
    database.execute(
        """
        INSERT INTO activity_credits (
            activity_id, participation_id, user_id, credited_elevation_m
        )
        VALUES (?, ?, ?, ?)
        """,
        (activity_id, participation["id"], user_id, elevation_m),
    )


def _managed(slug: str, user_id: int) -> dict:
    try:
        expedition = present_expedition(slug, user_id)
    except KeyError:
        raise KeyError("Not found") from None
    if not expedition["can_manage"]:
        if expedition["visibility"] == "private":
            raise KeyError("Not found")
        raise PlatformError("This expedition is not yours to change.", 403)
    return expedition


def _active_member_id(expedition_id: int, username: str) -> int:
    row = get_db().execute(
        """
        SELECT account.id
        FROM users AS account
        JOIN expedition_participations AS participation
            ON participation.user_id = account.id
        WHERE account.username_key = ?
          AND participation.expedition_id = ?
          AND participation.status = 'active'
        """,
        (" ".join((username or "").split()).casefold(), expedition_id),
    ).fetchone()
    if row is None:
        raise PlatformError("That person is not an active member.", 400)
    return row["id"]


def _pending_token(raw_token: str):
    if not raw_token:
        return None
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return get_db().execute(
        """
        SELECT invitation.id, invitation.invited_user_id, invitation.expires_at,
               invitation.expedition_id, expedition.slug, expedition.name,
               expedition.status AS status_expedition, route.name AS route_name
        FROM expedition_invitations AS invitation
        JOIN expeditions AS expedition ON expedition.id = invitation.expedition_id
        JOIN routes AS route ON route.id = expedition.route_id
        WHERE invitation.token_hash = ?
          AND invitation.status = 'pending'
          AND invitation.expires_at > now()
        """,
        (digest,),
    ).fetchone()


def _checkpoint_rows(route_id: int):
    return get_db().execute(
        """
        SELECT slug, name, position, elevation_m
        FROM route_checkpoints
        WHERE route_id = ?
        ORDER BY position
        """,
        (route_id,),
    ).fetchall()


def _story(route_slug: str):
    try:
        return get_challenge(route_slug)
    except KeyError:
        return None


def _merge_stops(checkpoints, story) -> list[dict]:
    by_slug = {stop["slug"]: stop for stop in story["stops"]} if story else {}
    stops = []
    for checkpoint in checkpoints:
        narrative = by_slug.get(checkpoint["slug"], {})
        stops.append(
            {
                "slug": checkpoint["slug"],
                "name": checkpoint["name"],
                "elevation_ft": feet_number(checkpoint["elevation_m"]),
                "place": narrative.get("place", "checkpoint"),
                "summary": narrative.get("summary", ""),
                "unlocks": narrative.get("unlocks", []),
                "names": narrative.get("names"),
            }
        )
    return stops


def _route_summary(row) -> dict:
    return {
        "slug": row["slug"],
        "name": row["name"],
        "region": row["region"],
        "summary": row["summary"],
        "summit_ft": feet_number(row["target_elevation_m"]),
    }


def _activity_public(row) -> dict:
    credits = row["credits"] or []
    if isinstance(credits, str):
        credits = json.loads(credits)
    return {
        "id": row["id"],
        "activity_date": _date_text(row["activity_date"]),
        "happened_on": _date_text(row["activity_date"]),
        "elevation_ft": feet_number(row["elevation_gain_m"]),
        "distance_km": None if row["distance_m"] is None else kilometers_number(row["distance_m"]),
        "note": row["note"],
        "created_at": _timestamp_text(row["created_at"]),
        "source": row["source"],
        "activity_type": row["activity_type"],
        "verification_status": row["verification_status"],
        "credits": [
            {
                "expedition_slug": item["expedition_slug"],
                "expedition_name": item["expedition_name"],
                "elevation_ft": feet_number(item["credited_elevation_m"]),
            }
            for item in credits
        ],
    }


def _invitation_public(row, username, *, link: bool) -> dict:
    return {
        "id": row["id"],
        "status": row.get("status", "pending"),
        "expires_at": _timestamp_text(row["expires_at"]),
        "username": None if link else username,
        "kind": "link" if link else "username",
    }


def _clean_expedition_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not 2 <= len(cleaned) <= 80:
        raise PlatformError("Use an expedition name of 2 to 80 characters.", 400)
    return cleaned


def _clean_description(description: str) -> str:
    cleaned = " ".join((description or "").split())
    if len(cleaned) > 500:
        raise PlatformError("Keep the description under 500 characters.", 400)
    return cleaned


def _unique_slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:48] or "expedition"
    if base in RESERVED_SLUGS:
        base = f"{base}-expedition"
    slug = base
    number = 2
    while _slug_taken(slug):
        suffix = f"-{number}"
        slug = f"{base[: 48 - len(suffix)]}{suffix}"
        number += 1
    return slug


def _slug_taken(slug: str) -> bool:
    if slug in RESERVED_SLUGS:
        return True
    return (
        get_db().execute("SELECT 1 AS found FROM expeditions WHERE slug = ?", (slug,)).fetchone()
        is not None
    )


def _new_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw, digest


def _date_text(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _timestamp_text(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)
