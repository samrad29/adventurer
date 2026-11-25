import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import psycopg
import pytest

from adventurer import trail_identity
from adventurer.activities import create_activity, update_activity
from adventurer.content_pipeline import ContentRejected, import_route, import_trail_name_pool
from adventurer.db import get_db
from adventurer.expeditions import (
    accept_invitation, archive_private_expedition, create_private_expedition,
    create_share_link, get_leaderboard, get_updates, leave_expedition,
    remove_participation_privacy,
)
from adventurer.journeys import choose_activity_policy, restart_journey, start_journey
from adventurer.measures import feet_to_meters
from adventurer.metrics import MILE_IN_METERS

from test_content_pipeline import _geography, _valid
from test_narrative_api import DAY, _auth, _credit, _publish, _register, _route, _start, _stop

PROFILE = "/api/v1/profile/trail-identity"


def _pool(tmp_path, names=("Night Heron", "Juniper")):
    path = tmp_path / "pool.json"
    path.write_text(json.dumps(names), encoding="utf-8")
    import_trail_name_pool(get_db(), path)


def _qualify(route_id, *slugs):
    database = get_db()
    database.execute(
        """
        INSERT INTO trail_identity_checkpoints
        SELECT id FROM route_checkpoints WHERE route_id = ? AND slug = ANY(?)
        """,
        (route_id, list(slugs)),
    )
    database.commit()


def _manifest(path, slugs):
    (path / "manifest.json").write_text(json.dumps({
        "format_version": 1, "route_slug": path.name, "trail_identity_checkpoint_slugs": slugs,
    }), encoding="utf-8")


def test_editorial_pool_and_route_configuration_are_atomic(app, tmp_path):
    with app.app_context():
        database = get_db()
        first = _geography("identity-first", ["gate", "camp", "summit"])
        second = _geography("identity-second", ["gate", "camp", "summit"])
        package = _valid(tmp_path, "identity-first")
        _manifest(package, ["gate"])
        _pool(tmp_path, ["Juniper"])
        with pytest.raises(ContentRejected, match="two active names"):
            import_route(database, package)
        with pytest.raises(psycopg.errors.CheckViolation, match="two active names"):
            _qualify(first, "gate")
        database.rollback()
        with pytest.raises(ContentRejected, match="duplicate normalized"):
            _pool(tmp_path, ["Night  Heron", " night heron "])
        _pool(tmp_path)
        _qualify(second, "gate")
        for invalid in (["elsewhere"], ["gate", "gate"], [{}]):
            _manifest(package, invalid)
            with pytest.raises(ContentRejected):
                import_route(database, package)
        _manifest(package, ["gate", "camp"])
        assert any(change.kind == "trail_identity" for change in import_route(database, package, dry_run=True))
        assert import_route(database, package)
        assert not any(change.kind == "trail_identity" for change in import_route(database, package))
        with pytest.raises(ContentRejected, match="two active names"):
            _pool(tmp_path, ["Juniper"])
        _manifest(package, [])
        import_route(database, package)
        assert database.execute(
            """
            SELECT route_id FROM trail_identity_checkpoints
            JOIN route_checkpoints ON id = checkpoint_id
            """
        ).fetchall() == [{"route_id": second}]
        assert database.execute("SELECT count(*) AS n FROM user_trail_identities").fetchone()["n"] == 0
        assert database.execute("SELECT count(*) AS n FROM trail_name_pool WHERE active").fetchone()["n"] == 2


@pytest.mark.parametrize("metrics", ["elevation", "distance", "combined"])
def test_first_reach_uses_all_thresholds_and_recovers_without_story(app, client, tmp_path, metrics):
    with app.app_context():
        _pool(tmp_path)
        target = {"elevation": feet_to_meters(Decimal(1000)), "distance": 10 * MILE_IN_METERS}
        fields = {"elevation": ("elevation_ft", "150"), "distance": ("distance_mi", "2")}
        codes = list(fields) if metrics == "combined" else [metrics]
        route_id = _route("identity-metrics", [(code, target[code]) for code in codes])
        for pos, slug, fraction in [(0, "gate", Decimal(".10")), (1, "camp", Decimal(".15")),
                                    (2, "summit", 1)]:
            _stop(route_id, slug, pos, [(code, target[code] * fraction) for code in codes])
        _publish(route_id)
        _qualify(route_id, "gate", "camp")
        user, token, _ = _register(client)
        journey = _start(client, token, "identity-metrics")
        headers = _auth(token)
        assert client.get("/api/v1/home", headers=headers).get_json()["you"]["trail_identity"] is None
        key, amount = fields[codes[0]]
        draft = {"happened_on": DAY, key: amount}
        assert client.post("/api/v1/activities/preview", json=draft, headers=headers).status_code == 200
        assert trail_identity.owner_identity(get_db(), user)["trail_identity"] is None
        saved = _credit(client, token, **{key: amount})
        if metrics == "combined":
            assert "trail_name_reveal" not in saved
            saved = _credit(client, token, distance_mi="2")
        reveal = saved["trail_name_reveal"]
        assert reveal["checkpoint_slug"] == "gate"
        assert saved["arrivals"] == []
        assert client.get(
            f"/api/v1/journeys/{journey['id']}/journal/gate", headers=headers
        ).status_code == 404
        restart_journey(user, route_id)
        assert client.get("/api/v1/home", headers=headers).get_json()["you"]["trail_name_reveal"] == reveal
        assert client.get("/api/v1/session", headers=headers).get_json()["you"]["trail_name_reveal"] == reveal
        first = client.post(PROFILE + "/reveal-acknowledgment", headers=headers).get_json()
        retry = client.post(PROFILE + "/reveal-acknowledgment", headers=headers).get_json()
        assert first == retry and first["acknowledged_at"]


def test_reveal_is_independent_of_journal_and_survives_relock(app, client, tmp_path):
    with app.app_context():
        _pool(tmp_path)
        route = _geography("identity-story", ["gate", "camp", "summit"])
        package = _valid(tmp_path, "identity-story")
        _manifest(package, ["gate"])
        import_route(get_db(), package)
        user, token, _ = _register(client)
        journey = _start(client, token, "identity-story")
        activity = _credit(client, token, elevation_ft="50")["activity"]["id"]
        journal = f"/api/v1/journeys/{journey['id']}/journal/gate"
        headers = _auth(token)
        updated = update_activity(user, activity, {"elevation_ft": "100"})
        assert client.get(journal, headers=headers).get_json()["trail_name_reveal"] == updated["trail_name_reveal"]
        assert client.post(journal + "/acknowledgment", headers=headers).status_code == 200
        assert trail_identity.owner_identity(get_db(), user)["trail_name_reveal"]
        _, outsider, _ = _register(client, "Outsider")
        assert client.get(journal, headers=_auth(outsider)).status_code == 403
        update_activity(user, activity, {"elevation_ft": "20"})
        assert client.get(journal, headers=headers).status_code == 404
        assert trail_identity.owner_identity(get_db(), user)["trail_name_reveal"]
        assert "trail_name_reveal" not in update_activity(user, activity, {"elevation_ft": "100"})
        assert trail_identity.owner_identity(get_db(), user)["trail_identity"]["reroll_available"]


def test_progress_and_feed_rollback_with_assignment_and_reroll_failure(app, client, tmp_path, monkeypatch):
    with app.app_context():
        _pool(tmp_path)
        route = _geography("identity-rollback", ["gate", "summit"])
        _qualify(route, "gate")
        user, token, _ = _register(client)
        create_private_expedition(user, slug="identity-board", name="Board", route_slug="identity-rollback")
        from adventurer import activities

        def fail_feed(*args):
            raise RuntimeError("feed write failed")

        with monkeypatch.context() as patch:
            patch.setattr(activities, "_write_create_feed", fail_feed)
            with pytest.raises(RuntimeError, match="feed write failed"):
                create_activity(user, {"elevation_ft": "150", "happened_on": DAY})
        database = get_db()
        for table in ("user_trail_identities", "activities", "expedition_updates"):
            assert database.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0
        _credit(client, token, elevation_ft="150")

        def fail_draw(*args):
            raise trail_identity._pool_unavailable()

        with monkeypatch.context() as patch:
            patch.setattr(trail_identity, "_draw", fail_draw)
            response = client.post(PROFILE + "/reroll", headers=_auth(token),
                                   json={"confirm": True, "request_key": "same-request"})
            assert response.status_code == 503
        assert trail_identity.owner_identity(database, user)["trail_identity"]["reroll_available"]


def test_assignment_and_reroll_serialize_for_one_account(app, client, tmp_path):
    with app.app_context():
        _pool(tmp_path)
        route = _geography("identity-race", ["gate", "camp", "summit"])
        _qualify(route, "gate", "camp")
        user, _, _ = _register(client)
        start_journey(user, route)

    def race(action):
        barrier = Barrier(2, timeout=10)

        def run():
            with app.app_context():
                barrier.wait()
                return action()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return [future.result(timeout=15) for future in (pool.submit(run), pool.submit(run))]

    saves = race(lambda: create_activity(user, {"happened_on": DAY, "elevation_ft": "150"}))
    reveals = [saved["trail_name_reveal"] for saved in saves if "trail_name_reveal" in saved]
    assert len(reveals) == 1
    results = race(lambda: trail_identity.change_identity(get_db(), user, request_key="retry"))
    assert results[0] == results[1]
    assert results[0]["trail_identity"]["name"] != reveals[0]["name"]
    with app.app_context(), pytest.raises(trail_identity.TrailIdentityError, match="already rerolled"):
        trail_identity.change_identity(get_db(), user, request_key="different")


def test_confirmed_eligibility_expansion_can_assign_identity(app, client, tmp_path):
    with app.app_context():
        _pool(tmp_path)
        route = _geography("identity-expansion", ["gate", "summit"])
        _qualify(route, "gate")
        user, token, _ = _register(client)
        expedition = create_private_expedition(
            user, slug="identity-restricted", name="Restricted", route_slug="identity-expansion",
            activity_policy={"mode": "restricted", "activity_types": ["hike"]},
        )
        journey = expedition["journey"]["id"]
        assert "trail_name_reveal" not in _credit(client, token, elevation_ft="150", activity_type="cycle")
        leave_expedition(user, "identity-restricted")
        with pytest.raises(Exception) as preview:
            choose_activity_policy(user, journey, "expand", None)
        assert trail_identity.owner_identity(get_db(), user)["trail_identity"] is None
        applied = choose_activity_policy(user, journey, "expand", preview.value.details["confirmation_token"])
        assert applied["trail_name_reveal"]["checkpoint_slug"] == "gate"


def test_existing_reach_and_rejected_summit_correction_do_not_assign(app, client, tmp_path):
    with app.app_context():
        _pool(tmp_path)
        route = _geography("identity-confirm", ["gate", "summit"])
        user, token, _ = _register(client)
        headers = _auth(token)
        _start(client, token, "identity-confirm")
        activity = _credit(client, token, elevation_ft="150")["activity"]["id"]
        _qualify(route, "gate", "summit")
        endpoint = f"/api/v1/activities/{activity}"
        assert client.patch(endpoint, headers=headers, json={"note": "Already reached."}).status_code == 200
        assert client.get("/api/v1/home", headers=headers).get_json()["you"]["trail_identity"] is None
        response = client.patch(endpoint, headers=headers, json={"elevation_ft": "1000"})
        assert response.status_code == 409
        token = response.get_json()["details"]["preview"]["confirmation_token"]
        assert trail_identity.owner_identity(get_db(), user)["trail_identity"] is None
        response = client.patch(endpoint, headers=headers, json={
            "elevation_ft": "1000", "confirmation_token": token,
        })
        assert response.status_code == 200
        assert response.get_json()["trail_name_reveal"]["checkpoint_slug"] == "summit"


def test_social_preferences_history_and_privacy(app, client, tmp_path, monkeypatch):
    cookie_client = app.test_client()
    owner, owner_token, csrf = _register(cookie_client, "Zed", cookie=True)
    assert cookie_client.patch(PROFILE, json={"trail_name_display": "trail_only"}).get_json()["code"] == "csrf"
    assert cookie_client.patch(PROFILE, json={"trail_name_display": "trail_only"},
                               headers={"X-CSRF-Token": csrf}).status_code == 200
    with app.app_context():
        _pool(tmp_path)
        route = _geography("identity-social", ["gate", "summit"])
        _qualify(route, "gate")
        monkeypatch.setattr(trail_identity, "choice", lambda choices: min(choices, key=lambda row: row["id"]))
        person, token, _ = _register(client, "Ada")
        create_private_expedition(owner, slug="identity-social", name="Board", route_slug="identity-social")
        assert client.get("/api/v1/expeditions/identity-social/leaderboard", headers=_auth(token)).status_code == 404
        invitation = create_share_link(owner, "identity-social")
        accept_invitation(person, token=invitation["token"])
        assert [row["user_id"] for row in get_leaderboard(owner, "identity-social")["entries"]] == [person, owner]
        _credit(client, token, elevation_ft="50")
        database = get_db()
        legacy = database.execute(
            """
            INSERT INTO expedition_updates (expedition_id, user_id, participation_id, kind, activity_id)
            SELECT expedition_id, user_id, participation_id, kind, activity_id
            FROM expedition_updates WHERE user_id = ? ORDER BY id LIMIT 1 RETURNING id
            """,
            (person,),
        ).fetchone()["id"]
        database.execute(
            """
            INSERT INTO expedition_update_metrics (update_id, metric_code, credited_amount)
            VALUES (?, 'elevation', 1)
            """,
            (legacy,),
        )
        database.commit()
        owner_name = _credit(client, owner_token, elevation_ft="150")["trail_name_reveal"]["name"]
        other_name = _credit(client, token, elevation_ft="150")["trail_name_reveal"]["name"]
        assert owner_name == other_name
        live = get_leaderboard(owner, "identity-social")["entries"]
        assert next(row for row in live if row["user_id"] == owner)["profile_name"] is None
        feed = get_updates(owner, "identity-social")
        assert any(row["checkpoint_name"] == "Gate" for row in feed)
        assert next(row for row in feed if row["id"] == legacy)["trail_name"] is None
        remove_participation_privacy(person, "identity-social", confirm=True)
        accept_invitation(person, token=invitation["token"])
        assert not [row for row in get_updates(owner, "identity-social") if row["user_id"] == person]
        assert trail_identity.owner_identity(database, person)["trail_identity"]["name"] == other_name
        archive_private_expedition(owner, "identity-social")
        before = get_leaderboard(owner, "identity-social")["entries"]
        rerolled = trail_identity.change_identity(get_db(), owner, request_key="reroll")
        assert rerolled["trail_identity"]["name"] != owner_name
        assert get_leaderboard(owner, "identity-social")["entries"] == before
        trail_identity.change_identity(get_db(), owner, preference="hidden")
        final = get_leaderboard(owner, "identity-social")["entries"]
        assert [(r["user_id"], r["final_rank"]) for r in final] == [(r["user_id"], r["final_rank"]) for r in before]
        assert next(r for r in final if r["user_id"] == owner)["display_name"] == "Adventurer"
        assert all(row["display_name"] == "Adventurer" for row in get_updates(owner, "identity-social")
                   if row["user_id"] == owner)
        remove_participation_privacy(person, "identity-social", confirm=True)
        database = get_db()
        assert trail_identity.owner_identity(database, person)["trail_identity"]["name"] == other_name
        assert not database.execute("SELECT 1 FROM expedition_updates WHERE user_id = ?", (person,)).fetchone()
        database.execute("UPDATE users SET deletion_state = 'deleting' WHERE id = ?", (person,))
        database.execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
        database.execute(
            "DELETE FROM journey_credits WHERE journey_id IN (SELECT id FROM route_journeys WHERE user_id = ?)",
            (person,),
        )
        database.execute("DELETE FROM users WHERE id = ?", (person,))
        database.commit()
        assert not database.execute("SELECT 1 FROM user_trail_identities WHERE user_id = ?", (person,)).fetchone()
        assert trail_identity.owner_identity(database, owner)["trail_identity"]["name"] == rerolled["trail_identity"]["name"]
