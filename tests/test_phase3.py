"""Routes, expeditions, invitations, and the activity library."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

from adventurer.db import DB, get_db
from adventurer.measures import feet_to_meters
from adventurer.services import create_adventurer

from test_app import _csrf, _join, _log


def _session_token(client) -> str:
    return client.get("/api/v1/session").get_json()["csrf"]


def _api(client, method, path, payload=None):
    token = _session_token(client)
    return client.open(
        path,
        method=method,
        json=payload,
        headers={"X-CSRF-Token": token},
    )


def _user_id(name_key: str) -> int:
    return get_db().execute(
        "SELECT id FROM users WHERE username_key = ?",
        (name_key,),
    ).fetchone()["id"]


def test_published_routes_hide_draft_and_retired(app):
    with app.app_context():
        get_db().execute(
            """
            INSERT INTO routes (slug, name, region, summary, target_elevation_m, status)
            VALUES
                ('draft-ridge', 'Draft Ridge', 'Test', 'Hidden', 1000, 'draft'),
                ('old-ridge', 'Old Ridge', 'Test', 'Retired', 1000, 'retired')
            """
        )
        get_db().commit()
    client = app.test_client()
    listed = client.get("/api/v1/routes").get_json()["routes"]
    slugs = [row["slug"] for row in listed]
    assert "everest" in slugs
    assert "draft-ridge" not in slugs
    assert "old-ridge" not in slugs
    assert client.get("/api/v1/routes/draft-ridge").status_code == 404
    assert client.get("/api/v1/routes/old-ridge").status_code == 404
    detail = client.get("/api/v1/routes/everest").get_json()
    assert detail["name"] == "Everest"
    assert detail["region"] == "Khumbu, Nepal"
    assert detail["summit_ft"] == 29032
    assert len(detail["checkpoints"]) == 16
    assert [stop["slug"] for stop in detail["checkpoints"]][:2] == ["timms-hill", "four-thousand"]
    assert detail["checkpoints"][-1]["slug"] == "summit"
    assert detail["expeditions"][0]["slug"] == "everest"
    page = client.get("/routes/everest")
    assert page.status_code == 200
    assert "Global Everest Challenge" in page.get_data(as_text=True)
    assert "Timms Hill" in page.get_data(as_text=True)


def test_account_creation_does_not_enroll_but_onboarding_does(client):
    with client.application.app_context():
        created = create_adventurer("Rae", "trailhead")
        count = get_db().execute(
            "SELECT COUNT(*) AS n FROM expedition_participations WHERE user_id = ?",
            (created["id"],),
        ).fetchone()["n"]
    assert count == 0
    _join(client, "Sam")
    with client.application.app_context():
        row = get_db().execute(
            """
            SELECT expedition.slug, participation.status
            FROM expedition_participations AS participation
            JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
            JOIN users ON users.id = participation.user_id
            WHERE users.username_key = 'sam'
            """
        ).fetchone()
    assert row["slug"] == "everest"
    assert row["status"] == "active"


def test_join_is_idempotent_and_rejoin_restores_the_same_row(client):
    _join(client, "Sam")
    _log(client, 180, note="kept")
    with client.application.app_context():
        before = get_db().execute(
            """
            SELECT participation.id
            FROM expedition_participations AS participation
            JOIN users ON users.id = participation.user_id
            WHERE users.username_key = 'sam'
            """
        ).fetchone()["id"]
    again = _api(client, "POST", "/api/v1/expeditions/everest/participation")
    assert again.status_code == 200
    left = _api(client, "DELETE", "/api/v1/expeditions/everest/participation")
    assert left.status_code == 200
    board = client.get("/everest/board").get_data(as_text=True)
    assert "Sam" not in __import__("re").findall(r"<strong>([^<]+)</strong>", board)
    with client.application.app_context():
        stored = get_db().execute(
            """
            SELECT participation.id, participation.status, credit.credited_elevation_m
            FROM expedition_participations AS participation
            JOIN activity_credits AS credit ON credit.participation_id = participation.id
            JOIN users ON users.id = participation.user_id
            WHERE users.username_key = 'sam'
            """
        ).fetchone()
    assert stored["id"] == before
    assert stored["status"] == "withdrawn"
    assert stored["credited_elevation_m"] == feet_to_meters(Decimal("180"))
    rejoined = _api(client, "POST", "/api/v1/expeditions/everest/participation")
    assert rejoined.status_code == 200
    with client.application.app_context():
        from adventurer.services import total_elevation

        assert total_elevation(_user_id("sam"), "everest") == 180
        after = get_db().execute(
            "SELECT id, status FROM expedition_participations WHERE id = ?",
            (before,),
        ).fetchone()
    assert after["id"] == before
    assert after["status"] == "active"
    assert "Sam" in client.get("/everest/board").get_data(as_text=True)


def test_zero_elevation_member_is_on_the_board_below_climbers(app):
    sam = app.test_client()
    alex = app.test_client()
    _join(sam, "Sam")
    _join(alex, "Alex")
    _log(sam, 100)
    text = sam.get("/everest/board").get_data(as_text=True)
    names = __import__("re").findall(r"<strong>([^<]+)</strong>", text)
    assert names == ["Sam", "Alex"]
    home = alex.get("/everest").get_data(as_text=True)
    assert "Alex" in home
    assert "0 ft" in home


def test_same_activity_can_count_on_two_expeditions_of_one_route(client):
    _join(client, "Sam")
    _log(client, 125, note="shared day")
    created = _api(
        client,
        "POST",
        "/api/v1/expeditions",
        {"route_slug": "everest", "name": "Friday Hills", "description": "Our group", "visibility": "private"},
    )
    assert created.status_code == 201
    slug = created.get_json()["slug"]
    rejected = _api(
        client,
        "POST",
        "/api/v1/expeditions",
        {"route_slug": "everest", "name": "Open Hills", "visibility": "public"},
    )
    assert rejected.status_code == 400
    with client.application.app_context():
        activity_id = get_db().execute(
            "SELECT id FROM activities WHERE note = 'shared day'"
        ).fetchone()["id"]
    credited = _api(
        client,
        "POST",
        f"/api/v1/activities/{activity_id}/credits",
        {"expedition_slug": slug, "credited_elevation_ft": 99999},
    )
    assert credited.status_code == 200
    with client.application.app_context():
        rows = get_db().execute(
            """
            SELECT expedition.slug, credit.credited_elevation_m, activity.elevation_gain_m
            FROM activity_credits AS credit
            JOIN activities AS activity ON activity.id = credit.activity_id
            JOIN expedition_participations AS participation
                ON participation.id = credit.participation_id
            JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
            WHERE activity.id = ?
            ORDER BY expedition.slug
            """,
            (activity_id,),
        ).fetchall()
    assert [row["slug"] for row in rows] == sorted(["everest", slug])
    assert all(row["credited_elevation_m"] == row["elevation_gain_m"] for row in rows)
    assert "Friday Hills" not in client.get("/api/v1/expeditions").get_data(as_text=True)
    removed = _api(client, "DELETE", f"/api/v1/activities/{activity_id}/credits/{slug}")
    assert removed.status_code == 200
    library = _api(client, "GET", "/api/v1/activities").get_json()["activities"]
    assert library[0]["note"] == "shared day"
    assert [item["expedition_slug"] for item in library[0]["credits"]] == ["everest"]
    _api(client, "DELETE", f"/api/v1/activities/{activity_id}/credits/everest")
    library = _api(client, "GET", "/api/v1/activities").get_json()["activities"]
    assert library[0]["id"] == activity_id
    assert library[0]["credits"] == []


def test_private_expedition_is_indistinguishable_from_missing(app):
    owner = app.test_client()
    stranger = app.test_client()
    _join(owner, "Sam")
    _join(stranger, "Alex")
    created = _api(
        owner,
        "POST",
        "/api/v1/expeditions",
        {"route_slug": "everest", "name": "Secret Ridge Party", "description": "quiet"},
    )
    slug = created.get_json()["slug"]
    missing = stranger.get("/api/v1/expeditions/no-such-expedition")
    hidden = stranger.get(f"/api/v1/expeditions/{slug}")
    assert missing.status_code == hidden.status_code == 404
    assert missing.get_json() == hidden.get_json() == {"error": "Not found"}
    missing_page = stranger.get("/no-such-expedition")
    hidden_page = stranger.get(f"/{slug}")
    assert missing_page.status_code == hidden_page.status_code == 404
    assert missing_page.get_data() == hidden_page.get_data()
    assert b"Secret Ridge Party" not in hidden_page.get_data()
    visible = owner.get(f"/api/v1/expeditions/{slug}")
    assert visible.status_code == 200
    assert visible.get_json()["name"] == "Secret Ridge Party"
    assert "Secret Ridge Party" in owner.get(f"/{slug}").get_data(as_text=True)
    board = stranger.get(f"/api/v1/expeditions/{slug}/leaderboard")
    assert board.status_code == 404
    assert board.get_json() == {"error": "Not found"}


def test_withdrawn_member_loses_private_access_and_owner_keeps_it(app):
    owner = app.test_client()
    member = app.test_client()
    _join(owner, "Sam")
    _join(member, "Alex")
    created = _api(
        owner,
        "POST",
        "/api/v1/expeditions",
        {"route_slug": "everest", "name": "Quiet Camp", "description": "us"},
    )
    slug = created.get_json()["slug"]
    invited = _api(owner, "POST", f"/api/v1/expeditions/{slug}/invitations", {"username": "Alex"})
    assert invited.status_code == 201
    invitation_id = invited.get_json()["id"]
    accepted = member.post(
        f"/invitations/{invitation_id}/accept",
        data={"csrf": _csrf(member, "/activities")},
        follow_redirects=True,
    )
    assert accepted.status_code == 200
    assert "Quiet Camp" in accepted.get_data(as_text=True)
    _api(member, "DELETE", f"/api/v1/expeditions/{slug}/participation")
    hidden = member.get(f"/api/v1/expeditions/{slug}")
    assert hidden.status_code == 404
    assert hidden.get_json() == {"error": "Not found"}
    _api(owner, "DELETE", f"/api/v1/expeditions/{slug}/participation")
    still = owner.get(f"/api/v1/expeditions/{slug}")
    assert still.status_code == 200
    renamed = _api(owner, "PATCH", f"/api/v1/expeditions/{slug}", {"name": "Quiet Camp Still"})
    assert renamed.status_code == 200
    assert renamed.get_json()["name"] == "Quiet Camp Still"
    assert owner.get(f"/expeditions/{slug}/manage").status_code == 200


def test_archived_expeditions_stay_readable_and_reject_new_activity(app):
    owner = app.test_client()
    stranger = app.test_client()
    _join(owner, "Sam")
    _join(stranger, "Alex")
    _log(owner, 40, note="already counted")
    created = _api(
        owner,
        "POST",
        "/api/v1/expeditions",
        {"route_slug": "everest", "name": "Closed Camp", "description": "done"},
    )
    slug = created.get_json()["slug"]
    archived = _api(owner, "POST", f"/api/v1/expeditions/{slug}/archive")
    assert archived.status_code == 200
    assert archived.get_json()["status"] == "archived"
    assert owner.get(f"/api/v1/expeditions/{slug}").status_code == 200
    assert stranger.get(f"/api/v1/expeditions/{slug}").status_code == 404
    refused = _api(owner, "POST", f"/api/v1/expeditions/{slug}/participation")
    assert refused.status_code == 403
    with owner.application.app_context():
        activity_id = get_db().execute(
            "SELECT id FROM activities WHERE note = 'already counted'"
        ).fetchone()["id"]
    credit = _api(
        owner,
        "POST",
        f"/api/v1/activities/{activity_id}/credits",
        {"expedition_slug": slug},
    )
    assert credit.status_code == 403
    with owner.application.app_context():
        get_db().execute(
            """
            UPDATE expeditions
            SET visibility = 'public', status = 'archived'
            WHERE slug = ?
            """,
            (slug,),
        )
        get_db().commit()
    assert stranger.get(f"/api/v1/expeditions/{slug}").status_code == 200
    assert slug not in [
        row["slug"] for row in stranger.get("/api/v1/expeditions").get_json()["expeditions"]
    ]
    assert _api(stranger, "POST", f"/api/v1/expeditions/{slug}/participation").status_code == 403
    global_edit = _api(owner, "PATCH", "/api/v1/expeditions/everest", {"name": "Mine"})
    assert global_edit.status_code == 403
    assert _api(owner, "POST", "/api/v1/expeditions/everest/archive").status_code == 403


def test_invitations_are_hashed_single_use_for_names_and_shared_for_links(app):
    owner = app.test_client()
    alex = app.test_client()
    rae = app.test_client()
    _join(owner, "Sam")
    _join(alex, "Alex")
    _join(rae, "Rae")
    created = _api(
        owner,
        "POST",
        "/api/v1/expeditions",
        {"route_slug": "everest", "name": "Invite Camp", "description": "come"},
    )
    slug = created.get_json()["slug"]
    assert _api(alex, "POST", f"/api/v1/expeditions/{slug}/invitations", {"username": "Rae"}).status_code == 404
    first = _api(owner, "POST", f"/api/v1/expeditions/{slug}/invitations", {"username": "Alex"})
    second = _api(owner, "POST", f"/api/v1/expeditions/{slug}/invitations", {"username": "Alex"})
    assert first.status_code == 201
    assert second.get_json()["id"] == first.get_json()["id"]
    listed = _api(owner, "GET", f"/api/v1/expeditions/{slug}/invitations").get_json()
    assert "token" not in listed["invitations"][0]
    wrong = rae.post(
        f"/invitations/{first.get_json()['id']}/accept",
        data={"csrf": _csrf(rae, "/activities")},
    )
    assert wrong.status_code == 404
    declined = alex.post(
        f"/invitations/{first.get_json()['id']}/decline",
        data={"csrf": _csrf(alex, "/activities")},
        follow_redirects=True,
    )
    assert declined.status_code == 200
    reissued = _api(owner, "POST", f"/api/v1/expeditions/{slug}/invitations", {"username": "Alex"})
    assert reissued.status_code == 201
    assert reissued.get_json()["id"] != first.get_json()["id"]
    link = _api(owner, "POST", f"/api/v1/expeditions/{slug}/invitations", {"kind": "link"})
    assert link.status_code == 201
    token = link.get_json()["token"]
    again = _api(owner, "POST", f"/api/v1/expeditions/{slug}/invitations", {"kind": "link"})
    assert again.status_code == 409
    listed = _api(owner, "GET", f"/api/v1/expeditions/{slug}/invitations").get_json()
    blob = str(listed)
    assert token not in blob
    with owner.application.app_context():
        stored = get_db().execute(
            """
            SELECT token_hash FROM expedition_invitations
            WHERE invited_user_id IS NULL AND status = 'pending'
            """
        ).fetchone()
    assert stored["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert stored["token_hash"] != token
    manage = owner.get(f"/expeditions/{slug}/manage").get_data(as_text=True)
    assert token not in manage
    outsider = app.test_client()
    visit = outsider.get(f"/i/{token}", follow_redirects=True)
    assert visit.status_code == 200
    assert token not in visit.get_data(as_text=True)
    assert "Invite Camp" in visit.get_data(as_text=True)
    _join(outsider, "Noa")
    with outsider.application.app_context():
        everest = get_db().execute(
            """
            SELECT COUNT(*) AS n
            FROM expedition_participations AS participation
            JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
            JOIN users ON users.id = participation.user_id
            WHERE users.username_key = 'noa' AND expedition.slug = 'everest'
            """
        ).fetchone()["n"]
    assert everest == 0
    accepted = outsider.post(
        "/i",
        data={"csrf": _csrf(outsider, "/i"), "action": "accept"},
        follow_redirects=True,
    )
    assert accepted.status_code == 200
    assert "Invite Camp" in accepted.get_data(as_text=True)
    rae_accept = _api(rae, "POST", f"/api/v1/invitations/{token}/accept")
    assert rae_accept.status_code == 200
    revoked = _api(owner, "DELETE", f"/api/v1/expeditions/{slug}/invitations/{link.get_json()['id']}")
    assert revoked.status_code == 200
    assert _api(owner, "POST", f"/api/v1/invitations/{token}/accept").status_code == 404
    with owner.application.app_context():
        get_db().execute(
            """
            UPDATE expedition_invitations
            SET status = 'pending', expires_at = now() - interval '1 minute'
            WHERE id = ?
            """,
            (link.get_json()["id"],),
        )
        get_db().commit()
    assert _api(rae, "POST", f"/api/v1/invitations/{token}/accept").status_code == 404


def test_activity_library_is_private_and_elevation_edits_are_atomic(app, monkeypatch):
    sam = app.test_client()
    alex = app.test_client()
    _join(sam, "Sam")
    _join(alex, "Alex")
    made = _api(
        sam,
        "POST",
        "/api/v1/activities",
        {"elevation_ft": 100, "note": "mine", "happened_on": "2026-11-01", "expedition_slug": "everest"},
    )
    assert made.status_code == 201
    activity_id = made.get_json()["id"]
    assert _api(alex, "GET", "/api/v1/activities").get_json()["activities"] == []
    assert _api(alex, "PATCH", f"/api/v1/activities/{activity_id}", {
        "elevation_ft": 50, "happened_on": "2026-11-01", "note": "stolen"
    }).status_code == 404
    assert _api(alex, "DELETE", f"/api/v1/activities/{activity_id}").status_code == 404
    private = _api(
        sam,
        "POST",
        "/api/v1/expeditions",
        {"route_slug": "everest", "name": "Second Board", "description": "also"},
    ).get_json()["slug"]
    assert _api(
        sam, "POST", f"/api/v1/activities/{activity_id}/credits", {"expedition_slug": private}
    ).status_code == 200
    changed = _api(
        sam,
        "PATCH",
        f"/api/v1/activities/{activity_id}",
        {"elevation_ft": 220, "distance_km": "3", "happened_on": "2026-11-02", "note": "revised"},
    )
    assert changed.status_code == 200
    expected = feet_to_meters(Decimal("220"))
    with app.app_context():
        rows = get_db().execute(
            """
            SELECT activity.elevation_gain_m, credit.credited_elevation_m
            FROM activities AS activity
            JOIN activity_credits AS credit ON credit.activity_id = activity.id
            WHERE activity.id = ?
            """,
            (activity_id,),
        ).fetchall()
    assert len(rows) == 2
    assert all(row["elevation_gain_m"] == expected for row in rows)
    assert all(row["credited_elevation_m"] == expected for row in rows)

    original = DB.execute

    def explode(self, sql, params=()):
        if "UPDATE activity_credits" in sql:
            raise RuntimeError("credit update failed")
        return original(self, sql, params)

    monkeypatch.setattr(DB, "execute", explode)
    with app.app_context():
        from adventurer.progress import clean_entry
        from adventurer.platform import update_activity

        try:
            update_activity(
                _user_id("sam"),
                activity_id,
                clean_entry("300", "", "2026-11-02", "nope", date.today()),
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("elevation edit should have rolled back")
        stored = get_db().execute(
            "SELECT elevation_gain_m FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        credits = get_db().execute(
            "SELECT credited_elevation_m FROM activity_credits WHERE activity_id = ?",
            (activity_id,),
        ).fetchall()
    assert stored["elevation_gain_m"] == expected
    assert [row["credited_elevation_m"] for row in credits] == [expected, expected]

    monkeypatch.undo()
    removed = _api(sam, "DELETE", f"/api/v1/activities/{activity_id}")
    assert removed.status_code == 200
    with app.app_context():
        counts = get_db().execute(
            """
            SELECT
                (SELECT COUNT(*) FROM activities WHERE id = ?) AS activities,
                (SELECT COUNT(*) FROM activity_credits WHERE activity_id = ?) AS credits,
                (SELECT COUNT(*) FROM activity_verification_events WHERE activity_id = ?) AS events
            """,
            (activity_id, activity_id, activity_id),
        ).fetchone()
    assert counts["activities"] == 0
    assert counts["credits"] == 0
    assert counts["events"] == 0


def test_new_credits_honor_the_expedition_window_and_old_credits_remain(client):
    _join(client, "Sam")
    _log(client, 80, note="historical")
    with client.application.app_context():
        get_db().execute(
            """
            UPDATE expeditions
            SET ends_at = now() - interval '1 hour'
            WHERE slug = 'everest'
            """
        )
        get_db().commit()
        activity_id = get_db().execute(
            "SELECT id FROM activities WHERE note = 'historical'"
        ).fetchone()["id"]
    denied = _api(
        client,
        "POST",
        "/api/v1/activities",
        {"elevation_ft": 10, "note": "too late", "happened_on": "2020-01-02"},
    )
    assert denied.status_code == 201
    credit = _api(
        client,
        "POST",
        f"/api/v1/activities/{activity_id}/credits",
        {"expedition_slug": "everest"},
    )
    assert credit.status_code == 403
    direct = _api(
        client,
        "POST",
        "/api/v1/activities",
        {
            "elevation_ft": 15,
            "note": "closed window",
            "happened_on": "2020-01-03",
            "expedition_slug": "everest",
        },
    )
    assert direct.status_code == 403
    with client.application.app_context():
        notes = [
            row["note"]
            for row in get_db().execute("SELECT note FROM activities ORDER BY id").fetchall()
        ]
        total = get_db().execute(
            "SELECT COALESCE(SUM(credited_elevation_m), 0) AS total_m FROM activity_credits"
        ).fetchone()["total_m"]
    assert "historical" in notes
    assert "closed window" not in notes
    assert total == feet_to_meters(Decimal("80"))


def test_everest_page_and_log_endpoints_still_work(client):
    page = client.get("/everest")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "Global Everest Challenge" in text
    assert "Everest" in text
    assert "The board is empty" in text
    _join(client, "Sam")
    token = _session_token(client)
    created = client.post(
        "/api/v1/challenges/everest/logs",
        json={"elevation_ft": 100, "note": "compat"},
        headers={"X-CSRF-Token": token},
    )
    assert created.status_code == 201
    activity_id = None
    with client.application.app_context():
        activity_id = get_db().execute(
            "SELECT id FROM activities WHERE note = 'compat'"
        ).fetchone()["id"]
    deleted = client.delete(
        f"/api/v1/challenges/everest/logs/{activity_id}",
        headers={"X-CSRF-Token": token},
    )
    assert deleted.status_code == 200
    with client.application.app_context():
        assert get_db().execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"] == 1
        assert get_db().execute("SELECT COUNT(*) AS n FROM activity_credits").fetchone()["n"] == 0
