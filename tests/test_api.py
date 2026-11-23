"""HTTP coverage for the versioned API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from adventurer import limits
from adventurer.db import get_db
from adventurer.measures import feet_to_meters
from adventurer.scene import select_scene

DAY = "2026-01-02"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    limits.clear()
    yield
    limits.clear()


def _register(client, name="Sam", password="trailhead", *, cookie=False):
    response = client.post(
        "/api/v1/auth/register",
        json={"name": name, "password": password},
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    if not cookie:
        client.delete_cookie("session")
    return body["you"]["id"], body["token"], body["csrf"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _check_route_shape(database) -> None:
    database.execute(
        """
        SET CONSTRAINTS
            adventurer.route_progress_shape_routes,
            adventurer.route_progress_shape_requirements,
            adventurer.route_progress_shape_checkpoints,
            adventurer.route_progress_shape_checkpoint_requirements
        IMMEDIATE
        """
    )


def _route(slug: str, feet: str = "1000") -> int:
    database = get_db()
    target = feet_to_meters(Decimal(feet))
    route_id = database.execute(
        """
        INSERT INTO routes (slug, name, region, summary, status)
        VALUES (?, ?, 'Test', 'A test route', 'draft')
        RETURNING id
        """,
        (slug, slug),
    ).fetchone()["id"]
    database.execute(
        """
        INSERT INTO route_requirements (route_id, metric_code, target_amount)
        VALUES (?, 'elevation', ?)
        """,
        (route_id, target),
    )
    checkpoint_id = database.execute(
        """
        INSERT INTO route_checkpoints (route_id, slug, name, position)
        VALUES (?, 'summit', 'Summit', 2)
        RETURNING id
        """,
        (route_id,),
    ).fetchone()["id"]
    database.execute(
        """
        INSERT INTO checkpoint_requirements (
            checkpoint_id, route_id, metric_code, threshold_amount
        )
        VALUES (?, ?, 'elevation', ?)
        """,
        (checkpoint_id, route_id, target),
    )
    database.execute("UPDATE routes SET status = 'published' WHERE id = ?", (route_id,))
    _check_route_shape(database)
    database.commit()
    return route_id


def _checkpoint(route_id: int, slug: str, feet: str, position: int) -> None:
    database = get_db()
    checkpoint_id = database.execute(
        """
        INSERT INTO route_checkpoints (route_id, slug, name, position)
        VALUES (?, ?, ?, ?)
        RETURNING id
        """,
        (route_id, slug, slug, position),
    ).fetchone()["id"]
    database.execute(
        """
        INSERT INTO checkpoint_requirements (
            checkpoint_id, route_id, metric_code, threshold_amount
        )
        VALUES (?, ?, 'elevation', ?)
        """,
        (checkpoint_id, route_id, feet_to_meters(Decimal(feet))),
    )
    _check_route_shape(database)
    database.commit()


def _count(table: str, where: str = "TRUE", params: tuple = ()) -> int:
    row = get_db().execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE {where}",
        params,
    ).fetchone()
    return row["count"]


def test_scene_never_uses_an_unreached_checkpoint(tmp_path: Path):
    checkpoints = [
        {"slug": "low", "elevation_m": Decimal("10"), "position": 1},
        {"slug": "high", "elevation_m": Decimal("50"), "position": 2},
    ]
    (tmp_path / "ridge").mkdir()
    (tmp_path / "ridge" / "hero.jpg").write_bytes(b"hero")
    (tmp_path / "ridge" / "low.jpg").write_bytes(b"low")
    (tmp_path / "high.jpg").write_bytes(b"locked")
    below = select_scene("ridge", checkpoints, Decimal("0"), tmp_path)
    assert below["role"] == "route_hero"
    assert below["checkpoint_slug"] is None
    assert below["image"]["path"].endswith("/ridge/hero.jpg")
    assert "high" not in below["image"]["path"]
    reached = select_scene("ridge", checkpoints, Decimal("10"), tmp_path)
    assert reached["role"] == "checkpoint"
    assert reached["checkpoint_slug"] == "low"
    assert reached["fallback"] is None
    missing = select_scene(
        "ridge",
        [{"slug": "bare", "elevation_m": Decimal("40"), "position": 1}, checkpoints[0]],
        Decimal("40"),
        tmp_path,
    )
    assert missing["checkpoint_slug"] == "bare"
    assert missing["image"]["path"].endswith("/ridge/low.jpg")
    assert missing["fallback"] == "lower_checkpoint"
    assert "high" not in missing["image"]["path"]


def test_unauthenticated_and_bad_bearer_do_not_use_the_cookie(client):
    assert client.get("/api/v1/home").status_code == 401
    _id, token, csrf = _register(client)
    stolen = client.get(
        "/api/v1/home",
        headers={"Authorization": "Bearer not-a-token", "X-CSRF-Token": csrf},
    )
    assert stolen.status_code == 401
    assert stolen.get_json()["code"] == "unauthenticated"
    opened = client.get("/api/v1/home", headers=_auth(token))
    assert opened.status_code == 200
    assert opened.get_json()["state"] == "ready"


def test_cookie_writes_require_csrf_and_bearer_does_not(client):
    _id, token, csrf = _register(client, cookie=True)
    missing = client.post("/api/v1/journeys/start", json={"route_slug": "everest"})
    assert missing.status_code == 400
    assert missing.get_json()["code"] == "csrf"
    started = client.post(
        "/api/v1/journeys/start",
        json={"route_slug": "everest"},
        headers={"X-CSRF-Token": csrf},
    )
    assert started.status_code == 201
    assert started.get_json()["journey"]["status"] == "active"
    client.delete_cookie("session")
    other = client.post(
        "/api/v1/auth/register",
        json={"name": "Rita", "password": "trailhead"},
    )
    rita = other.get_json()["token"]
    denied = client.get("/api/v1/journeys", headers=_auth(rita))
    assert denied.get_json()["journeys"] == []
    via_bearer = client.post(
        "/api/v1/journeys/start",
        json={"route_slug": "everest"},
        headers=_auth(rita),
    )
    assert via_bearer.status_code == 201


def test_cross_user_journey_and_activity_are_rejected(client):
    _sam, sam, _csrf = _register(client, "Sam")
    _rita, rita, _csrf = _register(client, "Rita")
    started = client.post(
        "/api/v1/journeys/start",
        json={"route_slug": "everest"},
        headers=_auth(sam),
    )
    journey_id = started.get_json()["journey"]["id"]
    foreign = client.get(f"/api/v1/journeys/{journey_id}", headers=_auth(rita))
    assert foreign.status_code == 403
    saved = client.post(
        "/api/v1/activities",
        json={"elevation_ft": "20", "happened_on": DAY},
        headers=_auth(sam),
    )
    activity_id = saved.get_json()["activity"]["id"]
    hidden = client.get(f"/api/v1/activities/{activity_id}", headers=_auth(rita))
    assert hidden.status_code == 403
    assert hidden.get_json()["code"] == "forbidden"


def test_journey_lifecycle_and_get_does_not_restart(client, app):
    with app.app_context():
        _route("kili", "1000")
        _route("denali", "1000")
    _id, token, _csrf = _register(client)
    headers = _auth(token)
    started = client.post("/api/v1/journeys/start", json={"route_slug": "kili"}, headers=headers)
    assert started.status_code == 201
    journey_id = started.get_json()["journey"]["id"]
    listed = client.get("/api/v1/journeys", headers=headers)
    assert listed.get_json()["journeys"][0]["id"] == journey_id
    active = client.get("/api/v1/journeys/active", headers=headers)
    assert active.get_json()["journey"]["status"] == "active"
    before = client.get(f"/api/v1/journeys/{journey_id}", headers=headers).get_json()["journey"]
    again = client.get(f"/api/v1/journeys/{journey_id}", headers=headers).get_json()["journey"]
    assert again["status"] == before["status"] == "active"
    assert again["attempt_number"] == 1
    switched = client.post("/api/v1/journeys/switch", json={"route_slug": "denali"}, headers=headers)
    assert switched.get_json()["journey"]["route_slug"] == "denali"
    assert switched.get_json()["journey"]["status"] == "active"
    resumed = client.post("/api/v1/journeys/resume", json={"route_slug": "kili"}, headers=headers)
    assert resumed.get_json()["journey"]["status"] == "active"
    abandoned = client.post("/api/v1/journeys/abandon", json={"route_slug": "kili"}, headers=headers)
    assert abandoned.get_json()["journey"]["status"] == "abandoned"
    blocked = client.post("/api/v1/journeys/switch", json={"route_slug": "kili"}, headers=headers)
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "restart_required"
    with app.app_context():
        assert _count("route_journeys", "user_id = ? AND route_id = (SELECT id FROM routes WHERE slug = 'kili')", (_id,)) == 1
    restarted = client.post("/api/v1/journeys/restart", json={"route_slug": "kili"}, headers=headers)
    assert restarted.status_code == 200
    assert restarted.get_json()["journey"]["attempt_number"] == 2
    assert restarted.get_json()["journey"]["status"] == "active"


def test_activity_confirmation_edit_and_completed_protection(client, app):
    with app.app_context():
        route_id = _route("summit-ridge", "100")
        _checkpoint(route_id, "top", "100", 1)
    _id, token, _csrf = _register(client)
    headers = _auth(token)
    created = client.post(
        "/api/v1/expeditions",
        json={"slug": "summit-party", "name": "Summit Party", "route_slug": "summit-ridge"},
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()
    preview = client.post(
        "/api/v1/activities/preview",
        json={"elevation_ft": "40", "happened_on": DAY},
        headers=headers,
    )
    assert preview.get_json()["preview"]["will_complete"] is False
    first = client.post(
        "/api/v1/activities",
        json={"elevation_ft": "40", "happened_on": DAY},
        headers=headers,
    )
    assert first.status_code == 201
    activity_id = first.get_json()["activity"]["id"]
    listed = client.get("/api/v1/activities", headers=headers)
    assert activity_id in {row["id"] for row in listed.get_json()["activities"]}
    stale_preview = client.post(
        "/api/v1/activities/preview",
        json={"elevation_ft": "100", "happened_on": DAY},
        headers=headers,
    ).get_json()["preview"]
    assert stale_preview["will_complete"] is True
    client.post(
        "/api/v1/activities",
        json={"elevation_ft": "10", "happened_on": DAY},
        headers=headers,
    )
    rejected = client.post(
        "/api/v1/activities",
        json={
            "elevation_ft": "100",
            "happened_on": DAY,
            "confirmation_token": stale_preview["confirmation_token"],
        },
        headers=headers,
    )
    assert rejected.status_code == 409
    assert rejected.get_json()["code"] == "confirmation_required"
    assert rejected.get_json()["details"]["preview"]["confirmation_token"]
    with app.app_context():
        assert _count("activities", "user_id = ?", (_id,)) == 2
        assert get_db().execute(
            "SELECT status FROM route_journeys WHERE user_id = ? AND is_current",
            (_id,),
        ).fetchone()["status"] == "active"
    edit_preview = client.post(
        f"/api/v1/activities/{activity_id}/preview",
        json={"elevation_ft": "100"},
        headers=headers,
    ).get_json()["preview"]
    assert edit_preview["will_complete"] is True
    edited = client.patch(
        f"/api/v1/activities/{activity_id}",
        json={
            "elevation_ft": "100",
            "confirmation_token": edit_preview["confirmation_token"],
        },
        headers=headers,
    )
    assert edited.status_code == 200, edited.get_json()
    updates = client.get("/api/v1/expeditions/summit-party/updates", headers=headers)
    assert "summit_reached" in {row["kind"] for row in updates.get_json()["updates"]}
    protected = client.patch(
        f"/api/v1/activities/{activity_id}",
        json={"elevation_ft": "90"},
        headers=headers,
    )
    assert protected.status_code == 409
    removed = client.delete(f"/api/v1/activities/{activity_id}", headers=headers)
    assert removed.status_code == 409
    assert removed.get_json()["code"] == "invalid_state"


def test_private_expedition_invitation_leave_and_privacy(client, app):
    with app.app_context():
        _route("kili", "1000")
    _owen, owen, _csrf = _register(client, "Owen")
    _sam, sam, _csrf = _register(client, "Sam")
    _rita, rita, _csrf = _register(client, "Rita")
    created = client.post(
        "/api/v1/expeditions",
        json={"slug": "owen-kili", "name": "Owen Kili", "route_slug": "kili"},
        headers=_auth(owen),
    )
    assert created.status_code == 201
    link = client.post(
        "/api/v1/expeditions/owen-kili/invitations",
        json={"kind": "link"},
        headers=_auth(owen),
    )
    assert link.status_code == 201
    token = link.get_json()["token"]
    assert token
    named = client.post(
        "/api/v1/expeditions/owen-kili/invitations",
        json={"username": "Rita"},
        headers=_auth(owen),
    )
    assert named.status_code == 201
    listed = client.get("/api/v1/expeditions/owen-kili/invitations", headers=_auth(owen))
    assert all(row["token"] is None for row in listed.get_json()["invitations"])
    stranger = client.get("/api/v1/expeditions/owen-kili", headers=_auth(sam))
    assert stranger.status_code == 404
    joined = client.post(
        "/api/v1/invitations/accept",
        json={"token": token},
        headers=_auth(sam),
    )
    assert joined.status_code == 200, joined.get_json()
    client.post(
        "/api/v1/activities",
        json={"elevation_ft": "100", "happened_on": DAY},
        headers=_auth(sam),
    )
    live = client.get("/api/v1/expeditions/owen-kili/leaderboard", headers=_auth(sam))
    assert live.get_json()["mode"] == "live"
    assert any(row["user_id"] == _sam for row in live.get_json()["entries"])
    left = client.post("/api/v1/expeditions/owen-kili/leave", headers=_auth(sam))
    assert left.get_json()["participation"]["status"] == "withdrawn"
    hidden = client.get("/api/v1/expeditions/owen-kili/leaderboard", headers=_auth(sam))
    assert hidden.status_code == 404
    rejoined = client.post(
        "/api/v1/invitations/accept",
        json={"token": token},
        headers=_auth(sam),
    )
    assert rejoined.get_json()["participation"]["display_credits_after"] is None
    still = client.get("/api/v1/expeditions/owen-kili/leaderboard", headers=_auth(owen))
    sam_row = next(row for row in still.get_json()["entries"] if row["user_id"] == _sam)
    assert Decimal(sam_row["total_m"]) > 0
    client.post(
        "/api/v1/expeditions/owen-kili/privacy-removal",
        json={"confirm": True},
        headers=_auth(sam),
    )
    removed = client.get("/api/v1/expeditions/owen-kili/roster", headers=_auth(owen))
    assert _sam not in {row["user_id"] for row in removed.get_json()["roster"]}
    back = client.post(
        "/api/v1/invitations/accept",
        json={"token": token},
        headers=_auth(sam),
    )
    assert back.get_json()["participation"]["display_credits_after"] is not None
    board = client.get("/api/v1/expeditions/owen-kili/leaderboard", headers=_auth(owen))
    sam_again = next(row for row in board.get_json()["entries"] if row["user_id"] == _sam)
    assert Decimal(sam_again["total_m"]) == 0
    home = client.get("/api/v1/home", headers=_auth(rita))
    invite = home.get_json()["incoming_invitations"][0]
    assert invite["kind"] == "username"
    assert [action["type"] for action in invite["actions"]] == [
        "accept_invitation",
        "decline_invitation",
    ]
    assert "accept_invitation" not in {action["type"] for action in home.get_json()["next_actions"]}
    declined = client.post(
        f"/api/v1/invitations/{invite['id']}/decline",
        headers=_auth(rita),
    )
    assert declined.status_code == 200
    revoked = client.post(
        "/api/v1/expeditions/owen-kili/invitations",
        json={"username": "Rita"},
        headers=_auth(owen),
    )
    client.post(
        f"/api/v1/invitations/{revoked.get_json()['id']}/revoke",
        headers=_auth(owen),
    )
    gone = client.post(
        f"/api/v1/invitations/{revoked.get_json()['id']}/accept",
        headers=_auth(rita),
    )
    assert gone.status_code == 409


def test_retained_read_and_final_board(client, app):
    with app.app_context():
        route_id = _route("finale", "100")
        _checkpoint(route_id, "top", "100", 1)
    _owen, owen, _csrf = _register(client, "Owen")
    _fay, fay, _csrf = _register(client, "Fay")
    _lee, lee, _csrf = _register(client, "Lee")
    client.post(
        "/api/v1/expeditions",
        json={"slug": "finale-party", "name": "Finale", "route_slug": "finale"},
        headers=_auth(owen),
    )
    link = client.post(
        "/api/v1/expeditions/finale-party/invitations",
        json={"kind": "link"},
        headers=_auth(owen),
    ).get_json()["token"]
    for token, person in ((link, fay), (link, lee)):
        assert client.post(
            "/api/v1/invitations/accept",
            json={"token": token},
            headers=_auth(person),
        ).status_code == 200
    preview = client.post(
        "/api/v1/activities/preview",
        json={"elevation_ft": "100", "happened_on": DAY},
        headers=_auth(fay),
    ).get_json()["preview"]
    assert client.post(
        "/api/v1/activities",
        json={
            "elevation_ft": "100",
            "happened_on": DAY,
            "confirmation_token": preview["confirmation_token"],
        },
        headers=_auth(fay),
    ).status_code == 201
    client.post("/api/v1/expeditions/finale-party/leave", headers=_auth(fay))
    client.post("/api/v1/expeditions/finale-party/leave", headers=_auth(lee))
    kept = client.get("/api/v1/expeditions/finale-party/leaderboard", headers=_auth(fay))
    assert kept.status_code == 200
    assert kept.get_json()["mode"] == "live"
    assert _fay not in {row["user_id"] for row in kept.get_json()["entries"]}
    assert client.get(
        "/api/v1/expeditions/finale-party/leaderboard",
        headers=_auth(lee),
    ).status_code == 404
    closed = client.post("/api/v1/expeditions/finale-party/archive", headers=_auth(owen))
    assert closed.status_code == 200
    final = client.get("/api/v1/expeditions/finale-party/leaderboard", headers=_auth(fay))
    assert final.status_code == 200
    assert final.get_json()["mode"] == "final"
    assert _fay in {row["user_id"] for row in final.get_json()["entries"]}
    assert client.get(
        "/api/v1/expeditions/finale-party/leaderboard",
        headers=_auth(lee),
    ).status_code == 404


def test_organization_enrollment_slot_and_admin_boundary(client, app):
    _ada, ada, _csrf = _register(client, "Ada")
    _owen, owen, _csrf = _register(client, "Owen")
    _sam, sam, _csrf = _register(client, "Sam")
    app.config["PLATFORM_ADMIN_IDS"] = {_ada}
    with app.app_context():
        _route("alpha", "1000")
        _route("beta", "1000")
    denied = client.post(
        "/api/v1/organizations",
        json={"slug": "ridge-club", "name": "Ridge Club", "owner_username": "Owen"},
        headers=_auth(owen),
    )
    assert denied.status_code == 403
    created = client.post(
        "/api/v1/organizations",
        json={"slug": "ridge-club", "name": "Ridge Club", "owner_username": "Owen"},
        headers=_auth(ada),
    )
    assert created.status_code == 201, created.get_json()
    later = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    earlier = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    for slug, route, closes in (
        ("alpha-season", "alpha", None),
        ("beta-season", "beta", None),
        ("closed-season", "alpha", earlier),
    ):
        body = {
            "slug": slug,
            "name": slug,
            "route_slug": route,
            "enrollment": "open",
            "starts_at": earlier,
            "ends_at": later,
        }
        if closes:
            body["enrollment_closes_at"] = closes
        made = client.post(
            "/api/v1/organizations/ridge-club/expeditions",
            json=body,
            headers=_auth(owen),
        )
        assert made.status_code == 201, made.get_json()
    members = client.get("/api/v1/organizations/ridge-club/members", headers=_auth(sam))
    assert members.status_code == 404
    added = client.post(
        "/api/v1/organizations/ridge-club/members",
        json={"username": "Sam", "role": "member"},
        headers=_auth(owen),
    )
    assert added.status_code == 201
    forbidden = client.get("/api/v1/organizations/ridge-club/members", headers=_auth(sam))
    assert forbidden.status_code == 403
    closed = client.post("/api/v1/expeditions/closed-season/join", headers=_auth(sam))
    assert closed.status_code == 409
    assert closed.get_json()["code"] == "invalid_state"
    first = client.post("/api/v1/expeditions/alpha-season/join", headers=_auth(sam))
    assert first.status_code == 200, first.get_json()
    second = client.post("/api/v1/expeditions/beta-season/join", headers=_auth(sam))
    assert second.status_code == 409
    assert second.get_json()["code"] == "slot_occupied"
    suspend = client.post("/api/v1/admin/organizations/ridge-club/suspend", headers=_auth(owen))
    assert suspend.status_code == 403
    done = client.post("/api/v1/admin/organizations/ridge-club/suspend", headers=_auth(ada))
    assert done.status_code == 200, done.get_json()
    assert done.get_json()["organization"]["status"] == "suspended"
    assert client.get(
        "/api/v1/expeditions/alpha-season",
        headers=_auth(owen),
    ).get_json()["expedition"]["status"] == "completed"


def test_home_states_actions_photos_and_no_writes(client, app, tmp_path):
    photos = tmp_path / "photos"
    (photos / "vista").mkdir(parents=True)
    (photos / "vista" / "hero.jpg").write_bytes(b"hero")
    (photos / "vista" / "camp.jpg").write_bytes(b"camp")
    (photos / "vista" / "summit.jpg").write_bytes(b"summit")
    app.config["PHOTOS_DIR"] = str(photos)
    with app.app_context():
        vista = _route("vista", "1000")
        _checkpoint(vista, "camp", "100", 1)
        _route("other", "1000")
        _route("done", "100")
    _id, token, _csrf = _register(client, "Sam")
    headers = _auth(token)
    ready = client.get("/api/v1/home", headers=headers).get_json()
    assert ready["state"] == "ready"
    assert ready["journey"] is None
    assert ready["focus"] is None
    assert ready["next_actions"] == [{"type": "choose_route"}]
    with app.app_context():
        journeys_before = _count("route_journeys", "user_id = ?", (_id,))
        invites_before = _count("expedition_invitations")
    client.get("/api/v1/home", headers=headers)
    with app.app_context():
        assert _count("route_journeys", "user_id = ?", (_id,)) == journeys_before
        assert _count("expedition_invitations") == invites_before
    client.post("/api/v1/journeys/start", json={"route_slug": "vista"}, headers=headers)
    below = client.get("/api/v1/home", headers=headers).get_json()
    assert below["state"] == "active"
    assert below["journey"]["status"] == "active"
    assert below["focus"] == {"journey_id": below["journey"]["id"], "status": "active"}
    assert below["journey"]["checkpoint"]["current"] is None
    assert below["journey"]["scene"]["role"] == "route_hero"
    assert below["journey"]["scene"]["image"]["path"].endswith("/vista/hero.jpg")
    assert "path" not in below["journey"]["checkpoint"]["next"]
    client.post(
        "/api/v1/activities",
        json={"elevation_ft": "100", "happened_on": DAY},
        headers=headers,
    )
    camp = client.get("/api/v1/home", headers=headers).get_json()
    assert camp["journey"]["checkpoint"]["current"]["slug"] == "camp"
    assert camp["journey"]["scene"]["checkpoint_slug"] == "camp"
    assert camp["journey"]["scene"]["image"]["path"].endswith("/vista/camp.jpg")
    assert "summit" not in camp["journey"]["scene"]["image"]["path"]
    client.post("/api/v1/journeys/switch", json={"route_slug": "other"}, headers=headers)
    client.post("/api/v1/journeys/start", json={"route_slug": "done"}, headers=headers)
    preview = client.post(
        "/api/v1/activities/preview",
        json={"elevation_ft": "100", "happened_on": DAY},
        headers=headers,
    ).get_json()["preview"]
    client.post(
        "/api/v1/activities",
        json={
            "elevation_ft": "100",
            "happened_on": DAY,
            "confirmation_token": preview["confirmation_token"],
        },
        headers=headers,
    )
    client.post("/api/v1/journeys/resume", json={"route_slug": "other"}, headers=headers)
    mixed = client.get("/api/v1/home", headers=headers).get_json()
    kinds = [action["type"] for action in mixed["next_actions"]]
    assert kinds == ["continue_journey", "resume_journey", "start_new_attempt"]
    assert mixed["journey"]["route"]["slug"] == "other"
    assert mixed["paused_journeys"][0]["route"]["slug"] == "vista"
    assert mixed["paused_journeys"][0]["status"] == "paused"
    assert mixed["focus"]["status"] == "active"
    client.post("/api/v1/journeys/abandon", json={"route_slug": "other"}, headers=headers)
    paused_only = client.get("/api/v1/home", headers=headers).get_json()
    assert paused_only["state"] == "resume"
    assert paused_only["journey"] is None
    assert paused_only["focus"] is None
    assert {action["type"] for action in paused_only["next_actions"]} == {
        "resume_journey",
        "start_new_attempt",
    }
    client.post("/api/v1/journeys/abandon", json={"route_slug": "vista"}, headers=headers)
    finished = client.get("/api/v1/home", headers=headers).get_json()
    assert finished["journey"] is None
    assert finished["state"] in ("completed", "abandoned")
    assert finished["focus"]["status"] == finished["state"]
    assert "scene" in finished["focus"]
    assert all(action["type"] == "start_new_attempt" for action in finished["next_actions"])


def test_login_returns_a_usable_token_and_logout_revokes_it(client):
    _register(client, "Sam")
    wrong = client.post(
        "/api/v1/auth/login",
        json={"name": "Sam", "password": "not-the-password"},
    )
    assert wrong.status_code == 401
    assert wrong.get_json()["code"] == "unauthenticated"
    logged = client.post(
        "/api/v1/auth/login",
        json={"name": "Sam", "password": "trailhead"},
    )
    assert logged.status_code == 200
    token = logged.get_json()["token"]
    client.delete_cookie("session")
    assert client.get("/api/v1/home", headers=_auth(token)).status_code == 200
    signed_out = client.post("/api/v1/auth/logout", headers=_auth(token))
    assert signed_out.status_code == 200
    denied = client.get("/api/v1/home", headers=_auth(token))
    assert denied.status_code == 401
    assert denied.get_json()["code"] == "unauthenticated"


def test_failed_logins_reach_the_limit_and_success_clears_it(client, app):
    _register(client, "Sam")
    with app.app_context():
        tokens_before = _count("api_tokens")

    def fail():
        return client.post(
            "/api/v1/auth/login",
            json={"name": "Sam", "password": "not-the-password"},
        )

    for _attempt in range(4):
        rejected = fail()
        assert rejected.status_code == 401
        assert rejected.get_json()["code"] == "unauthenticated"
    logged = client.post(
        "/api/v1/auth/login",
        json={"name": "Sam", "password": "trailhead"},
    )
    assert logged.status_code == 200
    client.delete_cookie("session")
    for _attempt in range(5):
        rejected = fail()
        assert rejected.status_code == 401
    limited = fail()
    assert limited.status_code == 429
    assert limited.get_json()["code"] == "rate_limited"
    with app.app_context():
        assert _count("api_tokens") == tokens_before + 1


def test_invalid_invite_codes_are_rate_limited_and_create_nothing(client, app):
    app.config["INVITE_CODE"] = "khumbu"
    body = {"name": "Sam", "password": "trailhead", "invite": "wrong"}
    for _attempt in range(5):
        rejected = client.post("/api/v1/auth/register", json=body)
        assert rejected.status_code == 400
        payload = rejected.get_json()
        assert payload["code"] == "invalid"
        assert "khumbu" not in payload["error"]
    limited = client.post("/api/v1/auth/register", json=body)
    assert limited.status_code == 429
    assert limited.get_json()["code"] == "rate_limited"
    assert "khumbu" not in limited.get_json()["error"]
    with app.app_context():
        assert _count("users") == 0
        assert _count("api_tokens") == 0


def test_registration_with_the_invite_code_clears_failures(client, app):
    app.config["INVITE_CODE"] = "khumbu"
    wrong = {"name": "Sam", "password": "trailhead", "invite": "wrong"}
    for _attempt in range(4):
        assert client.post("/api/v1/auth/register", json=wrong).status_code == 400
    created = client.post(
        "/api/v1/auth/register",
        json={"name": "Sam", "password": "trailhead", "invite": "khumbu"},
    )
    assert created.status_code == 201, created.get_json()
    token = created.get_json()["token"]
    client.delete_cookie("session")
    assert client.get("/api/v1/home", headers=_auth(token)).status_code == 200
    with app.app_context():
        assert _count("users") == 1
        assert _count("api_tokens") == 1
    for _attempt in range(5):
        rejected = client.post("/api/v1/auth/register", json=wrong)
        assert rejected.status_code == 400
        assert rejected.get_json()["code"] == "invalid"
    limited = client.post("/api/v1/auth/register", json=wrong)
    assert limited.status_code == 429
    with app.app_context():
        assert _count("users") == 1
        assert _count("api_tokens") == 1


def test_revoke_all_invalidates_every_bearer_token(client):
    _user, first, _csrf = _register(client, "Sam")
    logged = client.post(
        "/api/v1/auth/login",
        json={"name": "Sam", "password": "trailhead"},
    )
    second = logged.get_json()["token"]
    client.delete_cookie("session")
    assert client.get("/api/v1/home", headers=_auth(first)).status_code == 200
    assert client.get("/api/v1/home", headers=_auth(second)).status_code == 200
    revoked = client.post("/api/v1/auth/revoke", headers=_auth(second))
    assert revoked.status_code == 200
    assert client.get("/api/v1/home", headers=_auth(first)).status_code == 401
    assert client.get("/api/v1/home", headers=_auth(second)).status_code == 401


def test_expired_rate_limit_keys_are_removed(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(limits.time, "time", lambda: clock["now"])
    for name in range(30):
        assert limits.too_many(f"203.0.113.8\nname-{name}") is False
    assert limits._failures == {}
    for _attempt in range(limits.LIMIT):
        limits.record("203.0.113.8\nsam")
    assert limits.too_many("203.0.113.8\nsam") is True
    assert set(limits._failures) == {"203.0.113.8\nsam"}
    clock["now"] += limits.WINDOW_SECONDS
    assert limits.too_many("203.0.113.8\nsam") is False
    assert limits._failures == {}
    limits.record("203.0.113.8\nsam")
    limits.clear("203.0.113.8\nsam")
    assert limits._failures == {}


def test_route_detail_lists_only_joinable_expeditions(client, app):
    _ada, ada, _csrf = _register(client, "Ada")
    _owen, owen, _csrf = _register(client, "Owen")
    app.config["PLATFORM_ADMIN_IDS"] = {_ada}
    with app.app_context():
        _route("catalog", "1000")
    created = client.post(
        "/api/v1/organizations",
        json={"slug": "ridge-club", "name": "Ridge Club", "owner_username": "Owen"},
        headers=_auth(ada),
    )
    assert created.status_code == 201, created.get_json()
    now = datetime.now(timezone.utc)
    windows = {
        "open-now": {},
        "finish-me": {},
        "invite-only": {"enrollment": "invitation"},
        "closed-window": {"enrollment_closes_at": (now - timedelta(days=1)).isoformat()},
        "future-window": {
            "enrollment_opens_at": (now + timedelta(days=1)).isoformat(),
            "enrollment_closes_at": (now + timedelta(days=3)).isoformat(),
        },
    }
    for slug, extra in windows.items():
        body = {
            "slug": slug,
            "name": slug,
            "route_slug": "catalog",
            "enrollment": extra.get("enrollment", "open"),
        }
        body.update({key: value for key, value in extra.items() if key != "enrollment"})
        made = client.post(
            "/api/v1/organizations/ridge-club/expeditions",
            json=body,
            headers=_auth(owen),
        )
        assert made.status_code == 201, made.get_json()
    private = client.post(
        "/api/v1/expeditions",
        json={"slug": "owen-private", "name": "Owen Private", "route_slug": "catalog"},
        headers=_auth(owen),
    )
    assert private.status_code == 201, private.get_json()
    archived = client.post("/api/v1/expeditions/owen-private/archive", headers=_auth(owen))
    assert archived.status_code == 200, archived.get_json()

    def slugs():
        detail = client.get("/api/v1/routes/catalog")
        assert detail.status_code == 200
        return {row["slug"] for row in detail.get_json()["expeditions"]}

    assert slugs() == {"open-now", "finish-me"}
    finished = client.post("/api/v1/expeditions/finish-me/complete", headers=_auth(owen))
    assert finished.status_code == 200, finished.get_json()
    assert slugs() == {"open-now"}
    with app.app_context():
        get_db().execute(
            "UPDATE organizations SET status = 'suspended' WHERE slug = 'ridge-club'"
        )
        get_db().commit()
        status = get_db().execute(
            "SELECT status FROM expeditions WHERE slug = 'open-now'"
        ).fetchone()
        assert status["status"] == "active"
    assert slugs() == set()
    everest = client.get("/api/v1/routes/everest")
    assert everest.status_code == 200
    assert {row["slug"] for row in everest.get_json()["expeditions"]} == {"everest"}
