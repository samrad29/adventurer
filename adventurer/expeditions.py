"""Expedition, invitation, and organization services.

Journey activation and activity credits stay in their own modules. This
module locks in the architecture order: users, expeditions, journeys,
participations, then feed rows and result rows. A new expedition row is
inserted after journey activation because it does not exist early enough
to lock; its new id sorts after the active expeditions already locked.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timezone

from flask import current_app

from .db import get_db
from .journeys import JourneyError, activate_for_participation

INVITE_DAYS = 30
MISSED = "That expedition does not exist."
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_RESERVED = {
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
    "organizations",
    "i",
}
_CLOSE_ATTEMPTS = 3


class ExpeditionError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **details) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


class _Retry(Exception):
    pass


def translate_database_error(exc: BaseException) -> ExpeditionError | None:
    sqlstate = getattr(exc, "sqlstate", None)
    message = str(exc)
    if sqlstate == "23514" and "one active organization expedition" in message:
        return ExpeditionError(
            "slot_occupied",
            "You already have an active organization expedition.",
            status=409,
        )
    if sqlstate == "23505":
        return ExpeditionError(
            "conflict",
            "That record changed during this update. Try again.",
            status=409,
        )
    return None


def create_private_expedition(
    user_id: int,
    *,
    slug: str,
    name: str,
    description: str = "",
    route_slug: str,
    confirm_new_attempt: bool = False,
) -> dict:
    cleaned_slug = _clean_slug(slug)
    cleaned_name = _clean_name(name)
    cleaned_description = _clean_description(description)

    def write(database):
        _lock_user(database, user_id)
        route = _published_route(database, route_slug)
        _lock_route_expeditions(database, route["id"])
        journey = _activate(database, user_id, route["id"], confirm_new_attempt)
        row = database.execute(
            """
            INSERT INTO expeditions (
                slug, name, description, route_id, visibility, owner_user_id,
                kind, status
            )
            VALUES (?, ?, ?, ?, 'private', ?, 'private', 'active')
            RETURNING id
            """,
            (cleaned_slug, cleaned_name, cleaned_description, route["id"], user_id),
        ).fetchone()
        participation = _save_participation(
            database,
            user_id,
            _locked_expedition(database, row["id"]),
            journey["id"],
        )
        return _joined(database, row["id"], participation, journey), True

    return _commit(write)


def join_expedition(user_id: int, slug: str, *, confirm_new_attempt: bool = False) -> dict:
    complete_due_expeditions()

    def write(database):
        _lock_user(database, user_id)
        expedition = _reload_for_join(database, user_id, slug)
        journey = _activate(
            database, user_id, expedition["route_id"], confirm_new_attempt
        )
        _require_slot(database, user_id, expedition)
        participation = _save_participation(
            database, user_id, expedition, journey["id"]
        )
        return _joined(database, expedition["id"], participation, journey), True

    return _commit(write)


def leave_expedition(user_id: int, slug: str) -> dict:
    def write(database):
        _lock_user(database, user_id)
        expedition = _find_slug(database, slug)
        if expedition is None:
            raise ExpeditionError("not_found", MISSED, status=404)
        preview = _participation_row(database, user_id, expedition["id"])
        if preview is None:
            if _access(database, user_id, expedition) == "none":
                raise ExpeditionError("not_found", MISSED, status=404)
            raise ExpeditionError("invalid_state", "You are not on this expedition.", status=409)
        locked = _lock_expedition(database, expedition["id"])
        preview = _participation_row(database, user_id, locked["id"])
        if preview is None:
            raise ExpeditionError("invalid_state", "You are not on this expedition.", status=409)
        _lock_ids(database, "route_journeys", [preview["journey_id"]])
        participation = _locked_participation(database, user_id, locked["id"])
        journey = database.execute(
            "SELECT status FROM route_journeys WHERE id = ?",
            (participation["journey_id"],),
        ).fetchone()
        finished = database.execute(
            """
            SELECT 1 AS found
            FROM route_journeys
            WHERE user_id = ? AND route_id = ? AND status = 'completed'
            LIMIT 1
            """,
            (user_id, locked["route_id"]),
        ).fetchone()
        database.execute(
            """
            UPDATE expedition_participations
            SET status = 'withdrawn',
                final_standings_eligible = ?,
                retained_read_access = ?
            WHERE id = ?
            """,
            (
                journey is not None and journey["status"] == "completed",
                finished is not None,
                participation["id"],
            ),
        )
        return _participation(database, participation["id"]), True

    return _commit(write)


def transfer_ownership(user_id: int, slug: str, new_owner_id: int) -> dict:
    def write(database):
        if user_id == new_owner_id:
            raise ExpeditionError("invalid", "Choose another active participant.")
        expedition = _find_slug(database, slug)
        if expedition is None or expedition["kind"] != "private":
            raise ExpeditionError("not_found", MISSED, status=404)
        _lock_users(database, [user_id, new_owner_id])
        locked = _lock_expedition(database, expedition["id"])
        if locked["owner_user_id"] != user_id or locked["status"] != "active":
            raise _manage_denied(database, user_id, locked)
        participant = database.execute(
            """
            SELECT id
            FROM expedition_participations
            WHERE expedition_id = ? AND user_id = ? AND status = 'active'
            FOR UPDATE
            """,
            (locked["id"], new_owner_id),
        ).fetchone()
        if participant is None:
            raise ExpeditionError(
                "invalid_state",
                "Ownership can move only to an active participant.",
                status=409,
            )
        database.execute(
            """
            UPDATE expeditions
            SET owner_user_id = ?
            WHERE id = ? AND kind = 'private' AND status = 'active'
            """,
            (new_owner_id, locked["id"]),
        )
        return _profile(database, user_id, _lock_expedition(database, locked["id"])), True

    return _commit(write)


def update_private_expedition(
    user_id: int,
    slug: str,
    *,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    cleaned_name = None if name is None else _clean_name(name)
    cleaned_description = None if description is None else _clean_description(description)

    def write(database):
        _lock_user(database, user_id)
        expedition = _managed_private(database, user_id, slug)
        database.execute(
            """
            UPDATE expeditions
            SET name = COALESCE(?, name),
                description = COALESCE(?, description)
            WHERE id = ?
            """,
            (cleaned_name, cleaned_description, expedition["id"]),
        )
        return _profile(database, user_id, _lock_expedition(database, expedition["id"])), True

    return _commit(write)


def archive_private_expedition(user_id: int, slug: str) -> dict:
    def write(database):
        expedition = _find_slug(database, slug)
        if expedition is None or expedition["kind"] != "private":
            raise ExpeditionError("not_found", MISSED, status=404)
        if expedition["owner_user_id"] != user_id:
            raise _manage_denied(database, user_id, expedition)
        return _close(
            database,
            expedition["id"],
            actor_id=user_id,
            terminal="archived",
        )

    return _commit(write)


def create_username_invitation(user_id: int, slug: str, username: str) -> dict:
    def write(database):
        _lock_user(database, user_id)
        expedition = _inviter_expedition(database, user_id, slug)
        invited = _user_by_name(database, username)
        if invited["id"] == user_id:
            raise ExpeditionError("invalid", "You cannot invite yourself.")
        _expire_username(database, expedition["id"], invited["id"])
        existing = database.execute(
            """
            SELECT id, status, expires_at, invited_user_id, expedition_id, token_hash
            FROM expedition_invitations
            WHERE expedition_id = ? AND invited_user_id = ? AND status = 'pending'
            """,
            (expedition["id"], invited["id"]),
        ).fetchone()
        if existing is not None:
            return _invitation_body(database, existing, token=None), False
        raw, digest = _new_token()
        row = database.execute(
            """
            INSERT INTO expedition_invitations (
                expedition_id, invited_by_user_id, invited_user_id,
                token_hash, status, expires_at
            )
            VALUES (?, ?, ?, ?, 'pending', now() + interval '30 days')
            RETURNING id
            """,
            (expedition["id"], user_id, invited["id"], digest),
        ).fetchone()
        stored = _invitation_row(database, row["id"])
        return _invitation_body(database, stored, token=raw), True

    return _commit(write)


def create_share_link(user_id: int, slug: str) -> dict:
    def write(database):
        _lock_user(database, user_id)
        expedition = _inviter_expedition(database, user_id, slug)
        _expire_share_link(database, expedition["id"])
        existing = database.execute(
            """
            SELECT id
            FROM expedition_invitations
            WHERE expedition_id = ? AND invited_user_id IS NULL AND status = 'pending'
            """,
            (expedition["id"],),
        ).fetchone()
        if existing is not None:
            stored = _invitation_row(database, existing["id"])
            return _invitation_body(database, stored, token=None), False
        raw, digest = _new_token()
        row = database.execute(
            """
            INSERT INTO expedition_invitations (
                expedition_id, invited_by_user_id, invited_user_id,
                token_hash, status, expires_at
            )
            VALUES (?, ?, NULL, ?, 'pending', now() + interval '30 days')
            RETURNING id
            """,
            (expedition["id"], user_id, digest),
        ).fetchone()
        stored = _invitation_row(database, row["id"])
        return _invitation_body(database, stored, token=raw), True

    return _commit(write)


def accept_invitation(
    user_id: int,
    *,
    invitation_id: int | None = None,
    token: str | None = None,
    confirm_new_attempt: bool = False,
) -> dict:
    complete_due_expeditions()
    located = _locate_invitation(invitation_id, token)

    def write(database):
        _lock_user(database, user_id)
        preview = _peek_invitation(database, located, user_id)
        expedition = _lock_target_route(database, preview["expedition_id"])
        invitation = _lock_invitation_row(database, preview["id"])
        if invitation["invited_user_id"] not in (None, user_id):
            raise ExpeditionError("not_found", "That invitation does not exist.", status=404)
        if invitation["status"] != "pending":
            raise ExpeditionError("invalid_state", "That invitation is no longer pending.", status=409)
        if _is_past(database, invitation["expires_at"]):
            database.execute(
                "UPDATE expedition_invitations SET status = 'expired' WHERE id = ?",
                (invitation["id"],),
            )
            return {"expired": True}, True
        _ensure_acceptable(database, expedition)
        journey = _activate(
            database, user_id, expedition["route_id"], confirm_new_attempt
        )
        _require_slot(database, user_id, expedition)
        participation = _save_participation(
            database, user_id, expedition, journey["id"]
        )
        if invitation["invited_user_id"] is not None:
            database.execute(
                """
                UPDATE expedition_invitations
                SET status = 'accepted'
                WHERE id = ? AND status = 'pending'
                """,
                (invitation["id"],),
            )
        body = _joined(database, expedition["id"], participation, journey)
        body["invitation_id"] = invitation["id"]
        return body, True

    result = _commit(write)
    if result.get("expired"):
        raise ExpeditionError("invalid_state", "That invitation has expired.", status=409)
    return result


def decline_invitation(user_id: int, invitation_id: int) -> dict:
    def write(database):
        _lock_user(database, user_id)
        preview = _peek_invitation(database, ("id", invitation_id), user_id)
        invitation = _lock_invitation_row(database, preview["id"])
        if invitation["invited_user_id"] is None:
            raise ExpeditionError("invalid_state", "A share link cannot be declined.", status=409)
        if invitation["status"] == "pending" and _is_past(database, invitation["expires_at"]):
            database.execute(
                "UPDATE expedition_invitations SET status = 'expired' WHERE id = ?",
                (invitation["id"],),
            )
            return {"expired": True}, True
        if invitation["status"] != "pending":
            raise ExpeditionError("invalid_state", "That invitation is no longer pending.", status=409)
        database.execute(
            "UPDATE expedition_invitations SET status = 'declined' WHERE id = ?",
            (invitation["id"],),
        )
        return _invitation_body(database, _invitation_row(database, invitation["id"]), token=None), True

    result = _commit(write)
    if result.get("expired"):
        raise ExpeditionError("invalid_state", "That invitation has expired.", status=409)
    return result


def revoke_invitation(user_id: int, invitation_id: int) -> dict:
    def write(database):
        _lock_user(database, user_id)
        preview = database.execute(
            """
            SELECT id, expedition_id, status, invited_user_id, invited_by_user_id, token_hash
            FROM expedition_invitations
            WHERE id = ?
            """,
            (invitation_id,),
        ).fetchone()
        if preview is None:
            raise ExpeditionError("not_found", "That invitation does not exist.", status=404)
        expedition = _lock_expedition(database, preview["expedition_id"])
        invitation = _lock_invitation_row(database, preview["id"])
        if not _can_invite(database, user_id, expedition):
            raise ExpeditionError("not_found", MISSED, status=404)
        if invitation["status"] != "pending":
            raise ExpeditionError("invalid_state", "That invitation is no longer pending.", status=409)
        database.execute(
            "UPDATE expedition_invitations SET status = 'revoked' WHERE id = ?",
            (invitation["id"],),
        )
        return _invitation_body(database, _invitation_row(database, invitation["id"]), token=None), True

    return _commit(write)


def list_received_invitations(user_id: int) -> list[dict]:
    database = get_db()
    try:
        _require_account(database, user_id)
        rows = database.execute(
            """
            SELECT invitation.id, invitation.expedition_id, invitation.status,
                   invitation.expires_at, invitation.invited_user_id,
                   invitation.invited_by_user_id, invitation.token_hash,
                   account.username
            FROM expedition_invitations AS invitation
            JOIN users AS account ON account.id = invitation.invited_user_id
            WHERE invitation.invited_user_id = ?
              AND invitation.status = 'pending'
              AND invitation.expires_at > now()
            ORDER BY invitation.id
            """,
            (user_id,),
        ).fetchall()
        return [_invitation_body(database, row, token=None) for row in rows]
    finally:
        database.rollback()


def list_expedition_invitations(user_id: int, slug: str) -> list[dict]:
    database = get_db()
    try:
        _require_account(database, user_id)
        expedition = _find_slug(database, slug)
        if expedition is None or not _can_invite(database, user_id, expedition):
            raise ExpeditionError("not_found", MISSED, status=404)
        rows = database.execute(
            """
            SELECT invitation.id, invitation.expedition_id, invitation.status,
                   invitation.expires_at, invitation.invited_user_id,
                   invitation.invited_by_user_id, invitation.token_hash,
                   account.username
            FROM expedition_invitations AS invitation
            LEFT JOIN users AS account ON account.id = invitation.invited_user_id
            WHERE invitation.expedition_id = ? AND invitation.status = 'pending'
            ORDER BY invitation.id
            """,
            (expedition["id"],),
        ).fetchall()
        return [_invitation_body(database, row, token=None) for row in rows]
    except ExpeditionError:
        database.rollback()
        raise
    finally:
        database.rollback()


def create_organization(
    user_id: int,
    *,
    slug: str,
    name: str,
    owner_username: str,
    description: str = "",
) -> dict:
    _require_platform_admin(user_id)
    cleaned_slug = _clean_slug(slug)
    cleaned_name = _clean_name(name)
    cleaned_description = _clean_description(description)

    def write(database):
        owner = _user_by_name(database, owner_username)
        _lock_users(database, [user_id, owner["id"]])
        row = database.execute(
            """
            INSERT INTO organizations (slug, name, description)
            VALUES (?, ?, ?)
            RETURNING id
            """,
            (cleaned_slug, cleaned_name, cleaned_description),
        ).fetchone()
        database.execute(
            """
            INSERT INTO organization_memberships (organization_id, user_id, role, status)
            VALUES (?, ?, 'owner', 'active')
            """,
            (row["id"], owner["id"]),
        )
        return _organization_body(database, user_id, row["id"]), True

    return _commit(write)


def add_organization_member(
    user_id: int, slug: str, username: str, *, role: str = "member"
) -> dict:
    if role not in ("member", "admin"):
        raise ExpeditionError("invalid", "Choose a member or admin role.")

    def write(database):
        actor = _active_membership(database, user_id, slug, lock_user=False)
        if actor["role"] not in ("owner", "admin") or actor["organization_status"] != "active":
            raise ExpeditionError("forbidden", "You cannot edit this organization.", status=403)
        if role == "admin" and actor["role"] != "owner":
            raise ExpeditionError("forbidden", "Only an owner can name an admin.", status=403)
        person = _user_by_name(database, username)
        _lock_users(database, [user_id, person["id"]])
        actor = _active_membership(database, user_id, slug, lock_user=False)
        current = database.execute(
            """
            SELECT id, role, status
            FROM organization_memberships
            WHERE organization_id = ? AND user_id = ?
            FOR UPDATE
            """,
            (actor["organization_id"], person["id"]),
        ).fetchone()
        if current is not None and current["status"] == "active" and current["role"] == "owner":
            raise ExpeditionError("invalid_state", "The owner role is assigned separately.", status=409)
        if current is None:
            database.execute(
                """
                INSERT INTO organization_memberships (
                    organization_id, user_id, role, status
                )
                VALUES (?, ?, ?, 'active')
                """,
                (actor["organization_id"], person["id"], role),
            )
        else:
            database.execute(
                """
                UPDATE organization_memberships
                SET role = ?, status = 'active'
                WHERE id = ?
                """,
                (role, current["id"]),
            )
        return _member_body(database, actor["organization_id"], person["id"]), True

    return _commit(write)


def update_organization_role(user_id: int, slug: str, username: str, role: str) -> dict:
    if role not in ("member", "admin"):
        raise ExpeditionError("invalid", "Choose a member or admin role.")

    def write(database):
        actor = _active_membership(database, user_id, slug, lock_user=False)
        if actor["role"] != "owner":
            raise ExpeditionError("forbidden", "Only an owner can change roles.", status=403)
        person = _user_by_name(database, username)
        _lock_users(database, [user_id, person["id"]])
        actor = _active_membership(database, user_id, slug, lock_user=False)
        current = database.execute(
            """
            SELECT id, role, status
            FROM organization_memberships
            WHERE organization_id = ? AND user_id = ? AND status = 'active'
            FOR UPDATE
            """,
            (actor["organization_id"], person["id"]),
        ).fetchone()
        if current is None:
            raise ExpeditionError("not_found", "That member does not exist.", status=404)
        if current["role"] == "owner":
            raise ExpeditionError(
                "ownership_transfer_required",
                "Assign another owner before changing this role.",
                status=409,
            )
        database.execute(
            "UPDATE organization_memberships SET role = ? WHERE id = ?",
            (role, current["id"]),
        )
        return _member_body(database, actor["organization_id"], person["id"]), True

    return _commit(write)


def remove_organization_member(user_id: int, slug: str, username: str) -> dict:
    def write(database):
        actor = _active_membership(database, user_id, slug, lock_user=False)
        if actor["role"] not in ("owner", "admin"):
            raise ExpeditionError("forbidden", "You cannot edit this organization.", status=403)
        person = _user_by_name(database, username)
        _lock_users(database, [user_id, person["id"]])
        actor = _active_membership(database, user_id, slug, lock_user=False)
        current = database.execute(
            """
            SELECT id, role, status
            FROM organization_memberships
            WHERE organization_id = ? AND user_id = ? AND status = 'active'
            FOR UPDATE
            """,
            (actor["organization_id"], person["id"]),
        ).fetchone()
        if current is None:
            raise ExpeditionError("not_found", "That member does not exist.", status=404)
        if current["role"] == "owner":
            raise ExpeditionError(
                "ownership_transfer_required",
                "Assign another owner before removing this member.",
                status=409,
            )
        if current["role"] == "admin" and actor["role"] != "owner":
            raise ExpeditionError("forbidden", "Only an owner can remove an admin.", status=403)
        database.execute(
            "UPDATE organization_memberships SET status = 'removed' WHERE id = ?",
            (current["id"],),
        )
        return {"user_id": person["id"], "status": "removed"}, True

    return _commit(write)


def assign_organization_owner(user_id: int, slug: str, username: str) -> dict:
    _require_platform_admin(user_id)

    def write(database):
        person = _user_by_name(database, username)
        _lock_users(database, [user_id, person["id"]])
        organization = database.execute(
            "SELECT id, status FROM organizations WHERE slug = ? FOR UPDATE",
            (slug,),
        ).fetchone()
        if organization is None:
            raise ExpeditionError("not_found", "That organization does not exist.", status=404)
        database.execute(
            """
            UPDATE organization_memberships
            SET role = 'admin'
            WHERE organization_id = ? AND role = 'owner' AND status = 'active'
              AND user_id <> ?
            """,
            (organization["id"], person["id"]),
        )
        current = database.execute(
            """
            SELECT id
            FROM organization_memberships
            WHERE organization_id = ? AND user_id = ?
            FOR UPDATE
            """,
            (organization["id"], person["id"]),
        ).fetchone()
        if current is None:
            database.execute(
                """
                INSERT INTO organization_memberships (
                    organization_id, user_id, role, status
                )
                VALUES (?, ?, 'owner', 'active')
                """,
                (organization["id"], person["id"]),
            )
        else:
            database.execute(
                """
                UPDATE organization_memberships
                SET role = 'owner', status = 'active'
                WHERE id = ?
                """,
                (current["id"],),
            )
        return _organization_body(database, user_id, organization["id"]), True

    return _commit(write)


def suspend_organization(user_id: int, slug: str) -> dict:
    _require_platform_admin(user_id)
    for _attempt in range(_CLOSE_ATTEMPTS):
        pending = _active_organization_expeditions(slug)
        for expedition_id in pending:
            _commit(
                lambda database, expedition_id=expedition_id: _close(
                    database,
                    expedition_id,
                    actor_id=None,
                    terminal="completed",
                )
            )
        if not pending and _mark_suspended(user_id, slug):
            return get_organization(user_id, slug)
    raise ExpeditionError(
        "conflict",
        "The organization changed during suspension. Try again.",
        status=409,
    )


def create_organization_expedition(
    user_id: int,
    organization_slug: str,
    *,
    slug: str,
    name: str,
    route_slug: str,
    enrollment: str,
    description: str = "",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    enrollment_opens_at: datetime | None = None,
    enrollment_closes_at: datetime | None = None,
    welcome_override: str | None = None,
    accent_color: str | None = None,
    image_ref: str | None = None,
) -> dict:
    complete_due_expeditions()
    if enrollment not in ("open", "invitation"):
        raise ExpeditionError("invalid", "Enrollment must be open or invitation.")
    cleaned_slug = _clean_slug(slug)
    cleaned_name = _clean_name(name)
    cleaned_description = _clean_description(description)
    for value in (starts_at, ends_at, enrollment_opens_at, enrollment_closes_at):
        _require_aware(value)
    if starts_at is not None and ends_at is not None and ends_at <= starts_at:
        raise ExpeditionError("invalid", "Choose an end later than the start.")
    if (
        enrollment_opens_at is not None
        and enrollment_closes_at is not None
        and enrollment_closes_at <= enrollment_opens_at
    ):
        raise ExpeditionError("invalid", "Choose an enrollment close later than the open.")

    def write(database):
        _lock_user(database, user_id)
        organization = _managed_organization(database, user_id, organization_slug)
        route = _published_route(database, route_slug)
        row = database.execute(
            """
            INSERT INTO expeditions (
                slug, name, description, route_id, visibility, owner_user_id,
                kind, status, organization_id, enrollment, starts_at, ends_at,
                enrollment_opens_at, enrollment_closes_at, welcome_override,
                accent_color, image_ref
            )
            VALUES (
                ?, ?, ?, ?, 'public', NULL, 'organization', 'active', ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            RETURNING id
            """,
            (
                cleaned_slug,
                cleaned_name,
                cleaned_description,
                route["id"],
                organization["id"],
                enrollment,
                starts_at,
                ends_at,
                enrollment_opens_at,
                enrollment_closes_at,
                welcome_override,
                accent_color,
                image_ref,
            ),
        ).fetchone()
        return _profile(database, user_id, _lock_expedition(database, row["id"])), True

    return _commit(write)


def extend_expedition(user_id: int, slug: str, ends_at: datetime) -> dict:
    _require_aware(ends_at)

    def write(database):
        _lock_user(database, user_id)
        expedition = _find_slug(database, slug)
        if expedition is None or expedition["kind"] != "organization":
            raise ExpeditionError("not_found", MISSED, status=404)
        if not _is_org_manager(database, user_id, expedition["organization_id"]):
            raise ExpeditionError("forbidden", "You cannot manage this expedition.", status=403)
        locked = _lock_expedition(database, expedition["id"])
        if locked["status"] != "active":
            raise ExpeditionError("invalid_state", "This expedition is not open.", status=409)
        updated = database.execute(
            """
            UPDATE expeditions
            SET ends_at = ?
            WHERE id = ?
              AND status = 'active'
              AND (starts_at IS NULL OR ? > starts_at)
              AND (ends_at IS NULL OR ? > ends_at)
            RETURNING id
            """,
            (ends_at, locked["id"], ends_at, ends_at),
        ).fetchone()
        if updated is None:
            raise ExpeditionError("invalid", "Choose an end later than the current schedule.")
        return _profile(database, user_id, _lock_expedition(database, locked["id"])), True

    return _commit(write)


def complete_organization_expedition(user_id: int, slug: str) -> dict:
    def write(database):
        expedition = _find_slug(database, slug)
        if expedition is None or expedition["kind"] != "organization":
            raise ExpeditionError("not_found", MISSED, status=404)
        if not _is_org_manager(database, user_id, expedition["organization_id"]):
            raise ExpeditionError("forbidden", "You cannot manage this expedition.", status=403)
        return _close(
            database,
            expedition["id"],
            actor_id=user_id,
            terminal="completed",
        )

    return _commit(write)


def complete_due_expeditions() -> list[int]:
    database = get_db()
    try:
        rows = database.execute(
            """
            SELECT id
            FROM expeditions
            WHERE kind = 'organization'
              AND status = 'active'
              AND ends_at IS NOT NULL
              AND ends_at <= now()
            ORDER BY id
            """
        ).fetchall()
    finally:
        database.rollback()
    closed = []
    for row in rows:
        result = _commit(
            lambda database, expedition_id=row["id"]: _close(
                database,
                expedition_id,
                actor_id=None,
                terminal="completed",
                only_if_due=True,
            )
        )
        if not result.get("skipped"):
            closed.append(row["id"])
    return closed


def get_expedition(user_id: int, slug: str) -> dict:
    complete_due_expeditions()
    database = get_db()
    try:
        _require_account(database, user_id)
        expedition, access = _readable(database, user_id, slug)
        if access == "none":
            raise ExpeditionError("not_found", MISSED, status=404)
        return _profile(database, user_id, expedition)
    except ExpeditionError:
        database.rollback()
        raise
    finally:
        database.rollback()


def get_roster(user_id: int, slug: str) -> list[dict]:
    complete_due_expeditions()
    database = get_db()
    try:
        _require_account(database, user_id)
        expedition, access = _readable(database, user_id, slug)
        _require_board_access(access)
        rows = database.execute(
            """
            SELECT participation.user_id, participation.status, participation.joined_at,
                   profile.display_name
            FROM expedition_participations AS participation
            JOIN user_profiles AS profile ON profile.user_id = participation.user_id
            WHERE participation.expedition_id = ?
            ORDER BY profile.display_name, participation.user_id
            """,
            (expedition["id"],),
        ).fetchall()
        return [dict(row) for row in rows]
    except ExpeditionError:
        database.rollback()
        raise
    finally:
        database.rollback()


def get_leaderboard(user_id: int, slug: str) -> dict:
    complete_due_expeditions()
    database = get_db()
    try:
        _require_account(database, user_id)
        expedition, access = _readable(database, user_id, slug)
        _require_board_access(access)
        if expedition["status"] == "active":
            return {"mode": "live", "entries": _live_entries(database, expedition)}
        return {"mode": "final", "entries": _final_entries(database, expedition["id"])}
    except ExpeditionError:
        database.rollback()
        raise
    finally:
        database.rollback()


def get_updates(user_id: int, slug: str) -> list[dict]:
    complete_due_expeditions()
    database = get_db()
    try:
        _require_account(database, user_id)
        expedition, access = _readable(database, user_id, slug)
        _require_board_access(access)
        rows = database.execute(
            """
            SELECT feed.id, feed.user_id, profile.display_name, feed.kind,
                   feed.activity_id, feed.checkpoint_name, feed.elevation_m,
                   feed.created_at
            FROM expedition_updates AS feed
            JOIN user_profiles AS profile ON profile.user_id = feed.user_id
            WHERE feed.expedition_id = ?
            ORDER BY feed.id
            """,
            (expedition["id"],),
        ).fetchall()
        return [dict(row) for row in rows]
    except ExpeditionError:
        database.rollback()
        raise
    finally:
        database.rollback()


def get_organization(user_id: int, slug: str) -> dict:
    database = get_db()
    try:
        _require_account(database, user_id)
        organization = _find_organization(database, slug)
        if organization is None or not _can_read_organization(database, user_id, organization["id"]):
            raise ExpeditionError("not_found", "That organization does not exist.", status=404)
        return _organization_body(database, user_id, organization["id"])
    except ExpeditionError:
        database.rollback()
        raise
    finally:
        database.rollback()


def list_organization_members(user_id: int, slug: str) -> list[dict]:
    database = get_db()
    try:
        _require_account(database, user_id)
        organization = _find_organization(database, slug)
        if organization is None or not _can_read_organization(database, user_id, organization["id"]):
            raise ExpeditionError("not_found", "That organization does not exist.", status=404)
        if not _is_org_manager(database, user_id, organization["id"]) and user_id not in _admin_ids():
            raise ExpeditionError("forbidden", "You cannot view this membership list.", status=403)
        rows = database.execute(
            """
            SELECT membership.user_id, account.username, profile.display_name,
                   membership.role, membership.status
            FROM organization_memberships AS membership
            JOIN users AS account ON account.id = membership.user_id
            JOIN user_profiles AS profile ON profile.user_id = membership.user_id
            WHERE membership.organization_id = ? AND membership.status = 'active'
            ORDER BY membership.role, profile.display_name, membership.user_id
            """,
            (organization["id"],),
        ).fetchall()
        return [dict(row) for row in rows]
    except ExpeditionError:
        database.rollback()
        raise
    finally:
        database.rollback()


def list_organization_expeditions(user_id: int, slug: str) -> list[dict]:
    complete_due_expeditions()
    database = get_db()
    try:
        _require_account(database, user_id)
        organization = _find_organization(database, slug)
        if organization is None or not _can_read_organization(database, user_id, organization["id"]):
            raise ExpeditionError("not_found", "That organization does not exist.", status=404)
        manager = _is_org_manager(database, user_id, organization["id"])
        rows = database.execute(
            """
            SELECT id
            FROM expeditions
            WHERE organization_id = ?
            ORDER BY id
            """,
            (organization["id"],),
        ).fetchall()
        visible = []
        for row in rows:
            expedition = _expedition(database, row["id"])
            access = _access(database, user_id, expedition)
            if manager or access in ("profile", "read", "manage"):
                if not manager and expedition["enrollment"] == "invitation" and access == "none":
                    continue
                profile = _profile(database, user_id, expedition)
                profile["can_manage"] = manager
                visible.append(profile)
        return visible
    except ExpeditionError:
        database.rollback()
        raise
    finally:
        database.rollback()


def remove_participation_privacy(user_id: int, slug: str, *, confirm: bool) -> dict:
    if confirm is not True:
        raise ExpeditionError("invalid", "Confirmation is required.")

    def write(database):
        expedition = _find_slug(database, slug)
        if expedition is None:
            raise ExpeditionError("not_found", MISSED, status=404)
        for _attempt in range(_CLOSE_ATTEMPTS):
            try:
                return _privacy_once(database, user_id, expedition["id"]), True
            except _Retry:
                database.rollback()
        raise ExpeditionError(
            "conflict",
            "The expedition changed during this update. Try again.",
            status=409,
        )

    return _commit(write)


def _privacy_once(database, user_id: int, expedition_id: int) -> dict:
    preview = _participation_row(database, user_id, expedition_id)
    if preview is None:
        raise ExpeditionError("not_found", MISSED, status=404)
    expedition = _expedition(database, expedition_id)
    owner_release = (
        expedition["kind"] == "private" and expedition["owner_user_id"] == user_id
    )
    keeper_ids = []
    if owner_release:
        keepers = database.execute(
            """
            SELECT id, user_id
            FROM expedition_participations
            WHERE expedition_id = ?
              AND status = 'withdrawn'
              AND final_standings_eligible
              AND user_id <> ?
            ORDER BY id
            """,
            (expedition_id, user_id),
        ).fetchall()
        keeper_ids = [row["id"] for row in keepers]
    user_ids = [user_id]
    if owner_release:
        user_ids.extend(
            row["user_id"]
            for row in database.execute(
                """
                SELECT user_id
                FROM expedition_participations
                WHERE expedition_id = ?
                  AND status = 'withdrawn'
                  AND final_standings_eligible
                  AND user_id <> ?
                """,
                (expedition_id, user_id),
            ).fetchall()
        )
    _lock_users(database, user_ids)
    locked = _lock_expedition(database, expedition_id)
    current = _participation_row(database, user_id, expedition_id)
    if current is None:
        raise ExpeditionError("not_found", MISSED, status=404)
    owner_release = locked["kind"] == "private" and locked["owner_user_id"] == user_id
    active_other = database.execute(
        """
        SELECT 1 AS found
        FROM expedition_participations
        WHERE expedition_id = ? AND status = 'active' AND user_id <> ?
        LIMIT 1
        """,
        (expedition_id, user_id),
    ).fetchone()
    if owner_release and active_other is not None:
        raise ExpeditionError(
            "ownership_transfer_required",
            "Transfer ownership before leaving this expedition.",
            status=409,
        )
    if owner_release:
        again = database.execute(
            """
            SELECT id
            FROM expedition_participations
            WHERE expedition_id = ?
              AND status = 'withdrawn'
              AND final_standings_eligible
              AND user_id <> ?
            ORDER BY id
            """,
            (expedition_id, user_id),
        ).fetchall()
        if [row["id"] for row in again] != keeper_ids:
            raise _Retry()
        journeys = database.execute(
            """
            SELECT journey_id
            FROM expedition_participations
            WHERE expedition_id = ?
              AND status = 'withdrawn'
              AND final_standings_eligible
              AND user_id <> ?
            ORDER BY journey_id
            """,
            (expedition_id, user_id),
        ).fetchall()
        if journeys:
            _lock_ids(
                database,
                "route_journeys",
                [row["journey_id"] for row in journeys],
            )
        _lock_ids(database, "expedition_participations", [row["id"] for row in again])
        _write_snapshot(
            database,
            locked,
            _candidate_rows(database, expedition_id, exclude_user_id=user_id, withdrawn_only=True),
        )
    else:
        _lock_ids(database, "route_journeys", [current["journey_id"]])
        _lock_ids(database, "expedition_participations", [current["id"]])
    _set_privacy(database, user_id)
    database.execute(
        """
        DELETE FROM expedition_updates
        WHERE expedition_id = ? AND user_id = ?
        """,
        (expedition_id, user_id),
    )
    database.execute(
        """
        DELETE FROM expedition_result_entries
        WHERE expedition_id = ? AND user_id = ?
        """,
        (expedition_id, user_id),
    )
    database.execute(
        """
        DELETE FROM expedition_participations
        WHERE expedition_id = ? AND user_id = ?
        """,
        (expedition_id, user_id),
    )
    if owner_release:
        remaining_people = database.execute(
            "SELECT COUNT(*) AS count FROM expedition_participations WHERE expedition_id = ?",
            (expedition_id,),
        ).fetchone()["count"]
        remaining_results = database.execute(
            "SELECT COUNT(*) AS count FROM expedition_result_entries WHERE expedition_id = ?",
            (expedition_id,),
        ).fetchone()["count"]
        if remaining_people == 0 and remaining_results == 0:
            database.execute("DELETE FROM expeditions WHERE id = ?", (expedition_id,))
            return {"removed": True, "expedition": None}
        database.execute(
            """
            UPDATE expeditions
            SET status = 'archived', owner_user_id = NULL
            WHERE id = ? AND kind = 'private'
            """,
            (expedition_id,),
        )
    _remember_privacy(database, expedition_id, user_id)
    return {"removed": True, "expedition_id": expedition_id}


def _close(database, expedition_id: int, *, actor_id: int | None, terminal: str, only_if_due: bool = False):
    for _attempt in range(_CLOSE_ATTEMPTS):
        try:
            return _close_once(
                database,
                expedition_id,
                actor_id=actor_id,
                terminal=terminal,
                only_if_due=only_if_due,
            )
        except _Retry:
            database.rollback()
    raise ExpeditionError(
        "conflict",
        "The expedition changed during close. Try again.",
        status=409,
    )


def _close_once(database, expedition_id: int, *, actor_id: int | None, terminal: str, only_if_due: bool):
    candidates = _candidate_rows(database, expedition_id)
    user_ids = [row["user_id"] for row in candidates]
    if actor_id is not None:
        user_ids.append(actor_id)
    _lock_users(database, user_ids)
    expedition = _lock_expedition(database, expedition_id)
    again = _candidate_rows(database, expedition_id)
    if [row["id"] for row in again] != [row["id"] for row in candidates]:
        raise _Retry()
    if only_if_due and not _is_due(database, expedition):
        return {"skipped": True, "expedition_id": expedition_id}, False
    if expedition["status"] != "active":
        if expedition["status"] == terminal:
            return _closed_body(database, expedition), False
        raise ExpeditionError("invalid_state", "This expedition is not open.", status=409)
    if terminal == "archived" and expedition["kind"] != "private":
        raise ExpeditionError("invalid_state", "This expedition cannot be archived.", status=409)
    if terminal == "completed" and expedition["kind"] != "organization":
        raise ExpeditionError("invalid_state", "This expedition cannot be completed.", status=409)
    if expedition["kind"] == "global":
        raise ExpeditionError("invalid_state", "This expedition cannot be closed.", status=409)
    journey_ids = [row["journey_id"] for row in again]
    participation_ids = [row["id"] for row in again]
    _lock_ids(database, "route_journeys", journey_ids)
    _lock_ids(database, "expedition_participations", participation_ids)
    final_rows = _candidate_rows(database, expedition_id)
    if [row["id"] for row in final_rows] != participation_ids:
        raise _Retry()
    database.execute(
        "UPDATE expeditions SET status = ? WHERE id = ? AND status = 'active'",
        (terminal, expedition_id),
    )
    _write_snapshot(database, expedition, final_rows)
    return _closed_body(database, _expedition(database, expedition_id)), True


def _write_snapshot(database, expedition, candidates) -> None:
    if not candidates:
        return
    existing = {
        row["participation_id"]
        for row in database.execute(
            """
            SELECT participation_id
            FROM expedition_result_entries
            WHERE expedition_id = ?
            """,
            (expedition["id"],),
        ).fetchall()
    }
    checkpoints = database.execute(
        """
        SELECT name, elevation_m
        FROM route_checkpoints
        WHERE route_id = ?
        ORDER BY elevation_m, position
        """,
        (expedition["route_id"],),
    ).fetchall()
    captured = database.execute("SELECT now() AS captured_at").fetchone()["captured_at"]
    ranked = []
    for candidate in candidates:
        if candidate["id"] in existing:
            continue
        total, last_credit = _journey_total(
            database, candidate["journey_id"], candidate["display_credits_after"]
        )
        name = database.execute(
            "SELECT display_name FROM user_profiles WHERE user_id = ?",
            (candidate["user_id"],),
        ).fetchone()
        ranked.append(
            {
                "participation_id": candidate["id"],
                "user_id": candidate["user_id"],
                "journey_id": candidate["journey_id"],
                "display_name": name["display_name"],
                "total_m": total,
                "last_credit_at": last_credit,
            }
        )
    ranked.sort(
        key=lambda row: (
            -row["total_m"],
            row["last_credit_at"] is None,
            row["last_credit_at"] or datetime.min.replace(tzinfo=timezone.utc),
            row["display_name"],
            row["user_id"],
        )
    )
    for rank, row in enumerate(ranked, start=1):
        database.execute(
            """
            INSERT INTO expedition_result_entries (
                expedition_id, participation_id, user_id, journey_id,
                display_name, total_m, rank, checkpoint_name, captured_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                expedition["id"],
                row["participation_id"],
                row["user_id"],
                row["journey_id"],
                row["display_name"],
                row["total_m"],
                rank,
                _checkpoint_name(checkpoints, row["total_m"]),
                captured,
            ),
        )


def _candidate_rows(database, expedition_id: int, *, exclude_user_id: int | None = None, withdrawn_only: bool = False):
    if withdrawn_only:
        rows = database.execute(
            """
            SELECT id, user_id, journey_id, display_credits_after, status
            FROM expedition_participations
            WHERE expedition_id = ?
              AND status = 'withdrawn'
              AND final_standings_eligible
              AND (? IS NULL OR user_id <> ?)
            ORDER BY id
            """,
            (expedition_id, exclude_user_id, exclude_user_id),
        ).fetchall()
        return list(rows)
    rows = database.execute(
        """
        SELECT id, user_id, journey_id, display_credits_after, status
        FROM expedition_participations
        WHERE expedition_id = ?
          AND (
              status = 'active'
              OR (status = 'withdrawn' AND final_standings_eligible)
          )
        ORDER BY id
        """,
        (expedition_id,),
    ).fetchall()
    return list(rows)


def _live_entries(database, expedition) -> list[dict]:
    rows = database.execute(
        """
        SELECT participation.user_id, participation.journey_id, profile.display_name,
               COALESCE(SUM(credit.credited_elevation_m), 0) AS total_m,
               MAX(credit.created_at) AS last_credit_at
        FROM expedition_participations AS participation
        JOIN user_profiles AS profile ON profile.user_id = participation.user_id
        LEFT JOIN journey_credits AS credit
          ON credit.journey_id = participation.journey_id
         AND (
             participation.display_credits_after IS NULL
             OR credit.created_at >= participation.display_credits_after
         )
        WHERE participation.expedition_id = ?
          AND participation.status = 'active'
        GROUP BY participation.user_id, participation.journey_id, profile.display_name
        """,
        (expedition["id"],),
    ).fetchall()
    checkpoints = database.execute(
        """
        SELECT name, elevation_m
        FROM route_checkpoints
        WHERE route_id = ?
        ORDER BY elevation_m, position
        """,
        (expedition["route_id"],),
    ).fetchall()
    ordered = sorted(
        rows,
        key=lambda row: (
            -row["total_m"],
            row["last_credit_at"] is None,
            row["last_credit_at"] or datetime.min.replace(tzinfo=timezone.utc),
            row["display_name"],
            row["user_id"],
        ),
    )
    return [
        {
            "user_id": row["user_id"],
            "display_name": row["display_name"],
            "journey_id": row["journey_id"],
            "total_m": row["total_m"],
            "rank": rank,
            "checkpoint_name": _checkpoint_name(checkpoints, row["total_m"]),
            "last_credit_at": row["last_credit_at"],
        }
        for rank, row in enumerate(ordered, start=1)
    ]


def _final_entries(database, expedition_id: int) -> list[dict]:
    rows = database.execute(
        """
        SELECT user_id, display_name, journey_id, total_m, rank,
               checkpoint_name, captured_at
        FROM expedition_result_entries
        WHERE expedition_id = ?
        ORDER BY rank, user_id
        """,
        (expedition_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _journey_total(database, journey_id: int, cutoff):
    row = database.execute(
        """
        SELECT COALESCE(SUM(credited_elevation_m), 0) AS total_m,
               MAX(created_at) AS last_credit_at
        FROM journey_credits
        WHERE journey_id = ?
          AND (?::timestamptz IS NULL OR created_at >= ?::timestamptz)
        """,
        (journey_id, cutoff, cutoff),
    ).fetchone()
    return row["total_m"], row["last_credit_at"]


def _checkpoint_name(checkpoints, total) -> str:
    # No checkpoint sits at zero. A participant who has not reached the first
    # stop still needs a name because the result column is required.
    name = "Start"
    for checkpoint in checkpoints:
        if checkpoint["elevation_m"] <= total:
            name = checkpoint["name"]
        else:
            break
    return name


def _reload_for_join(database, user_id: int, slug: str) -> dict:
    expedition = _find_slug(database, slug)
    if expedition is None or not _may_attempt_join(database, user_id, expedition):
        raise ExpeditionError("not_found", MISSED, status=404)
    locked = _lock_target_route(database, expedition["id"])
    _reject_unjoinable(database, user_id, locked)
    return locked


def _lock_target_route(database, expedition_id: int):
    expedition = _expedition(database, expedition_id)
    database.execute(
        """
        SELECT id
        FROM expeditions
        WHERE route_id = ? AND (status = 'active' OR id = ?)
        ORDER BY id
        FOR UPDATE
        """,
        (expedition["route_id"], expedition_id),
    ).fetchall()
    return _lock_expedition(database, expedition_id)


def _ensure_acceptable(database, expedition) -> None:
    route = database.execute(
        "SELECT status FROM routes WHERE id = ?",
        (expedition["route_id"],),
    ).fetchone()
    if route is None or route["status"] != "published":
        raise ExpeditionError("not_found", "That route does not exist.", status=404)
    if expedition["status"] != "active":
        raise ExpeditionError("invalid_state", "This expedition is not open.", status=409)
    if expedition["kind"] == "organization" and not _enrollment_open(database, expedition):
        raise ExpeditionError("invalid_state", "Enrollment is closed.", status=409)


def _may_attempt_join(database, user_id: int, expedition) -> bool:
    if expedition["kind"] == "global":
        return True
    if expedition["kind"] == "private":
        return expedition["owner_user_id"] == user_id
    if expedition["enrollment"] == "open":
        return True
    return _is_org_manager(database, user_id, expedition["organization_id"]) or _is_invited(
        database, user_id, expedition["id"]
    )


def _reject_unjoinable(database, user_id: int, expedition) -> None:
    route = database.execute(
        "SELECT status FROM routes WHERE id = ?",
        (expedition["route_id"],),
    ).fetchone()
    if route is None or route["status"] != "published":
        raise ExpeditionError("not_found", "That route does not exist.", status=404)
    if expedition["kind"] == "private":
        if expedition["owner_user_id"] != user_id:
            raise ExpeditionError("not_found", MISSED, status=404)
        if expedition["status"] != "active":
            raise ExpeditionError("invalid_state", "This expedition is not open.", status=409)
        return
    if expedition["kind"] == "global":
        if expedition["status"] != "active":
            raise ExpeditionError("invalid_state", "This expedition is not open.", status=409)
        return
    visible = _is_org_manager(database, user_id, expedition["organization_id"]) or _is_invited(
        database, user_id, expedition["id"]
    )
    if expedition["enrollment"] == "invitation" and not visible:
        raise ExpeditionError("not_found", MISSED, status=404)
    if expedition["enrollment"] == "invitation":
        raise ExpeditionError("forbidden", "This expedition requires an invitation.", status=403)
    if expedition["status"] != "active":
        raise ExpeditionError("invalid_state", "This expedition is not open.", status=409)
    if not _enrollment_open(database, expedition):
        raise ExpeditionError("invalid_state", "Enrollment is closed.", status=409)


def _require_slot(database, user_id: int, expedition) -> None:
    if expedition["kind"] != "organization":
        return
    occupied = database.execute(
        """
        SELECT expedition.id
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ?
          AND participation.status = 'active'
          AND expedition.kind = 'organization'
          AND expedition.status = 'active'
          AND expedition.id <> ?
        LIMIT 1
        """,
        (user_id, expedition["id"]),
    ).fetchone()
    if occupied is not None:
        raise ExpeditionError(
            "slot_occupied",
            "You already have an active organization expedition.",
            status=409,
        )


def _save_participation(database, user_id: int, expedition, journey_id: int) -> dict:
    existing = database.execute(
        """
        SELECT id
        FROM expedition_participations
        WHERE user_id = ? AND expedition_id = ?
        FOR UPDATE
        """,
        (user_id, expedition["id"]),
    ).fetchone()
    if existing is not None:
        database.execute(
            """
            UPDATE expedition_participations
            SET status = 'active', journey_id = ?, route_id = ?
            WHERE id = ?
            """,
            (journey_id, expedition["route_id"], existing["id"]),
        )
        return _participation(database, existing["id"])
    # A deleted participation cannot mark itself. The privacy-removal row is
    # the signal that this insert is a rejoin, which is when the board clock starts.
    after_privacy = _consume_privacy(database, expedition["id"], user_id)
    row = database.execute(
        """
        INSERT INTO expedition_participations (
            user_id, expedition_id, route_id, journey_id, status, display_credits_after
        )
        VALUES (
            ?, ?, ?, ?, 'active',
            CASE WHEN ? THEN now() ELSE NULL END
        )
        RETURNING id
        """,
        (user_id, expedition["id"], expedition["route_id"], journey_id, after_privacy),
    ).fetchone()
    return _participation(database, row["id"])


def _activate(database, user_id: int, route_id: int, confirm_new_attempt: bool) -> dict:
    try:
        return activate_for_participation(
            database,
            user_id,
            route_id,
            confirm_new_attempt=confirm_new_attempt,
        )
    except JourneyError as exc:
        raise _from_journey(exc) from exc


def _remember_privacy(database, expedition_id: int, user_id: int) -> None:
    database.execute(
        """
        INSERT INTO expedition_privacy_removals (expedition_id, user_id)
        VALUES (?, ?)
        ON CONFLICT (expedition_id, user_id)
        DO UPDATE SET removed_at = now()
        """,
        (expedition_id, user_id),
    )


def _consume_privacy(database, expedition_id: int, user_id: int) -> bool:
    row = database.execute(
        """
        DELETE FROM expedition_privacy_removals
        WHERE expedition_id = ? AND user_id = ?
        RETURNING expedition_id
        """,
        (expedition_id, user_id),
    ).fetchone()
    return row is not None


def _set_privacy(database, user_id: int) -> None:
    database.execute("SELECT set_config('adventurer.privacy_removal', 'on', true)")
    database.execute(
        "SELECT set_config('adventurer.privacy_removal_user_id', ?, true)",
        (str(user_id),),
    )


def _expire_username(database, expedition_id: int, invited_user_id: int) -> None:
    database.execute(
        """
        UPDATE expedition_invitations
        SET status = 'expired'
        WHERE expedition_id = ?
          AND invited_user_id = ?
          AND status = 'pending'
          AND expires_at <= now()
        """,
        (expedition_id, invited_user_id),
    )


def _expire_share_link(database, expedition_id: int) -> None:
    database.execute(
        """
        UPDATE expedition_invitations
        SET status = 'expired'
        WHERE expedition_id = ?
          AND invited_user_id IS NULL
          AND status = 'pending'
          AND expires_at <= now()
        """,
        (expedition_id,),
    )


def _locate_invitation(invitation_id, token):
    if (invitation_id is None) == (token is None):
        raise ExpeditionError("invalid", "Provide an invitation id or a share token.")
    if invitation_id is not None:
        return ("id", invitation_id)
    return ("token", hashlib.sha256(token.encode("utf-8")).hexdigest())


def _peek_invitation(database, located, user_id: int):
    row = _select_invitation(database, located, lock=False)
    if row is None:
        raise ExpeditionError("not_found", "That invitation does not exist.", status=404)
    if row["invited_user_id"] is not None and row["invited_user_id"] != user_id:
        raise ExpeditionError("not_found", "That invitation does not exist.", status=404)
    if row["status"] != "pending":
        raise ExpeditionError("invalid_state", "That invitation is no longer pending.", status=409)
    return row


def _select_invitation(database, located, *, lock: bool):
    kind, value = located
    column = "id" if kind == "id" else "token_hash"
    locking = " FOR UPDATE" if lock else ""
    return database.execute(
        f"""
        SELECT id, expedition_id, invited_by_user_id, invited_user_id,
               token_hash, status, expires_at
        FROM expedition_invitations
        WHERE {column} = ?{locking}
        """,
        (value,),
    ).fetchone()


def _lock_invitation_row(database, invitation_id: int):
    return database.execute(
        """
        SELECT id, expedition_id, invited_by_user_id, invited_user_id,
               token_hash, status, expires_at
        FROM expedition_invitations
        WHERE id = ?
        FOR UPDATE
        """,
        (invitation_id,),
    ).fetchone()


def _invitation_row(database, invitation_id: int):
    return database.execute(
        """
        SELECT invitation.id, invitation.expedition_id, invitation.status,
               invitation.expires_at, invitation.invited_user_id,
               invitation.invited_by_user_id, invitation.token_hash,
               account.username
        FROM expedition_invitations AS invitation
        LEFT JOIN users AS account ON account.id = invitation.invited_user_id
        WHERE invitation.id = ?
        """,
        (invitation_id,),
    ).fetchone()


def _invitation_body(database, row, *, token) -> dict:
    username = row.get("username")
    if username is None and row["invited_user_id"] is not None:
        found = database.execute(
            "SELECT username FROM users WHERE id = ?",
            (row["invited_user_id"],),
        ).fetchone()
        username = None if found is None else found["username"]
    return {
        "id": row["id"],
        "expedition_id": row["expedition_id"],
        "status": row["status"],
        "expires_at": row["expires_at"],
        "username": username,
        "kind": "link" if row["invited_user_id"] is None else "username",
        "token": token,
    }


def _inviter_expedition(database, user_id: int, slug: str):
    expedition = _find_slug(database, slug)
    if expedition is None:
        raise ExpeditionError("not_found", MISSED, status=404)
    locked = _lock_expedition(database, expedition["id"])
    if locked["status"] != "active" or not _can_invite(database, user_id, locked):
        raise ExpeditionError("not_found", MISSED, status=404)
    return locked


def _can_invite(database, user_id: int, expedition) -> bool:
    if expedition["kind"] == "private":
        return expedition["owner_user_id"] == user_id
    if expedition["kind"] == "organization":
        return _is_org_manager(database, user_id, expedition["organization_id"])
    return False


def _readable(database, user_id: int, slug: str):
    expedition = _find_slug(database, slug)
    if expedition is None:
        return None, "none"
    access = _access(database, user_id, expedition)
    if access == "none":
        return None, "none"
    return expedition, access


def _access(database, user_id: int, expedition) -> str:
    participation = _participation_row(database, user_id, expedition["id"])
    if expedition["kind"] == "private":
        if expedition["owner_user_id"] == user_id:
            return "manage"
        if participation is not None and participation["status"] == "active":
            return "read"
        if participation is not None and participation["retained_read_access"]:
            return "read"
        return "none"
    if expedition["kind"] == "global":
        return "read"
    manager = _is_org_manager(database, user_id, expedition["organization_id"])
    if manager:
        return "manage"
    if participation is not None and (
        participation["status"] == "active" or participation["retained_read_access"]
    ):
        return "read"
    if expedition["enrollment"] == "open":
        return "profile"
    if _is_invited(database, user_id, expedition["id"]):
        return "profile"
    return "none"


def _require_board_access(access: str) -> None:
    if access in ("read", "manage"):
        return
    if access == "profile":
        raise ExpeditionError("forbidden", "You cannot view that roster.", status=403)
    raise ExpeditionError("not_found", MISSED, status=404)


def _is_invited(database, user_id: int, expedition_id: int) -> bool:
    row = database.execute(
        """
        SELECT 1 AS found
        FROM expedition_invitations
        WHERE expedition_id = ?
          AND invited_user_id = ?
          AND status = 'pending'
          AND expires_at > now()
        LIMIT 1
        """,
        (expedition_id, user_id),
    ).fetchone()
    return row is not None


def _enrollment_open(database, expedition) -> bool:
    row = database.execute(
        """
        SELECT (
            (enrollment_opens_at IS NULL OR enrollment_opens_at <= now())
            AND (enrollment_closes_at IS NULL OR enrollment_closes_at > now())
        ) AS open
        FROM expeditions
        WHERE id = ?
        """,
        (expedition["id"],),
    ).fetchone()
    return bool(row["open"])


def _is_due(database, expedition) -> bool:
    row = database.execute(
        """
        SELECT (ends_at IS NOT NULL AND ends_at <= now()) AS due
        FROM expeditions
        WHERE id = ?
        """,
        (expedition["id"],),
    ).fetchone()
    return bool(row["due"])


def _managed_private(database, user_id: int, slug: str):
    expedition = _find_slug(database, slug)
    if expedition is None or expedition["kind"] != "private":
        raise ExpeditionError("not_found", MISSED, status=404)
    locked = _lock_expedition(database, expedition["id"])
    if locked["owner_user_id"] != user_id:
        raise _manage_denied(database, user_id, locked)
    return locked


def _manage_denied(database, user_id: int, expedition):
    if _access(database, user_id, expedition) == "none":
        return ExpeditionError("not_found", MISSED, status=404)
    return ExpeditionError("forbidden", "You cannot manage this expedition.", status=403)


def _profile(database, user_id: int, expedition) -> dict:
    route = database.execute(
        "SELECT slug, name FROM routes WHERE id = ?",
        (expedition["route_id"],),
    ).fetchone()
    participation = _participation_row(database, user_id, expedition["id"])
    open_enrollment = None
    if expedition["kind"] == "organization":
        open_enrollment = _enrollment_open(database, expedition)
    return {
        "id": expedition["id"],
        "slug": expedition["slug"],
        "name": expedition["name"],
        "description": expedition["description"],
        "kind": expedition["kind"],
        "status": expedition["status"],
        "visibility": expedition["visibility"],
        "route_slug": route["slug"],
        "route_name": route["name"],
        "enrollment": expedition["enrollment"],
        "enrollment_open": open_enrollment,
        "starts_at": expedition["starts_at"],
        "ends_at": expedition["ends_at"],
        "enrollment_opens_at": expedition["enrollment_opens_at"],
        "enrollment_closes_at": expedition["enrollment_closes_at"],
        "image_ref": expedition["image_ref"],
        "accent_color": expedition["accent_color"],
        "welcome_override": expedition["welcome_override"],
        "organization_id": expedition["organization_id"],
        "owner": expedition["owner_user_id"] == user_id,
        "participation_status": None if participation is None else participation["status"],
        "mode": "live" if expedition["status"] == "active" else "final",
    }


def _joined(database, expedition_id: int, participation, journey) -> dict:
    expedition = _expedition(database, expedition_id)
    return {
        "expedition": _profile(database, participation["user_id"], expedition),
        "participation": participation,
        "journey": journey,
    }


def _closed_body(database, expedition) -> dict:
    return {
        "skipped": False,
        "expedition": _profile(database, expedition["owner_user_id"] or 0, expedition),
        "entries": _final_entries(database, expedition["id"]),
    }


def _participation(database, participation_id: int) -> dict:
    row = database.execute(
        """
        SELECT id, user_id, expedition_id, route_id, journey_id, status,
               retained_read_access, final_standings_eligible,
               display_credits_after, joined_at
        FROM expedition_participations
        WHERE id = ?
        """,
        (participation_id,),
    ).fetchone()
    return dict(row)


def _participation_row(database, user_id: int, expedition_id: int):
    return database.execute(
        """
        SELECT id, user_id, journey_id, status, retained_read_access,
               final_standings_eligible, display_credits_after
        FROM expedition_participations
        WHERE user_id = ? AND expedition_id = ?
        """,
        (user_id, expedition_id),
    ).fetchone()


def _locked_participation(database, user_id: int, expedition_id: int):
    return database.execute(
        """
        SELECT id, journey_id, status
        FROM expedition_participations
        WHERE user_id = ? AND expedition_id = ?
        FOR UPDATE
        """,
        (user_id, expedition_id),
    ).fetchone()


def _find_slug(database, slug: str):
    if not isinstance(slug, str):
        return None
    return database.execute(
        "SELECT * FROM expeditions WHERE slug = ?",
        (slug,),
    ).fetchone()


def _expedition(database, expedition_id: int):
    return database.execute(
        "SELECT * FROM expeditions WHERE id = ?",
        (expedition_id,),
    ).fetchone()


def _locked_expedition(database, expedition_id: int):
    return _lock_expedition(database, expedition_id)


def _lock_expedition(database, expedition_id: int):
    row = database.execute(
        "SELECT * FROM expeditions WHERE id = ? FOR UPDATE",
        (expedition_id,),
    ).fetchone()
    if row is None:
        raise ExpeditionError("not_found", MISSED, status=404)
    return row


def _lock_route_expeditions(database, route_id: int) -> None:
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


def _lock_user(database, user_id: int) -> None:
    _require_account(database, user_id, lock=True)


def _lock_users(database, user_ids: list[int]) -> None:
    ordered = sorted({user_id for user_id in user_ids if user_id is not None})
    for user_id in ordered:
        _require_account(database, user_id, lock=False)
    if not ordered:
        return
    placeholders = ", ".join("?" for _ in ordered)
    found = database.execute(
        f"SELECT id, deletion_state FROM users WHERE id IN ({placeholders}) ORDER BY id FOR UPDATE",
        tuple(ordered),
    ).fetchall()
    if len(found) != len(ordered):
        raise ExpeditionError("not_found", "That account does not exist.", status=404)
    if any(row["deletion_state"] == "deleting" for row in found):
        raise ExpeditionError("forbidden", "This account is being deleted.", status=403)


def _lock_ids(database, table: str, ids: list[int]) -> None:
    ordered = sorted({value for value in ids if value is not None})
    if not ordered:
        return
    if table not in ("route_journeys", "expedition_participations"):
        raise ExpeditionError("invalid", "That lock is not available.")
    placeholders = ", ".join("?" for _ in ordered)
    database.execute(
        f"SELECT id FROM {table} WHERE id IN ({placeholders}) ORDER BY id FOR UPDATE",
        tuple(ordered),
    ).fetchall()


def _require_account(database, user_id: int, *, lock: bool = False) -> None:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ExpeditionError("not_found", "That account does not exist.", status=404)
    locking = " FOR UPDATE" if lock else ""
    row = database.execute(
        f"SELECT id, deletion_state FROM users WHERE id = ?{locking}",
        (user_id,),
    ).fetchone()
    if row is None:
        raise ExpeditionError("not_found", "That account does not exist.", status=404)
    if row["deletion_state"] == "deleting":
        raise ExpeditionError("forbidden", "This account is being deleted.", status=403)


def _published_route(database, slug: str):
    if not isinstance(slug, str) or not slug:
        raise ExpeditionError("not_found", "That route does not exist.", status=404)
    row = database.execute(
        """
        SELECT id, slug, status
        FROM routes
        WHERE slug = ? AND status = 'published'
        """,
        (slug,),
    ).fetchone()
    if row is None:
        raise ExpeditionError("not_found", "That route does not exist.", status=404)
    return row


def _user_by_name(database, username: str):
    key = " ".join((username or "").split()).casefold()
    row = database.execute(
        "SELECT id, username FROM users WHERE username_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        raise ExpeditionError("not_found", "No account uses that name.", status=404)
    return row


def _active_membership(database, user_id: int, slug: str, *, lock_user: bool):
    if lock_user:
        _lock_user(database, user_id)
    row = database.execute(
        """
        SELECT membership.organization_id, membership.role, membership.status,
               organization.status AS organization_status
        FROM organization_memberships AS membership
        JOIN organizations AS organization ON organization.id = membership.organization_id
        WHERE organization.slug = ? AND membership.user_id = ? AND membership.status = 'active'
        """,
        (slug, user_id),
    ).fetchone()
    if row is None:
        raise ExpeditionError("not_found", "That organization does not exist.", status=404)
    return row


def _managed_organization(database, user_id: int, slug: str):
    row = database.execute(
        """
        SELECT organization.id, organization.status, membership.role
        FROM organizations AS organization
        JOIN organization_memberships AS membership
          ON membership.organization_id = organization.id
         AND membership.user_id = ?
         AND membership.status = 'active'
        WHERE organization.slug = ?
        FOR UPDATE OF organization
        """,
        (user_id, slug),
    ).fetchone()
    if row is None or row["role"] not in ("owner", "admin"):
        raise ExpeditionError("not_found", "That organization does not exist.", status=404)
    if row["status"] != "active":
        raise ExpeditionError("invalid_state", "This organization is suspended.", status=409)
    return row


def _find_organization(database, slug: str):
    if not isinstance(slug, str):
        return None
    return database.execute(
        "SELECT id, slug, status FROM organizations WHERE slug = ?",
        (slug,),
    ).fetchone()


def _can_read_organization(database, user_id: int, organization_id: int) -> bool:
    if user_id in _admin_ids():
        return True
    row = database.execute(
        """
        SELECT 1 AS found
        FROM organization_memberships
        WHERE organization_id = ? AND user_id = ? AND status = 'active'
        """,
        (organization_id, user_id),
    ).fetchone()
    return row is not None


def _is_org_manager(database, user_id: int, organization_id: int | None) -> bool:
    if organization_id is None:
        return False
    row = database.execute(
        """
        SELECT 1 AS found
        FROM organization_memberships
        WHERE organization_id = ?
          AND user_id = ?
          AND status = 'active'
          AND role IN ('owner', 'admin')
        """,
        (organization_id, user_id),
    ).fetchone()
    return row is not None


def _organization_body(database, user_id: int, organization_id: int) -> dict:
    row = database.execute(
        """
        SELECT id, slug, name, description, status, logo_ref, image_ref,
               colors, welcome_message
        FROM organizations
        WHERE id = ?
        """,
        (organization_id,),
    ).fetchone()
    membership = database.execute(
        """
        SELECT role
        FROM organization_memberships
        WHERE organization_id = ? AND user_id = ? AND status = 'active'
        """,
        (organization_id, user_id),
    ).fetchone()
    body = dict(row)
    body["role"] = None if membership is None else membership["role"]
    return body


def _member_body(database, organization_id: int, member_id: int) -> dict:
    row = database.execute(
        """
        SELECT membership.user_id, account.username, profile.display_name,
               membership.role, membership.status
        FROM organization_memberships AS membership
        JOIN users AS account ON account.id = membership.user_id
        JOIN user_profiles AS profile ON profile.user_id = membership.user_id
        WHERE membership.organization_id = ? AND membership.user_id = ?
        """,
        (organization_id, member_id),
    ).fetchone()
    return dict(row)


def _active_organization_expeditions(slug: str) -> list[int]:
    database = get_db()
    try:
        rows = database.execute(
            """
            SELECT expedition.id
            FROM expeditions AS expedition
            JOIN organizations AS organization ON organization.id = expedition.organization_id
            WHERE organization.slug = ?
              AND expedition.kind = 'organization'
              AND expedition.status = 'active'
            ORDER BY expedition.id
            """,
            (slug,),
        ).fetchall()
        return [row["id"] for row in rows]
    finally:
        database.rollback()


def _mark_suspended(user_id: int, slug: str) -> bool:
    def write(database):
        _lock_user(database, user_id)
        organization = database.execute(
            "SELECT id, status FROM organizations WHERE slug = ? FOR UPDATE",
            (slug,),
        ).fetchone()
        if organization is None:
            raise ExpeditionError("not_found", "That organization does not exist.", status=404)
        pending = database.execute(
            """
            SELECT id
            FROM expeditions
            WHERE organization_id = ? AND kind = 'organization' AND status = 'active'
            ORDER BY id
            """,
            (organization["id"],),
        ).fetchall()
        if pending:
            return {"suspended": False}, False
        database.execute(
            "UPDATE organizations SET status = 'suspended' WHERE id = ?",
            (organization["id"],),
        )
        return {"suspended": True}, True

    return _commit(write)["suspended"]


def _admin_ids() -> set[int]:
    configured = current_app.config.get("PLATFORM_ADMIN_IDS", None)
    if configured is None:
        configured = os.environ.get("PLATFORM_ADMIN_IDS", "")
    if isinstance(configured, str):
        values = [item.strip() for item in configured.split(",") if item.strip()]
    else:
        values = list(configured)
    return {int(value) for value in values}


def _require_platform_admin(user_id: int) -> None:
    if user_id not in _admin_ids():
        raise ExpeditionError("forbidden", "You cannot manage organizations.", status=403)


def _clean_slug(slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug in _RESERVED or len(slug) > 48:
        raise ExpeditionError("invalid", "Use a lowercase slug of letters, numbers, and hyphens.")
    return slug


def _clean_name(name: str) -> str:
    cleaned = " ".join((name or "").split()) if isinstance(name, str) else ""
    if not 2 <= len(cleaned) <= 80:
        raise ExpeditionError("invalid", "Use a name of 2 to 80 characters.")
    return cleaned


def _clean_description(description: str) -> str:
    cleaned = " ".join((description or "").split()) if isinstance(description, str) else ""
    if len(cleaned) > 500:
        raise ExpeditionError("invalid", "Keep the description under 500 characters.")
    return cleaned


def _require_aware(value) -> None:
    if value is None:
        return
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ExpeditionError("invalid", "Use a timezone-aware time.")


def _is_past(database, expires_at) -> bool:
    row = database.execute("SELECT ? <= now() AS past", (expires_at,)).fetchone()
    return bool(row["past"])


def _new_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw, digest


def _from_journey(exc: JourneyError) -> ExpeditionError:
    http_status = {
        "not_found": 404,
        "forbidden": 403,
        "restart_required": 409,
        "invalid_state": 409,
        "conflict": 409,
    }.get(exc.code, 400)
    details = dict(exc.details)
    journey_status = details.pop("status", None)
    error = ExpeditionError(exc.code, str(exc), status=http_status, **details)
    if journey_status is not None:
        error.details["status"] = journey_status
    return error


def _commit(write):
    database = get_db()
    try:
        result, changed = write(database)
        if changed:
            database.commit()
        else:
            database.rollback()
    except Exception as exc:
        _rollback_preserving(database, exc)
        if isinstance(exc, ExpeditionError):
            raise
        if isinstance(exc, JourneyError):
            raise _from_journey(exc) from exc
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
