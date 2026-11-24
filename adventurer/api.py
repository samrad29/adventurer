"""Versioned JSON API.

Handlers validate the payload, take the acting user from the session or
bearer token, and call the domain services. They do not decide journey
progress or expedition standings.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, g, jsonify, request, session

from . import csrf_token, limits
from .activities import (
    ActivityError,
    create_activity,
    delete_activity,
    get_activity,
    list_activities,
    preview_activity,
    preview_activity_update,
    update_activity,
)
from .branding import resolve_branding
from .db import get_db
from .expeditions import (
    ExpeditionError,
    accept_invitation,
    add_organization_member,
    archive_private_expedition,
    assign_organization_owner,
    complete_organization_expedition,
    create_organization,
    create_organization_expedition,
    create_private_expedition,
    create_share_link,
    create_username_invitation,
    decline_invitation,
    extend_expedition,
    get_expedition,
    get_leaderboard,
    get_organization,
    get_roster,
    get_updates,
    join_expedition,
    leave_expedition,
    list_expedition_invitations,
    list_organization_expeditions,
    list_organization_members,
    list_organizations,
    list_received_invitations,
    remove_organization_member,
    remove_participation_privacy,
    revoke_invitation,
    suspend_organization,
    transfer_ownership,
    update_expedition_branding,
    update_organization_branding,
    update_organization_role,
    update_private_expedition,
)
from .home import compose_home, plain_display, progress_metrics
from .metrics import assess_metric
from .journeys import (
    JourneyError,
    abandon_journey,
    active_journey,
    get_journey,
    list_journeys,
    restart_journey,
    resume_journey,
    start_journey,
    switch_journey,
)
from .narrative import (
    NarrativeError,
    acknowledge_arrival,
    chapters_for_routes,
    journal_story,
    journal_timeline,
)
from .services import AccountError, authenticate, create_adventurer, current_viewer, get_adventurer
from .tokens import issue_token, revoke_all, revoke_token

bp = Blueprint("api", __name__, url_prefix="/api/v1")

_JOURNEY_STATUS = {
    "not_found": 404,
    "forbidden": 403,
    "restart_required": 409,
    "invalid_state": 409,
    "conflict": 409,
    "invalid": 422,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, **details) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def init_app(app) -> None:
    app.register_error_handler(ApiError, _render_api)
    app.register_error_handler(JourneyError, _render_journey)
    app.register_error_handler(ActivityError, _render_service)
    app.register_error_handler(ExpeditionError, _render_service)
    app.register_error_handler(NarrativeError, _render_service)


@bp.get("/session")
def session_view():
    if g.get("auth_method") == "bearer":
        return _ok(csrf=None, you=_you(get_adventurer(g.actor_id)))
    return _ok(csrf=csrf_token(), you=_you(current_viewer()))


@bp.post("/auth/register")
def register():
    body = _object()
    name = body.get("name") if isinstance(body.get("name"), str) else ""
    password = body.get("password") if isinstance(body.get("password"), str) else ""
    key = _limit_key(name)
    if limits.too_many(key):
        raise ApiError("rate_limited", "Try again later.", status=429)
    try:
        _require_invite(body)
        viewer = create_adventurer(name, password)
    except ApiError:
        limits.record(key)
        raise
    except AccountError as exc:
        limits.record(key)
        code = "conflict" if "already" in str(exc) else "invalid"
        status = 409 if code == "conflict" else 400
        raise ApiError(code, str(exc), status=status) from exc
    limits.clear(key)
    token = issue_token(viewer["id"])
    _start_cookie(viewer["id"])
    return _ok(you=_you(viewer), token=token, csrf=session["csrf"]), 201


@bp.post("/auth/login")
def login():
    body = _object()
    name = body.get("name") if isinstance(body.get("name"), str) else ""
    password = body.get("password") if isinstance(body.get("password"), str) else ""
    key = _limit_key(name)
    if limits.too_many(key):
        raise ApiError("rate_limited", "Try again later.", status=429)
    viewer = authenticate(name, password)
    if viewer is None:
        limits.record(key)
        raise ApiError("unauthenticated", "That name or password is not right.", status=401)
    limits.clear(key)
    token = issue_token(viewer["id"])
    _start_cookie(viewer["id"])
    return _ok(you=_you(viewer), token=token, csrf=session["csrf"])


@bp.post("/auth/logout")
def logout():
    if g.get("auth_method") == "bearer":
        raw = _bearer_value()
        if raw and g.get("actor_id"):
            revoke_token(raw, g.actor_id)
        return _ok(ok=True)
    if not g.get("actor_id"):
        raise ApiError("unauthenticated", "Log in first.", status=401)
    session.clear()
    return _ok(ok=True)


@bp.post("/auth/revoke")
def revoke():
    revoke_all(_actor())
    return _ok(ok=True)


@bp.get("/home")
def home():
    try:
        return _ok(**compose_home(_actor()))
    except JourneyError as exc:
        if exc.code == "forbidden" and "being deleted" in str(exc):
            raise ApiError("unauthenticated", "Log in first.", status=401) from exc
        raise


@bp.get("/journeys")
def journeys_list():
    return _ok(journeys=_with_metrics(list_journeys(_actor())))


@bp.get("/journeys/active")
def journeys_active():
    journey = active_journey(_actor())
    return _ok(journey=None if journey is None else _with_metrics([journey])[0])


@bp.get("/journeys/<int:journey_id>")
def journeys_get(journey_id: int):
    return _ok(journey=_with_metrics([get_journey(_actor(), journey_id)])[0])


@bp.get("/journeys/<int:journey_id>/journal")
def journeys_journal(journey_id: int):
    return _ok(**journal_timeline(_actor(), journey_id))


@bp.get("/journeys/<int:journey_id>/journal/<checkpoint_slug>")
def journeys_journal_story(journey_id: int, checkpoint_slug: str):
    return _ok(**journal_story(_actor(), journey_id, checkpoint_slug))


@bp.post("/journeys/<int:journey_id>/journal/<checkpoint_slug>/acknowledgment")
def journeys_journal_acknowledge(journey_id: int, checkpoint_slug: str):
    return _ok(**acknowledge_arrival(_actor(), journey_id, checkpoint_slug))


@bp.post("/journeys/start")
def journeys_start():
    return _ok(journey=start_journey(_actor(), _route_id(_object()))), 201


@bp.post("/journeys/switch")
def journeys_switch():
    return _ok(journey=switch_journey(_actor(), _route_id(_object())))


@bp.post("/journeys/resume")
def journeys_resume():
    return _ok(journey=resume_journey(_actor(), _route_id(_object())))


@bp.post("/journeys/abandon")
def journeys_abandon():
    return _ok(journey=abandon_journey(_actor(), _route_id(_object())))


@bp.post("/journeys/restart")
def journeys_restart():
    return _ok(**restart_journey(_actor(), _route_id(_object())))


@bp.get("/activities")
def activities_list():
    return _ok(activities=list_activities(_actor()))


@bp.get("/activities/<int:activity_id>")
def activities_get(activity_id: int):
    return _ok(activity=get_activity(_actor(), activity_id))


@bp.post("/activities/preview")
def activities_preview():
    return _ok(preview=preview_activity(_actor(), _activity_fields(_object())))


@bp.post("/activities")
def activities_create():
    body = _object()
    saved = create_activity(
        _actor(),
        _activity_fields(body),
        confirmation_token=body.get("confirmation_token"),
    )
    return _ok(**saved), 201


@bp.post("/activities/<int:activity_id>/preview")
def activities_preview_update(activity_id: int):
    return _ok(
        preview=preview_activity_update(_actor(), activity_id, _activity_fields(_object(), creating=False))
    )


@bp.patch("/activities/<int:activity_id>")
def activities_update(activity_id: int):
    body = _object()
    saved = update_activity(
        _actor(),
        activity_id,
        _activity_fields(body, creating=False),
        confirmation_token=body.get("confirmation_token"),
    )
    return _ok(**saved)


@bp.delete("/activities/<int:activity_id>")
def activities_delete(activity_id: int):
    return _ok(**delete_activity(_actor(), activity_id))


@bp.get("/routes")
def routes_list():
    database = get_db()
    rows = database.execute(
        """
        SELECT id, slug, name, region, summary
        FROM routes
        WHERE status = 'published'
        ORDER BY slug
        """
    ).fetchall()
    route_ids = [row["id"] for row in rows]
    requirements = _requirements_by_route(database, route_ids)
    chapters = chapters_for_routes(database, route_ids)
    database.rollback()
    return _ok(
        routes=[
            {
                "slug": row["slug"],
                "name": row["name"],
                "region": row["region"],
                "summary": row["summary"],
                "requirements": requirements.get(row["id"], []),
                "chapters": chapters.get(row["id"], []),
            }
            for row in rows
        ]
    )


@bp.get("/routes/<slug>")
def routes_get(slug: str):
    database = get_db()
    route = database.execute(
        """
        SELECT id, slug, name, region, summary
        FROM routes
        WHERE slug = ? AND status = 'published'
        """,
        (slug,),
    ).fetchone()
    if route is None:
        database.rollback()
        raise ApiError("not_found", "That route does not exist.", status=404)
    checkpoints = database.execute(
        """
        SELECT id, slug, name, position
        FROM route_checkpoints
        WHERE route_id = ?
        ORDER BY position, id
        """,
        (route["id"],),
    ).fetchall()
    expeditions = database.execute(
        """
        SELECT expedition.slug, expedition.name, expedition.kind,
               expedition.status, expedition.visibility,
               expedition.accent_color, expedition.welcome_override,
               expedition.image_ref,
               organization.logo_ref,
               organization.image_ref AS organization_image_ref,
               organization.colors, organization.welcome_message
        FROM expeditions AS expedition
        LEFT JOIN organizations AS organization
          ON organization.id = expedition.organization_id
        WHERE expedition.route_id = ?
          AND expedition.status = 'active'
          AND (
            expedition.kind = 'global'
            OR (
              expedition.kind = 'organization'
              AND expedition.enrollment = 'open'
              AND organization.status = 'active'
              AND (expedition.enrollment_opens_at IS NULL OR expedition.enrollment_opens_at <= now())
              AND (expedition.enrollment_closes_at IS NULL OR expedition.enrollment_closes_at > now())
            )
          )
        ORDER BY expedition.slug
        """,
        (route["id"],),
    ).fetchall()
    requirements = _requirements_by_route(database, [route["id"]]).get(route["id"], [])
    checkpoint_bodies = _checkpoint_catalog(database, checkpoints)
    chapters = chapters_for_routes(database, [route["id"]]).get(route["id"], [])
    database.rollback()
    journeys = []
    if g.get("actor_id"):
        journeys = _with_metrics(list_journeys(g.actor_id, route["id"]))
    return _ok(
        slug=route["slug"],
        name=route["name"],
        region=route["region"],
        summary=route["summary"],
        requirements=requirements,
        checkpoints=checkpoint_bodies,
        chapters=chapters,
        expeditions=[_enrollment_expedition(row) for row in expeditions],
        journeys=journeys,
    )


@bp.get("/expeditions/<slug>")
def expeditions_get(slug: str):
    return _ok(expedition=get_expedition(_actor(), slug))


@bp.post("/expeditions")
def expeditions_create():
    body = _object()
    created = create_private_expedition(
        _actor(),
        slug=_text(body, "slug"),
        name=_text(body, "name"),
        description=_text(body, "description", required=False),
        route_slug=_text(body, "route_slug"),
        confirm_new_attempt=_flag(body, "confirm_new_attempt"),
    )
    return _ok(**created), 201


@bp.patch("/expeditions/<slug>/branding")
def expeditions_branding(slug: str):
    return _ok(expedition=update_expedition_branding(_actor(), slug, _object()))


@bp.patch("/expeditions/<slug>")
def expeditions_update(slug: str):
    body = _object()
    return _ok(
        expedition=update_private_expedition(
            _actor(),
            slug,
            name=body.get("name"),
            description=body.get("description"),
        )
    )


@bp.post("/expeditions/<slug>/archive")
def expeditions_archive(slug: str):
    return _ok(**archive_private_expedition(_actor(), slug))


@bp.post("/expeditions/<slug>/join")
def expeditions_join(slug: str):
    body = _object()
    return _ok(**join_expedition(_actor(), slug, confirm_new_attempt=_flag(body, "confirm_new_attempt")))


@bp.post("/expeditions/<slug>/leave")
def expeditions_leave(slug: str):
    return _ok(participation=leave_expedition(_actor(), slug))


@bp.post("/expeditions/<slug>/owner")
def expeditions_owner(slug: str):
    body = _object()
    username = _text(body, "username")
    person = _user_id(username)
    return _ok(expedition=transfer_ownership(_actor(), slug, person))


@bp.post("/expeditions/<slug>/extend")
def expeditions_extend(slug: str):
    return _ok(expedition=extend_expedition(_actor(), slug, _aware(_object(), "ends_at")))


@bp.post("/expeditions/<slug>/complete")
def expeditions_complete(slug: str):
    return _ok(**complete_organization_expedition(_actor(), slug))


@bp.post("/expeditions/<slug>/privacy-removal")
def expeditions_privacy(slug: str):
    body = _object()
    if body.get("confirm") is not True:
        raise ApiError("invalid", "Confirmation is required.", status=422)
    return _ok(**remove_participation_privacy(_actor(), slug, confirm=True))


@bp.get("/expeditions/<slug>/roster")
def expeditions_roster(slug: str):
    return _ok(roster=get_roster(_actor(), slug))


@bp.get("/expeditions/<slug>/leaderboard")
def expeditions_leaderboard(slug: str):
    return _ok(**get_leaderboard(_actor(), slug))


@bp.get("/expeditions/<slug>/updates")
def expeditions_updates(slug: str):
    return _ok(updates=get_updates(_actor(), slug))


@bp.get("/expeditions/<slug>/invitations")
def expeditions_invitations(slug: str):
    return _ok(invitations=list_expedition_invitations(_actor(), slug))


@bp.post("/expeditions/<slug>/invitations")
def expeditions_invite(slug: str):
    body = _object()
    username = body.get("username")
    kind = body.get("kind")
    if isinstance(username, str) and username.strip() and kind not in (None, "username"):
        raise ApiError("invalid", "Send a username or a share link, not both.")
    if isinstance(username, str) and username.strip():
        return _ok(**create_username_invitation(_actor(), slug, username)), 201
    if kind == "link":
        return _ok(**create_share_link(_actor(), slug)), 201
    raise ApiError("invalid", "Send a username or kind link.")


@bp.get("/invitations")
def invitations_list():
    return _ok(invitations=list_received_invitations(_actor()))


@bp.post("/invitations/accept")
def invitations_accept_token():
    body = _object()
    return _ok(
        **accept_invitation(
            _actor(),
            token=_text(body, "token"),
            confirm_new_attempt=_flag(body, "confirm_new_attempt"),
        )
    )


@bp.post("/invitations/<int:invitation_id>/accept")
def invitations_accept(invitation_id: int):
    body = _object()
    return _ok(
        **accept_invitation(
            _actor(),
            invitation_id=invitation_id,
            confirm_new_attempt=_flag(body, "confirm_new_attempt"),
        )
    )


@bp.post("/invitations/<int:invitation_id>/decline")
def invitations_decline(invitation_id: int):
    return _ok(**decline_invitation(_actor(), invitation_id))


@bp.post("/invitations/<int:invitation_id>/revoke")
def invitations_revoke(invitation_id: int):
    return _ok(**revoke_invitation(_actor(), invitation_id))


@bp.get("/organizations")
def organizations_list():
    return _ok(organizations=list_organizations(_actor()))


@bp.post("/organizations")
def organizations_create():
    body = _object()
    created = create_organization(
        _actor(),
        slug=_text(body, "slug"),
        name=_text(body, "name"),
        owner_username=_text(body, "owner_username"),
        description=_text(body, "description", required=False),
    )
    return _ok(organization=created), 201


@bp.get("/organizations/<slug>")
def organizations_get(slug: str):
    return _ok(organization=get_organization(_actor(), slug))


@bp.get("/organizations/<slug>/members")
def organizations_members(slug: str):
    return _ok(members=list_organization_members(_actor(), slug))


@bp.post("/organizations/<slug>/members")
def organizations_add_member(slug: str):
    body = _object()
    role = body.get("role", "member")
    if not isinstance(role, str):
        raise ApiError("invalid", "Choose a member or admin role.")
    return _ok(
        member=add_organization_member(_actor(), slug, _text(body, "username"), role=role)
    ), 201


@bp.patch("/organizations/<slug>/members")
def organizations_role(slug: str):
    body = _object()
    role = body.get("role")
    if not isinstance(role, str):
        raise ApiError("invalid", "Choose a member or admin role.")
    return _ok(member=update_organization_role(_actor(), slug, _text(body, "username"), role))


@bp.delete("/organizations/<slug>/members")
def organizations_remove_member(slug: str):
    return _ok(**remove_organization_member(_actor(), slug, _text(_object(), "username")))


@bp.patch("/organizations/<slug>/branding")
def organizations_branding(slug: str):
    return _ok(organization=update_organization_branding(_actor(), slug, _object()))


@bp.get("/organizations/<slug>/expeditions")
def organizations_expeditions(slug: str):
    return _ok(expeditions=list_organization_expeditions(_actor(), slug))


@bp.post("/organizations/<slug>/expeditions")
def organizations_create_expedition(slug: str):
    body = _object()
    created = create_organization_expedition(
        _actor(),
        slug,
        slug=_text(body, "slug"),
        name=_text(body, "name"),
        route_slug=_text(body, "route_slug"),
        enrollment=_text(body, "enrollment"),
        description=_text(body, "description", required=False),
        starts_at=_optional_aware(body, "starts_at"),
        ends_at=_optional_aware(body, "ends_at"),
        enrollment_opens_at=_optional_aware(body, "enrollment_opens_at"),
        enrollment_closes_at=_optional_aware(body, "enrollment_closes_at"),
        welcome_override=body.get("welcome_override"),
        accent_color=body.get("accent_color"),
        image_ref=body.get("image_ref"),
    )
    return _ok(expedition=created), 201


@bp.post("/admin/organizations/<slug>/owner")
def admin_owner(slug: str):
    return _ok(organization=assign_organization_owner(_actor(), slug, _text(_object(), "username")))


@bp.post("/admin/organizations/<slug>/suspend")
def admin_suspend(slug: str):
    return _ok(organization=suspend_organization(_actor(), slug))


def _enrollment_expedition(row) -> dict:
    organization = None
    if row["kind"] == "organization":
        organization = {
            "logo_ref": row["logo_ref"],
            "image_ref": row["organization_image_ref"],
            "colors": row["colors"],
            "welcome_message": row["welcome_message"],
        }
    return {
        "slug": row["slug"],
        "name": row["name"],
        "kind": row["kind"],
        "status": row["status"],
        "visibility": row["visibility"],
        "branding": resolve_branding(
            kind=row["kind"], organization=organization, expedition=row
        ),
    }


def _actor() -> int:
    user_id = g.get("actor_id")
    if not user_id:
        raise ApiError("unauthenticated", "Log in first.", status=401)
    return user_id


def _object() -> dict:
    if request.content_length in (0, None) and not request.data:
        return {}
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiError("invalid", "Send a JSON object.")
    return body


def _text(body: dict, key: str, *, required: bool = True) -> str:
    value = body.get(key, "" if not required else None)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ApiError("invalid", f"Send {key} as text.")
    if required and not value.strip():
        raise ApiError("invalid", f"Send {key}.")
    return value


def _flag(body: dict, key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise ApiError("invalid", f"Send {key} as true or false.")
    return value


def _aware(body: dict, key: str) -> datetime:
    value = _optional_aware(body, key)
    if value is None:
        raise ApiError("invalid", f"Send {key}.")
    return value


def _optional_aware(body: dict, key: str) -> datetime | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError("invalid", f"Send {key} as a timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError("invalid", f"Send {key} as a timestamp.") from exc
    if parsed.tzinfo is None:
        raise ApiError("invalid", f"Send {key} with a timezone.")
    return parsed


def _activity_fields(body: dict, *, creating: bool = True) -> dict:
    """Measured fields, minus the confirmation token.

    A create or an update may send elevation, distance, or both. The activity
    service decides which omissions are measurements and which stay unchanged.
    """
    del creating
    return {
        key: value
        for key, value in body.items()
        if key != "confirmation_token"
    }


def _with_metrics(journeys: list[dict]) -> list[dict]:
    if not journeys:
        return []
    database = get_db()
    try:
        return [
            {**dict(row), "metrics": progress_metrics(database, row["id"])}
            for row in journeys
        ]
    finally:
        database.rollback()


def _requirements_by_route(database, route_ids: list[int]) -> dict[int, list[dict]]:
    if not route_ids:
        return {}
    grouped: dict[int, list[dict]] = {}
    rows = database.execute(
        """
        SELECT route_id, metric_code, target_amount
        FROM route_requirements
        WHERE route_id = ANY(?)
        ORDER BY route_id, metric_code
        """,
        (route_ids,),
    ).fetchall()
    for row in rows:
        shown = assess_metric(row["metric_code"], row["target_amount"], Decimal("0"))
        grouped.setdefault(row["route_id"], []).append(
            {
                "metric": row["metric_code"],
                "unit": shown.display_unit,
                "target": plain_display(shown.target_display),
            }
        )
    return grouped


def _checkpoint_catalog(database, checkpoints) -> list[dict]:
    if not checkpoints:
        return []
    thresholds: dict[int, list[dict]] = {}
    rows = database.execute(
        """
        SELECT checkpoint_id, metric_code, threshold_amount
        FROM checkpoint_requirements
        WHERE checkpoint_id = ANY(?)
        ORDER BY metric_code
        """,
        ([row["id"] for row in checkpoints],),
    ).fetchall()
    for row in rows:
        shown = assess_metric(row["metric_code"], row["threshold_amount"], Decimal("0"))
        thresholds.setdefault(row["checkpoint_id"], []).append(
            {
                "metric": row["metric_code"],
                "unit": shown.display_unit,
                "threshold": plain_display(shown.target_display),
            }
        )
    return [
        {
            "slug": row["slug"],
            "name": row["name"],
            "position": row["position"],
            "requirements": thresholds.get(row["id"], []),
        }
        for row in checkpoints
    ]


def _route_id(body: dict) -> int:
    slug = _text(body, "route_slug")
    row = get_db().execute(
        "SELECT id FROM routes WHERE slug = ? AND status = 'published'",
        (slug,),
    ).fetchone()
    get_db().rollback()
    if row is None:
        raise JourneyError("not_found", "That route does not exist.")
    return row["id"]


def _user_id(username: str) -> int:
    key = " ".join(username.split()).casefold()
    row = get_db().execute(
        "SELECT id FROM users WHERE username_key = ?",
        (key,),
    ).fetchone()
    get_db().rollback()
    if row is None:
        raise ExpeditionError("not_found", "No account uses that name.", status=404)
    return row["id"]


def _you(viewer) -> dict | None:
    if viewer is None:
        return None
    return {"id": viewer["id"], "name": viewer["name"]}


def _start_cookie(user_id: int) -> None:
    session.clear()
    session["adventurer_id"] = user_id
    session.permanent = True
    session["csrf"] = csrf_token()


def _require_invite(body: dict) -> None:
    from flask import current_app

    expected = current_app.config.get("INVITE_CODE") or ""
    if not expected:
        return
    submitted = body.get("invite") if isinstance(body.get("invite"), str) else ""
    if not hmac.compare_digest(submitted.strip(), expected):
        raise ApiError("invalid", "That invite code is not valid.", status=400)


def _limit_key(name: str) -> str:
    return f"{request.remote_addr or ''}\n{' '.join(name.split()).casefold()}"


def _bearer_value() -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return ""
    return header[7:].strip()


def _ok(**payload):
    return jsonify(_json_ready(payload))


def _rollback() -> None:
    try:
        get_db().rollback()
    except Exception:
        return


def _render_api(exc: ApiError):
    _rollback()
    body = {"error": str(exc), "code": exc.code}
    if exc.details:
        body["details"] = _json_ready(exc.details)
    return jsonify(body), exc.status


def _render_journey(exc: JourneyError):
    if request.path == "/api/v1/home" and exc.code == "forbidden" and "being deleted" in str(exc):
        exc = ApiError("unauthenticated", "Log in first.", status=401)
        return _render_api(exc)
    return _render_known(exc, _JOURNEY_STATUS.get(exc.code, 400))


def _render_service(exc):
    return _render_known(exc, exc.status)


def _render_known(exc, status: int):
    _rollback()
    body = {"error": str(exc), "code": exc.code}
    details = getattr(exc, "details", None) or {}
    if details:
        body["details"] = _json_ready(details)
    return jsonify(body), status


def _json_ready(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    return value
