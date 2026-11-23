"""Journey lifecycle against the isolated test database.

Activity credits and expedition boards are not part of this phase.
Restart still repoints participations that already point at the journey,
because that write belongs to the restart transaction.
"""

from __future__ import annotations

import threading

import psycopg
import pytest

from adventurer.db import get_db
from adventurer.journeys import (
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
from adventurer.services import create_adventurer


def _user(name: str) -> int:
    return create_adventurer(name, "trailhead")["id"]


def _route(slug: str, status: str = "published") -> int:
    database = get_db()
    route_id = database.execute(
        """
        INSERT INTO routes (slug, name, region, summary, status)
        VALUES (?, ?, 'Test', 'A test route', 'draft')
        RETURNING id
        """,
        (slug, slug),
    ).fetchone()["id"]
    if status != "published":
        database.execute(
            "UPDATE routes SET status = ? WHERE id = ?",
            (status, route_id),
        )
        database.commit()
        return route_id
    database.execute(
        """
        INSERT INTO route_requirements (route_id, metric_code, target_amount)
        VALUES (?, 'elevation', 1000)
        """,
        (route_id,),
    )
    checkpoint_id = database.execute(
        """
        INSERT INTO route_checkpoints (route_id, slug, name, position)
        VALUES (?, 'summit', 'Summit', 0)
        RETURNING id
        """,
        (route_id,),
    ).fetchone()["id"]
    database.execute(
        """
        INSERT INTO checkpoint_requirements (
            checkpoint_id, route_id, metric_code, threshold_amount
        )
        VALUES (?, ?, 'elevation', 1000)
        """,
        (checkpoint_id, route_id),
    )
    database.execute("UPDATE routes SET status = 'published' WHERE id = ?", (route_id,))
    _check_route_shape(database)
    database.commit()
    return route_id


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


def _everest() -> int:
    return get_db().execute("SELECT id FROM routes WHERE slug = 'everest'").fetchone()["id"]


def _statuses(user_id: int, route_id: int) -> list[tuple]:
    return [
        (row["attempt_number"], row["status"], row["is_current"], row["id"])
        for row in list_journeys(user_id, route_id)
    ]


def _seed_current(user_id: int, route_id: int, status: str) -> int:
    database = get_db()
    row = database.execute(
        """
        INSERT INTO route_journeys (
            user_id, route_id, status, attempt_number, progress_version, is_current,
            started_at, completed_at, abandoned_at
        )
        VALUES (
            ?, ?, ?, 1, 1, true, now(),
            CASE WHEN ? = 'completed' THEN now() ELSE NULL END,
            CASE WHEN ? = 'abandoned' THEN now() ELSE NULL END
        )
        RETURNING id
        """,
        (user_id, route_id, status, status, status),
    ).fetchone()
    database.commit()
    return row["id"]


def _credit(user_id: int, journey_id: int) -> None:
    database = get_db()
    activity_id = database.execute(
        """
        INSERT INTO activities (user_id, activity_date, elevation_gain_m, note)
        VALUES (?, DATE '2026-01-01', 100, 'kept')
        RETURNING id
        """,
        (user_id,),
    ).fetchone()["id"]
    database.execute(
        """
        INSERT INTO activity_verification_events (
            activity_id, from_status, to_status, reason, actor_type
        )
        VALUES (
            ?, NULL, 'not_required',
            'Manual entry. Manual entries are not independently verified.',
            'system'
        )
        """,
        (activity_id,),
    )
    route_id = database.execute(
        "SELECT route_id FROM route_journeys WHERE id = ?",
        (journey_id,),
    ).fetchone()["route_id"]
    assignment_id = database.execute(
        """
        INSERT INTO activity_journey_assignments (
            activity_id, journey_id, user_id, route_id
        )
        VALUES (?, ?, ?, ?)
        RETURNING id
        """,
        (activity_id, journey_id, user_id, route_id),
    ).fetchone()["id"]
    database.execute(
        """
        INSERT INTO journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (?, ?, ?, 'elevation', 100)
        """,
        (assignment_id, journey_id, route_id),
    )
    database.commit()


def _participation(user_id: int, expedition_id: int, route_id: int, journey_id: int, status: str = "active") -> int:
    database = get_db()
    row = database.execute(
        """
        INSERT INTO expedition_participations (
            user_id, expedition_id, route_id, journey_id, status
        )
        VALUES (?, ?, ?, ?, ?)
        RETURNING id
        """,
        (user_id, expedition_id, route_id, journey_id, status),
    ).fetchone()
    database.commit()
    return row["id"]


def _private_expedition(slug: str, route_id: int, owner_id: int, status: str) -> int:
    database = get_db()
    row = database.execute(
        """
        INSERT INTO expeditions (
            slug, name, route_id, visibility, status, kind, owner_user_id
        )
        VALUES (?, ?, ?, 'private', ?, 'private', ?)
        RETURNING id
        """,
        (slug, slug, route_id, status, owner_id),
    ).fetchone()
    database.commit()
    return row["id"]


def _pointed_journey(participation_id: int) -> int:
    return get_db().execute(
        "SELECT journey_id FROM expedition_participations WHERE id = ?",
        (participation_id,),
    ).fetchone()["journey_id"]


def test_first_journey_is_attempt_one(app):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        created = start_journey(user_id, everest)
        assert created["status"] == "active"
        assert created["is_current"] is True
        assert created["attempt_number"] == 1
        assert created["progress_version"] == 1
        assert created["route_slug"] == "everest"
        assert created["started_at"] is not None
        assert active_journey(user_id)["id"] == created["id"]
        assert _statuses(user_id, everest) == [(1, "active", True, created["id"])]
        credits = get_db().execute(
            "SELECT COUNT(*) AS n FROM journey_credits WHERE journey_id = ?",
            (created["id"],),
        ).fetchone()
        assert credits["n"] == 0


def test_switch_pauses_the_prior_journey_and_switch_back_resumes_it(app):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        kili = _route("kili")
        first = start_journey(user_id, everest)
        get_db().execute(
            "UPDATE route_journeys SET progress_version = 4 WHERE id = ?",
            (first["id"],),
        )
        get_db().commit()
        second = switch_journey(user_id, kili)
        paused = list_journeys(user_id, everest)[0]
        assert second["route_slug"] == "kili"
        assert second["attempt_number"] == 1
        assert second["status"] == "active"
        assert paused["id"] == first["id"]
        assert paused["status"] == "paused"
        assert paused["is_current"] is True
        assert paused["progress_version"] == 4
        assert paused["paused_at"] is not None
        resumed = switch_journey(user_id, everest)
        assert resumed["id"] == first["id"]
        assert resumed["status"] == "active"
        assert resumed["attempt_number"] == 1
        assert resumed["progress_version"] == 4
        assert list_journeys(user_id, kili)[0]["status"] == "paused"
        switch_journey(user_id, kili)
        resumed_again = resume_journey(user_id, everest)
        assert resumed_again["id"] == first["id"]
        assert resumed_again["status"] == "active"
        assert active_journey(user_id)["route_slug"] == "everest"


def test_multiple_attempts_stay_on_the_route(app):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        start_journey(user_id, everest)
        abandon_journey(user_id, everest)
        restart_journey(user_id, everest)
        restart_journey(user_id, everest)
        attempts = list_journeys(user_id, everest)
        assert [(row["attempt_number"], row["status"], row["is_current"]) for row in attempts] == [
            (1, "abandoned", False),
            (2, "abandoned", False),
            (3, "active", True),
        ]
        assert active_journey(user_id)["attempt_number"] == 3


def test_switch_to_a_completed_journey_requires_restart(app):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        kili = _route("kili")
        completed_id = _seed_current(user_id, everest, "completed")
        active = start_journey(user_id, kili)
        with pytest.raises(JourneyError) as caught:
            switch_journey(user_id, everest)
        assert caught.value.code == "restart_required"
        assert caught.value.details["route_slug"] == "everest"
        assert caught.value.details["status"] == "completed"
        assert caught.value.details["reset_expedition_slugs"] == []
        assert active_journey(user_id)["id"] == active["id"]
        assert _statuses(user_id, everest) == [(1, "completed", True, completed_id)]


def test_switch_to_an_abandoned_journey_requires_restart(app):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        kili = _route("kili")
        abandoned_id = _seed_current(user_id, everest, "abandoned")
        active = start_journey(user_id, kili)
        with pytest.raises(JourneyError) as caught:
            switch_journey(user_id, everest)
        with pytest.raises(JourneyError) as resumed:
            resume_journey(user_id, everest)
        assert caught.value.code == "restart_required"
        assert resumed.value.code == "restart_required"
        assert caught.value.details["status"] == "abandoned"
        assert active_journey(user_id)["id"] == active["id"]
        assert _statuses(user_id, everest) == [(1, "abandoned", True, abandoned_id)]


def test_restart_creates_a_new_attempt_and_preserves_history(app):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        kili = _route("kili")
        completed = start_journey(user_id, everest)
        completed_id = completed["id"]
        _credit(user_id, completed_id)
        get_db().execute(
            """
            UPDATE route_journeys
            SET status = 'completed', completed_at = now()
            WHERE id = ?
            """,
            (completed_id,),
        )
        get_db().commit()
        other = start_journey(user_id, kili)
        result = restart_journey(user_id, everest)
        assert result["paused_journey"]["id"] == other["id"]
        assert result["paused_journey"]["status"] == "paused"
        assert result["paused_journey"]["is_current"] is True
        created = result["journey"]
        assert created["attempt_number"] == 2
        assert created["status"] == "active"
        assert created["is_current"] is True
        assert created["progress_version"] == 1
        assert result["reset_expedition_slugs"] == []
        attempts = list_journeys(user_id, everest)
        assert attempts[0]["id"] == completed_id
        assert attempts[0]["status"] == "completed"
        assert attempts[0]["is_current"] is False
        assert attempts[1]["id"] == created["id"]
        credited = get_db().execute(
            """
            SELECT credit.journey_id
            FROM journey_credits AS credit
            JOIN activity_journey_assignments AS assignment
                ON assignment.id = credit.assignment_id
            WHERE assignment.user_id = ?
            """,
            (user_id,),
        ).fetchone()
        assert credited["journey_id"] == completed_id
        fresh = get_db().execute(
            "SELECT COUNT(*) AS n FROM journey_credits WHERE journey_id = ?",
            (created["id"],),
        ).fetchone()
        assert fresh["n"] == 0


def test_restart_abandons_an_active_or_paused_attempt(app):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        kili = _route("kili")
        active = start_journey(user_id, everest)
        restarted = restart_journey(user_id, everest)
        assert restarted["paused_journey"] is None
        assert _statuses(user_id, everest) == [
            (1, "abandoned", False, active["id"]),
            (2, "active", True, restarted["journey"]["id"]),
        ]
        paused = start_journey(user_id, kili)
        restarted_paused = restart_journey(user_id, everest)
        kili_row = list_journeys(user_id, kili)[0]
        assert kili_row["id"] == paused["id"]
        assert kili_row["status"] == "paused"
        assert kili_row["is_current"] is True
        assert restarted_paused["paused_journey"]["id"] == paused["id"]
        everest_rows = list_journeys(user_id, everest)
        assert everest_rows[1]["status"] == "abandoned"
        assert everest_rows[1]["is_current"] is False
        assert everest_rows[2]["status"] == "active"
        assert active_journey(user_id)["id"] == everest_rows[2]["id"]


def test_restart_repoints_only_active_expeditions(app):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        kili = _route("kili")
        personal = start_journey(user_id, kili)
        live = _private_expedition("live-kili", kili, user_id, "active")
        archived = _private_expedition("old-kili", kili, user_id, "archived")
        departed = _private_expedition("left-kili", kili, user_id, "active")
        live_participation = _participation(user_id, live, kili, personal["id"])
        archived_participation = _participation(user_id, archived, kili, personal["id"])
        departed_participation = _participation(
            user_id, departed, kili, personal["id"], status="withdrawn"
        )
        other = start_journey(user_id, everest)
        global_expedition = get_db().execute(
            "SELECT id FROM expeditions WHERE slug = 'everest'"
        ).fetchone()["id"]
        global_participation = _participation(user_id, global_expedition, everest, other["id"])
        result = restart_journey(user_id, kili)
        assert result["reset_expedition_slugs"] == ["live-kili"]
        assert result["paused_journey"]["id"] == other["id"]
        assert _pointed_journey(live_participation) == result["journey"]["id"]
        assert _pointed_journey(archived_participation) == personal["id"]
        assert _pointed_journey(departed_participation) == personal["id"]
        assert _pointed_journey(global_participation) == other["id"]
        assert list_journeys(user_id, everest)[0]["status"] == "paused"


def test_abandon_leaves_no_active_journey(app):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        kili = _route("kili")
        started = start_journey(user_id, everest)
        abandoned = abandon_journey(user_id, everest)
        assert abandoned["id"] == started["id"]
        assert abandoned["status"] == "abandoned"
        assert abandoned["is_current"] is True
        assert abandoned["abandoned_at"] is not None
        assert active_journey(user_id) is None
        with pytest.raises(JourneyError) as caught:
            switch_journey(user_id, everest)
        assert caught.value.code == "restart_required"
        denali = _route("denali")
        paused = start_journey(user_id, kili)
        start_journey(user_id, denali)
        abandon_journey(user_id, denali)
        assert active_journey(user_id) is None
        assert list_journeys(user_id, kili)[0]["id"] == paused["id"]
        assert list_journeys(user_id, kili)[0]["status"] == "paused"
        abandon_journey(user_id, kili)
        assert list_journeys(user_id, kili)[0]["status"] == "abandoned"
        assert list_journeys(user_id, kili)[0]["is_current"] is True
        assert active_journey(user_id) is None


def test_completed_journey_cannot_be_resumed_or_abandoned(app):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        kili = _route("kili")
        completed_id = _seed_current(user_id, everest, "completed")
        active = start_journey(user_id, kili)
        with pytest.raises(JourneyError) as resumed:
            resume_journey(user_id, everest)
        assert resumed.value.code == "restart_required"
        with pytest.raises(JourneyError) as abandoned:
            abandon_journey(user_id, everest)
        assert abandoned.value.code == "invalid_state"
        assert active_journey(user_id)["id"] == active["id"]
        assert _statuses(user_id, everest) == [(1, "completed", True, completed_id)]


def test_two_users_keep_independent_journeys_on_one_route(app):
    with app.app_context():
        sam = _user("Sam")
        alex = _user("Alex")
        everest = _everest()
        sam_journey = start_journey(sam, everest)
        alex_journey = start_journey(alex, everest)
        assert sam_journey["id"] != alex_journey["id"]
        assert active_journey(sam)["status"] == "active"
        assert active_journey(alex)["status"] == "active"
        assert active_journey(sam)["route_id"] == everest
        assert active_journey(alex)["route_id"] == everest


def test_invalid_routes_and_unauthorized_operations(app):
    with app.app_context():
        sam = _user("Sam")
        alex = _user("Alex")
        everest = _everest()
        draft = _route("draft-ridge", status="draft")
        started = start_journey(sam, everest)
        for route_id in (0, 999_999_999):
            with pytest.raises(JourneyError) as caught:
                start_journey(sam, route_id)
            assert caught.value.code == "not_found"
        with pytest.raises(JourneyError) as draft_start:
            start_journey(sam, draft)
        assert draft_start.value.code == "not_found"
        with pytest.raises(JourneyError) as missing:
            get_journey(sam, 999_999_999)
        assert missing.value.code == "not_found"
        with pytest.raises(JourneyError) as forbidden:
            get_journey(alex, started["id"])
        assert forbidden.value.code == "forbidden"
        with pytest.raises(JourneyError) as absent:
            abandon_journey(alex, everest)
        assert absent.value.code == "not_found"
        assert active_journey(sam)["id"] == started["id"]
        get_db().execute(
            "UPDATE users SET deletion_state = 'deleting' WHERE id = ?",
            (alex,),
        )
        get_db().commit()
        with pytest.raises(JourneyError) as deleting:
            start_journey(alex, everest)
        assert deleting.value.code == "forbidden"


def test_restart_repoint_failure_rolls_the_journey_back(app, monkeypatch):
    with app.app_context():
        user_id = _user("Sam")
        kili = _route("kili")
        original = start_journey(user_id, kili)
        expedition_id = _private_expedition("live-kili", kili, user_id, "active")
        participation_id = _participation(user_id, expedition_id, kili, original["id"])
        database = get_db()
        query = database.execute

        def fail_repoint(sql, params=()):
            if "SET journey_id" in sql:
                raise RuntimeError("repoint failed")
            return query(sql, params)

        monkeypatch.setattr(database, "execute", fail_repoint)
        with pytest.raises(RuntimeError, match="repoint failed"):
            restart_journey(user_id, kili)
        monkeypatch.undo()
        rows = list_journeys(user_id, kili)
        assert [(row["id"], row["status"], row["is_current"], row["attempt_number"]) for row in rows] == [
            (original["id"], "active", True, 1)
        ]
        assert _pointed_journey(participation_id) == original["id"]


def test_commit_failure_rolls_back_and_leaves_the_connection_usable(app, monkeypatch):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        kili = _route("kili")
        started = start_journey(user_id, everest)
        database = get_db()
        rollbacks = []
        original_rollback = database.rollback

        def fail_commit():
            raise RuntimeError("commit failed")

        def spy_rollback():
            rollbacks.append(True)
            return original_rollback()

        monkeypatch.setattr(database, "commit", fail_commit)
        monkeypatch.setattr(database, "rollback", spy_rollback)
        with pytest.raises(RuntimeError, match="commit failed") as caught:
            switch_journey(user_id, kili)
        assert caught.value.__cause__ is None
        assert rollbacks
        monkeypatch.undo()
        current = active_journey(user_id)
        assert current["id"] == started["id"]
        assert current["status"] == "active"
        assert list_journeys(user_id, kili) == []
        retried = switch_journey(user_id, kili)
        assert retried["status"] == "active"
        assert retried["route_slug"] == "kili"
        assert list_journeys(user_id, everest)[0]["status"] == "paused"


def test_a_rollback_failure_keeps_the_original_commit_exception(app, monkeypatch):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        kili = _route("kili")
        start_journey(user_id, everest)
        database = get_db()

        def fail_commit():
            raise RuntimeError("commit failed")

        def fail_rollback():
            raise RuntimeError("rollback failed")

        monkeypatch.setattr(database, "commit", fail_commit)
        monkeypatch.setattr(database, "rollback", fail_rollback)
        try:
            with pytest.raises(RuntimeError, match="commit failed") as caught:
                switch_journey(user_id, kili)
            assert str(caught.value.__cause__) == "rollback failed"
        finally:
            monkeypatch.undo()
            database.rollback()


def test_a_failed_transition_rolls_back(app, monkeypatch):
    with app.app_context():
        user_id = _user("Sam")
        everest = _everest()
        kili = _route("kili")
        started = start_journey(user_id, everest)
        database = get_db()
        original = database.execute

        def fail_insert(sql, params=()):
            if "INSERT INTO route_journeys" in sql:
                raise psycopg.errors.UniqueViolation("duplicate journey")
            return original(sql, params)

        monkeypatch.setattr(database, "execute", fail_insert)
        with pytest.raises(JourneyError) as caught:
            switch_journey(user_id, kili)
        assert caught.value.code == "conflict"
        monkeypatch.undo()
        current = active_journey(user_id)
        assert current["id"] == started["id"]
        assert current["status"] == "active"
        assert list_journeys(user_id, kili) == []


def test_concurrent_starts_do_not_create_two_active_journeys(app):
    with app.app_context():
        user_id = _user("Sam")
        other_id = _user("Alex")
        everest = _everest()
        kili = _route("kili")
        get_db().rollback()
    lock = threading.Lock()

    def run(subject_id: int, route_id: int, barrier: threading.Barrier, outcome: dict) -> None:
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                start_journey(subject_id, route_id)
                with lock:
                    outcome["ok"] += 1
            except JourneyError as exc:
                with lock:
                    outcome["codes"].append(exc.code)
            except Exception as exc:
                with lock:
                    outcome["errors"].append(repr(exc))
            finally:
                get_db().close()

    def finish(threads: list[threading.Thread]) -> None:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive()

    different = {"ok": 0, "codes": [], "errors": []}
    different_barrier = threading.Barrier(2)
    finish(
        [
            threading.Thread(target=run, args=(user_id, everest, different_barrier, different)),
            threading.Thread(target=run, args=(user_id, kili, different_barrier, different)),
        ]
    )
    with app.app_context():
        rows = list_journeys(user_id)
        assert different["errors"] == []
        assert different["codes"] == []
        assert different["ok"] == 2
        assert sorted(row["status"] for row in rows) == ["active", "paused"]

        same = {"ok": 0, "codes": [], "errors": []}
        same_barrier = threading.Barrier(2)
        finish(
            [
                threading.Thread(target=run, args=(other_id, everest, same_barrier, same)),
                threading.Thread(target=run, args=(other_id, everest, same_barrier, same)),
            ]
        )
        assert same["errors"] == []
        assert same["ok"] == 1
        assert same["codes"] == ["invalid_state"]
        follow = list_journeys(other_id, everest)
        assert len(follow) == 1
        assert follow[0]["status"] == "active"
