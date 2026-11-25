from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import psycopg
import pytest
from psycopg.types.json import Jsonb

from adventurer import achievement_engine
from adventurer.achievements import delete_earned_achievements
from adventurer.activities import create_activity, preview_activity, preview_activity_update, update_activity
from adventurer.db import get_db
from adventurer.expeditions import (
    archive_private_expedition, create_private_expedition, remove_participation_privacy,
)
from adventurer.journeys import JourneyError, abandon_journey, choose_activity_policy, restart_journey, start_journey
from adventurer.services import create_adventurer

from test_achievement_foundation import _edition
from test_expeditions import _route
from test_narrative_api import DAY, _auth, _credit, _register, _start
from test_trail_identity import _pool, _qualify


def _definition(route, slug, rule="once_per_account", *, criteria=None, status="active", version=1):
    return get_db().execute(
        """
        INSERT INTO achievement_definitions (
            slug, name, description, scope, source, criteria, status, presentation,
            evaluator_key, evaluator_version, evaluator_route_id, issuance_rule
        )
        VALUES (?, ?, 'A historical award.', 'route', 'route_completion', ?, ?,
                '{"badge": "ridge", "asset_ref": "medal", "extra": ["detail"]}',
                'route_completion', ?, ?, ?)
        RETURNING id
        """,
        (slug, slug.title(), Jsonb({} if criteria is None else criteria), status, version, route, rule),
    ).fetchone()["id"]


def _finish(user):
    fields = {"happened_on": DAY, "elevation_ft": "100", "activity_type": "hike"}
    token = preview_activity(user, fields)["confirmation_token"]
    return create_activity(user, fields, confirmation_token=token)


def test_completion_rules_numbering_and_private_trophy_case(app, client):
    with app.app_context():
        database = get_db()
        route = _route("award-climb", "100")
        other_route = _route("no-awards", "100")
        user, token, _ = _register(client)
        _, other_token, _ = _register(client, "Other")
        headers = _auth(token)

        _start(client, token, "award-climb")
        abandon_journey(user, route)
        restart_journey(user, route)
        assert _credit(client, token, elevation_ft=100)["newly_earned_achievements"] == []
        _definition(route, "once")
        _definition(route, "each", "once_per_event")
        _definition(route, "second", criteria={"completion_number": 2})
        _definition(route, "inactive", status="inactive")
        _definition(route, "future", criteria={"completion_number": 50})
        limited = _definition(route, "limited", "once_per_account_per_edition")
        edition = _edition(database, limited, "early", 1)
        _edition(database, limited, "later", 2)
        database.commit()
        assert client.get("/api/v1/trophy-case", headers=headers).get_json()["awards"] == []

        restart_journey(user, route)
        second = _credit(client, token, elevation_ft=100)
        first_awards = second["newly_earned_achievements"]
        assert [award["definition_slug"] for award in first_awards] == ["once", "each", "second", "limited", "limited"]
        assert all(award["completion_number"] == 2 and award["completed_at"] for award in first_awards)
        assert first_awards[-2]["edition"] == {"slug": "early", "sequence_number": 1, "size": 1}
        assert achievement_engine.finalize_route_completion(database, second["journey"]["id"]) == []
        database.rollback()
        attached = create_private_expedition(user, slug="completed-attachment", name="Attachment",
                                             route_slug="award-climb")
        assert attached["journey"]["id"] == second["journey"]["id"]
        assert "newly_earned_achievements" not in attached

        database.execute("UPDATE routes SET name = 'Renamed climb' WHERE id = ?", (route,))
        database.execute("UPDATE achievement_definitions SET name = 'Edited' WHERE slug = 'once'")
        database.commit()
        restart_journey(user, route)
        third = _credit(client, token, elevation_ft=100)["newly_earned_achievements"]
        assert [(award["definition_slug"], award["completion_number"]) for award in third] == [("each", 3)]
        restart_journey(user, route)
        assert _credit(client, token, elevation_ft=100)["newly_earned_achievements"][0]["completion_number"] == 4
        start_journey(user, other_route)
        assert _finish(user)["newly_earned_achievements"] == []
        assert database.execute(
            "SELECT completion_number FROM route_completions WHERE user_id = ? AND route_key = ?",
            (user, other_route),
        ).fetchone()["completion_number"] == 1

        home = client.get("/api/v1/home", headers=headers).get_json()["trophy_case"]
        assert (home["earned_count"], home["pending_reveal_count"], len(home["pending_awards"])) == (7, 7, 3)
        page = client.get("/api/v1/trophy-case?limit=2&pending_only=true", headers=headers).get_json()
        assert page["route_collections"] == [{
            "collection_key": route, "title": "Renamed climb", "award_count": 5, "completion_count": 4,
        }]
        ids = [award["id"] for award in page["awards"]]
        cursor = page["next_cursor"]
        assert client.get("/api/v1/trophy-case", query_string={"cursor": cursor},
                          headers=headers).status_code == 400
        while cursor:
            page = client.get("/api/v1/trophy-case", query_string={
                "limit": 2, "pending_only": "true", "cursor": cursor,
            }, headers=headers).get_json()
            ids.extend(award["id"] for award in page["awards"])
            cursor = page["next_cursor"]
        assert len(ids) == len(set(ids)) == 7

        endpoint = f"/api/v1/trophy-case/{first_awards[0]['id']}"
        detail = client.get(endpoint, headers=headers).get_json()["award"]
        assert detail["name"] == "Once"
        assert detail["context"]["route"]["slug"] == "award-climb"
        assert detail["presentation"]["extra"] == ["detail"]
        assert first_awards[0]["presentation"] == {"badge": "ridge", "asset_ref": "medal"}
        assert client.get(endpoint, headers=_auth(other_token)).status_code == 404
        assert client.post(endpoint + "/reveal-acknowledgment", headers=_auth(other_token)).status_code == 404
        ack = client.post(endpoint + "/reveal-acknowledgment", headers=headers).get_json()
        assert ack["acknowledged_at"] and client.post(
            endpoint + "/reveal-acknowledgment", headers=headers,
        ).get_json() == ack
        assert first_awards[0]["id"] not in {
            award["id"] for award in client.get("/api/v1/trophy-case?pending_only=true",
                                               headers=headers).get_json()["awards"]
        }
        assert database.execute("SELECT issued_count FROM achievement_editions WHERE id = ?", (edition,)).fetchone()[
            "issued_count"
        ] == 1


@pytest.mark.parametrize("criteria,version,rule", [
    ({"extra": 1}, 1, "once_per_account"), ({"completion_number": True}, 1, "once_per_account"),
    ({"completion_number": 0}, 1, "once_per_event"), ({}, 2, "once_per_account"),
    ({}, 1, "unlimited_repeats"),
])
def test_invalid_configuration(app, criteria, version, rule):
    with app.app_context():
        route = _route("invalid-award")
        with pytest.raises(psycopg.errors.CheckViolation):
            _definition(route, "invalid", rule, criteria=criteria, version=version)
        get_db().rollback()


@pytest.mark.parametrize("producer", ["measurement", "type", "expansion"])
def test_confirmed_completion_producers(app, producer):
    with app.app_context():
        route = _route("engine-path", "100")
        _definition(route, "path")
        get_db().commit()
        user = create_adventurer("Summiter", "trailhead")["id"]
        if producer == "measurement":
            start_journey(user, route)
            before_type = "hike"
        else:
            create_private_expedition(user, slug="hike-only", name="Hike", route_slug="engine-path",
                                      activity_policy={"mode": "restricted", "activity_types": ["hike"]})
            before_type = "cycle"
        activity = create_activity(user, {
            "happened_on": DAY, "elevation_ft": "50" if producer == "measurement" else "100",
            "activity_type": before_type,
        })
        if producer == "expansion":
            archive_private_expedition(user, "hike-only")
            journey_id = activity["journey"]["id"]
            with pytest.raises(JourneyError) as preview:
                choose_activity_policy(user, journey_id, "expand", None)
            confirm = lambda: choose_activity_policy(
                user, journey_id, "expand", preview.value.details["confirmation_token"]
            )
        else:
            changes = {"elevation_ft": "100"} if producer == "measurement" else {"activity_type": "hike"}
            activity_id = activity["activity"]["id"]
            preview = preview_activity_update(user, activity_id, changes)
            confirm = lambda: update_activity(
                user, activity_id, changes, confirmation_token=preview["confirmation_token"]
            )
        assert get_db().execute("SELECT count(*) AS n FROM route_completions").fetchone()["n"] == 0
        saved = confirm()
        assert [award["definition_slug"] for award in saved["newly_earned_achievements"]] == ["path"]


def test_edition_contention_and_closed_cohort_do_not_block_completion(app):
    with app.app_context():
        database = get_db()
        route = _route("engine-race", "100")
        limited = _definition(route, "limited", "once_per_account_per_edition")
        edition = _edition(database, limited, "last-slot", 1)
        closed = _edition(database, limited, "closed", 1)
        database.execute("UPDATE achievement_editions SET available_until = now() - interval '1 day' WHERE id = ?",
                         (closed,))
        _definition(route, "always", "once_per_event")
        database.commit()
        users = [create_adventurer(name, "trailhead")["id"] for name in ("ClimberOne", "ClimberTwo")]
        for user in users:
            start_journey(user, route)
    ready = Barrier(2, timeout=10)

    def finish(user):
        with app.app_context():
            ready.wait()
            return _finish(user)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=20) for future in [pool.submit(finish, user) for user in users]]
    assert all(result["journey"]["status"] == "completed" for result in results)
    assert sorted(len(result["newly_earned_achievements"]) for result in results) == [1, 2]
    with app.app_context():
        assert get_db().execute("SELECT edition_id FROM earned_achievements WHERE edition_id IS NOT NULL").fetchall() == [
            {"edition_id": edition}
        ]


def test_completion_failure_rolls_back_and_account_erases_private_history(app, tmp_path, monkeypatch):
    with app.app_context():
        database = get_db()
        route = _route("engine-rollback", "100")
        _pool(tmp_path)
        _qualify(route, "summit")
        limited = _definition(route, "limited", "once_per_account_per_edition")
        edition = _edition(database, limited, "solo", 1)
        database.commit()
        user = create_adventurer("RollbackOwner", "trailhead")["id"]
        expedition = create_private_expedition(user, slug="rollback-owner", name="Board", route_slug="engine-rollback")
        issued = achievement_engine._issue

        def fail(*args):
            assert issued(*args)
            raise RuntimeError("post-issuance failure")

        with monkeypatch.context() as patch:
            patch.setattr(achievement_engine, "_issue", fail)
            with pytest.raises(RuntimeError, match="post-issuance failure"):
                _finish(user)
        for table in ("activities", "journey_credits", "expedition_updates", "user_trail_identities",
                      "route_completions", "earned_achievements", "achievement_reveals"):
            assert database.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0
        assert database.execute("SELECT status FROM route_journeys WHERE id = ?",
                                (expedition["journey"]["id"],)).fetchone()["status"] == "active"
        assert database.execute("SELECT issued_count FROM achievement_editions WHERE id = ?",
                                (edition,)).fetchone()["issued_count"] == 0
        success = _finish(user)["newly_earned_achievements"][0]
        assert (success["completion_number"], success["edition"]["sequence_number"]) == (1, 1)
        for query in (
            "DELETE FROM user_trail_identities",
            "TRUNCATE trail_name_pool, trail_identity_checkpoints, user_trail_identities",
            "UPDATE route_completions SET completion_number = 9",
            "DELETE FROM route_completions",
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                database.execute(query)
            database.rollback()
        remove_participation_privacy(user, "rollback-owner", confirm=True)
        assert database.execute("SELECT id FROM earned_achievements WHERE user_id = ?", (user,)).fetchone()
        assert database.execute("SELECT user_id FROM user_trail_identities WHERE user_id = ?", (user,)).fetchone()
        database.execute("UPDATE users SET deletion_state = 'deleting' WHERE id = ?", (user,))
        database.execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
        delete_earned_achievements(database, user)
        database.execute("DELETE FROM journey_credits WHERE journey_id = ?", (expedition["journey"]["id"],))
        database.execute("DELETE FROM activity_journey_assignments WHERE journey_id = ?",
                         (expedition["journey"]["id"],))
        database.execute("DELETE FROM users WHERE id = ?", (user,))
        database.commit()
        for table in ("user_trail_identities", "route_completions", "earned_achievements", "achievement_reveals"):
            assert not database.execute(f"SELECT 1 FROM {table}").fetchone()
        assert database.execute("SELECT issued_count FROM achievement_editions WHERE id = ?",
                                (edition,)).fetchone()["issued_count"] == 1


def test_upgrade_backfills_numbers_without_dispatching(app):
    with app.app_context():
        database = get_db()
        route = _route("backfill-climb", "100")
        user = create_adventurer("PriorClimber", "trailhead")["id"]
        start_journey(user, route)
        attempt = _finish(user)["journey"]
        sql = Path(__file__).resolve().parents[1] / "migrations" / "sql"
        try:
            database.execute("DROP TRIGGER earned_achievement_event_time ON adventurer.earned_achievements")
            database.raw.execute((sql / "0018_achievement_engine_down.sql").read_text(encoding="utf-8"))
            database.raw.execute((sql / "0018_achievement_engine.sql").read_text(encoding="utf-8"))
            assert database.execute(
                "SELECT journey_id, completion_number FROM route_completions WHERE user_id = ?", (user,)
            ).fetchall() == [{"journey_id": attempt["id"], "completion_number": 1}]
            assert not database.execute("SELECT 1 FROM earned_achievements").fetchone()
            assert not database.execute("SELECT 1 FROM achievement_reveals").fetchone()
        finally:
            database.rollback()


def test_edition_eligibility_uses_the_authoritative_completion_time(app):
    with app.app_context():
        database = get_db()
        route = _route("event-time", "100")
        user = create_adventurer("EventClimber", "trailhead")["id"]
        start_journey(user, route)
        journey = _finish(user)["journey"]
        event = f"route_completed:{journey['id']}"
        definition = _definition(route, "timestamp", "once_per_account_per_edition")
        closed = _edition(database, definition, "prior", 1)
        current = _edition(database, definition, "current", 1)
        database.execute(
            "UPDATE achievement_editions SET eligible_until = now() - interval '1 day' WHERE id = ?", (closed,),
        )
        database.execute(
            "UPDATE achievement_editions SET eligible_from = now() - interval '1 day' WHERE id = ?", (current,),
        )
        database.commit()
        insert = """
            INSERT INTO earned_achievements (
                user_id, definition_id, edition_id, route_id, source,
                event_key, completion_event_key, event_at
            ) VALUES (?, ?, ?, ?, 'route_completion', ?, ?, now() - interval '2 days')
            RETURNING event_at
        """
        with pytest.raises(psycopg.errors.CheckViolation, match="outside its issuance window"):
            database.execute(insert, (user, definition, closed, route, event, event))
        database.rollback()
        earned = database.execute(insert, (user, definition, current, route, event, event)).fetchone()
        expected = database.execute(
            "SELECT completed_at FROM route_completions WHERE event_key = ?", (event,),
        ).fetchone()
        assert earned["event_at"] == expected["completed_at"]
        database.commit()
