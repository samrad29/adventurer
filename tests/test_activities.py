"""Activity records, journey credits, and summit confirmation.

Credits advance one journey. The activity keeps the elevation that was logged.
Expedition feed rows are written only for the journey that receives the credit.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest

from adventurer.activities import (
    MANUAL_EVENT_REASON,
    ActivityError,
    _issue_confirmation_token,
    create_activity,
    delete_activity,
    get_activity,
    list_activities,
    preview_activity,
    preview_activity_update,
    update_activity,
)
from adventurer.db import DB, get_db
from adventurer.journeys import abandon_journey, restart_journey, start_journey, switch_journey
from adventurer.measures import feet_to_meters, kilometers_to_meters
from adventurer.services import create_adventurer

DAY = date(2024, 6, 1)


def _ft(feet: str) -> Decimal:
    return feet_to_meters(Decimal(feet))


def _fields(feet: str, **extra) -> dict:
    payload = {"elevation_ft": Decimal(feet), "happened_on": DAY}
    payload.update(extra)
    return payload


def _user(name: str) -> int:
    return create_adventurer(name, "trailhead")["id"]


def _route(slug: str, feet: str = "1000") -> int:
    database = get_db()
    row = database.execute(
        """
        INSERT INTO routes (slug, name, region, summary, target_elevation_m, status)
        VALUES (?, ?, 'Test', 'A test route', ?, 'published')
        RETURNING id
        """,
        (slug, slug, _ft(feet)),
    ).fetchone()
    database.commit()
    return row["id"]


def _checkpoint(route_id: int, slug: str, name: str, position: int, feet: str) -> None:
    database = get_db()
    database.execute(
        """
        INSERT INTO route_checkpoints (route_id, slug, name, position, elevation_m)
        VALUES (?, ?, ?, ?, ?)
        """,
        (route_id, slug, name, position, _ft(feet)),
    )
    database.commit()


def _everest() -> tuple[int, Decimal]:
    row = get_db().execute(
        "SELECT id, target_elevation_m FROM routes WHERE slug = 'everest'"
    ).fetchone()
    return row["id"], Decimal(row["target_elevation_m"])


def _private_expedition(slug: str, route_id: int, owner_id: int, status: str = "active") -> int:
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


def _completed_expedition(slug: str, route_id: int) -> int:
    database = get_db()
    organization = database.execute(
        """
        INSERT INTO organizations (slug, name)
        VALUES (?, ?)
        RETURNING id
        """,
        (slug, slug),
    ).fetchone()
    row = database.execute(
        """
        INSERT INTO expeditions (
            slug, name, route_id, visibility, status, kind, organization_id, enrollment
        )
        VALUES (?, ?, ?, 'public', 'completed', 'organization', ?, 'open')
        RETURNING id
        """,
        (slug, slug, route_id, organization["id"]),
    ).fetchone()
    database.commit()
    return row["id"]


def _participation(
    user_id: int,
    expedition_id: int,
    route_id: int,
    journey_id: int,
    status: str = "active",
) -> int:
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


def _seed_credit(user_id: int, journey_id: int, meters: Decimal) -> int:
    database = get_db()
    activity_id = database.execute(
        """
        INSERT INTO activities (user_id, activity_date, elevation_gain_m, note)
        VALUES (?, DATE '2020-06-01', ?, 'seed')
        RETURNING id
        """,
        (user_id, meters),
    ).fetchone()["id"]
    database.execute(
        """
        INSERT INTO activity_verification_events (
            activity_id, from_status, to_status, reason, actor_type
        )
        VALUES (?, NULL, 'not_required', ?, 'system')
        """,
        (activity_id, MANUAL_EVENT_REASON),
    )
    database.execute(
        """
        INSERT INTO journey_credits (
            activity_id, journey_id, user_id, credited_elevation_m
        )
        VALUES (?, ?, ?, ?)
        """,
        (activity_id, journey_id, user_id, meters),
    )
    database.commit()
    return activity_id


def _journey(journey_id: int) -> dict:
    return dict(
        get_db().execute(
            """
            SELECT id, status, progress_version, completed_at
            FROM route_journeys
            WHERE id = ?
            """,
            (journey_id,),
        ).fetchone()
    )


def _total(journey_id: int) -> Decimal:
    row = get_db().execute(
        """
        SELECT COALESCE(SUM(credited_elevation_m), 0) AS total_m
        FROM journey_credits
        WHERE journey_id = ?
        """,
        (journey_id,),
    ).fetchone()
    return Decimal(row["total_m"])


def _credits(activity_id: int) -> list[dict]:
    return [
        dict(row)
        for row in get_db().execute(
            """
            SELECT journey_id, user_id, credited_elevation_m
            FROM journey_credits
            WHERE activity_id = ?
            """,
            (activity_id,),
        ).fetchall()
    ]


def _updates(user_id: int) -> list[dict]:
    return [
        dict(row)
        for row in get_db().execute(
            """
            SELECT expedition_id, participation_id, kind, activity_id,
                   checkpoint_name, elevation_m
            FROM expedition_updates
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()
    ]


def _count(table: str) -> int:
    if table not in {"activities", "journey_credits", "activity_verification_events", "expedition_updates"}:
        raise AssertionError(table)
    return get_db().execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def test_active_journey_stores_full_elevation_and_one_credit(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        started = datetime(2024, 6, 1, 15, 0, tzinfo=timezone.utc)
        ended = datetime(2024, 6, 1, 16, 30, tzinfo=timezone.utc)
        saved = create_activity(
            user_id,
            _fields(
                "400",
                distance_km="10",
                note="  ridge   day ",
                activity_type="hike",
                duration_s=5400,
                started_at=started,
                ended_at=ended,
            ),
        )
        activity = saved["activity"]
        assert activity["elevation_gain_m"] == _ft("400")
        assert activity["distance_m"] == kilometers_to_meters(Decimal("10"))
        assert activity["note"] == "ridge day"
        assert activity["activity_type"] == "hike"
        assert activity["activity_date"] == DAY
        assert activity["duration_s"] == 5400
        assert activity["started_at"] == started
        assert activity["ended_at"] == ended
        assert activity["source"] == "manual"
        assert activity["verification_status"] == "not_required"
        assert saved["credited_elevation_m"] == _ft("400")
        assert saved["journey"]["id"] == journey["id"]
        assert saved["journey"]["status"] == "active"
        assert saved["journey"]["progress_version"] == 2
        assert saved["journey"]["total_m"] == _ft("400")
        assert _credits(activity["id"]) == [
            {
                "journey_id": journey["id"],
                "user_id": user_id,
                "credited_elevation_m": _ft("400"),
            }
        ]
        event = get_db().execute(
            """
            SELECT from_status, to_status, reason, actor_type, actor_user_id
            FROM activity_verification_events
            WHERE activity_id = ?
            """,
            (activity["id"],),
        ).fetchone()
        assert event["from_status"] is None
        assert event["to_status"] == "not_required"
        assert event["reason"] == MANUAL_EVENT_REASON
        assert event["actor_type"] == "system"
        assert event["actor_user_id"] is None


def test_remainder_is_credited_and_the_activity_keeps_its_full_elevation(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        first = create_activity(user_id, _fields("800"))
        assert first["credited_elevation_m"] == _ft("800")
        assert first["journey"]["status"] == "active"
        preview = preview_activity(user_id, _fields("900"))
        assert preview["will_complete"] is True
        assert preview["credited_elevation_m"] == _ft("200")
        assert preview["activity_elevation_m"] == _ft("900")
        assert preview["resulting_total_m"] == _ft("1000")
        assert preview["journey"]["total_m"] == _ft("800")
        assert preview["journey"]["remaining_m"] == _ft("200")
        assert preview["confirmation_token"]
        assert _count("activities") == 1
        saved = create_activity(
            user_id,
            _fields("900"),
            confirmation_token=preview["confirmation_token"],
        )
        assert saved["activity"]["elevation_gain_m"] == _ft("900")
        assert saved["credited_elevation_m"] == _ft("200")
        assert saved["journey"]["status"] == "completed"
        assert saved["journey"]["total_m"] == _ft("1000")
        assert saved["journey"]["progress_version"] == 3
        assert _journey(journey["id"])["completed_at"] is not None
        history = get_activity(user_id, saved["activity"]["id"])
        assert history["elevation_gain_m"] == _ft("900")
        assert history["credit"]["credited_elevation_m"] == _ft("200")
        assert history["credit"]["journey_status"] == "completed"


def test_everest_remainder_stores_nine_hundred_and_credits_five_hundred(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id, target = _everest()
        assert target == _ft("29032")
        journey = start_journey(user_id, route_id)
        seed = target - _ft("500")
        assert seed == _ft("28532")
        _seed_credit(user_id, journey["id"], seed)
        preview = preview_activity(user_id, _fields("900"))
        assert preview["saved_without_credit"] is False
        assert preview["credited_elevation_m"] == _ft("500")
        assert preview["activity_elevation_m"] == _ft("900")
        assert preview["resulting_total_m"] == target
        assert preview["will_complete"] is True
        payload, _signature = preview["confirmation_token"].rsplit("|", 1)
        version, token_user, token_journey, progress, credit, _exp = payload.split("|")
        assert version == "v1"
        assert token_user == str(user_id)
        assert token_journey == str(journey["id"])
        assert progress == "1"
        assert credit == format(_ft("500"), "f")
        assert 590 <= preview["confirmation_expires_at"] - int(time.time()) <= 610
        saved = create_activity(
            user_id,
            _fields("900"),
            confirmation_token=preview["confirmation_token"],
        )
        assert saved["activity"]["elevation_gain_m"] == _ft("900")
        assert saved["credited_elevation_m"] == _ft("500")
        assert saved["journey"]["total_m"] == target
        assert saved["journey"]["status"] == "completed"
        assert saved["journey"]["progress_version"] == 2
        assert len(_credits(saved["activity"]["id"])) == 1


def test_no_active_journey_stores_the_activity_without_a_credit(app):
    with app.app_context():
        user_id = _user("Sam")
        preview = preview_activity(user_id, _fields("250"))
        assert preview["journey"] is None
        assert preview["saved_without_credit"] is True
        assert preview["credits_journey"] is False
        assert preview["credited_elevation_m"] == Decimal("0.00000")
        assert preview["will_complete"] is False
        assert preview["confirmation_token"] is None
        saved = create_activity(user_id, _fields("250", activity_type="run"))
        assert saved["credited_elevation_m"] is None
        assert saved["journey"] is None
        assert saved["activity"]["elevation_gain_m"] == _ft("250")
        assert saved["activity"]["activity_type"] == "run"
        assert _credits(saved["activity"]["id"]) == []
        assert _updates(user_id) == []
        assert get_activity(user_id, saved["activity"]["id"])["credit"] is None


def test_an_activity_after_completion_is_personal_history(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        expedition_id = _private_expedition("ridge-club", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey["id"])
        preview = preview_activity(user_id, _fields("1000"))
        create_activity(
            user_id,
            _fields("1000"),
            confirmation_token=preview["confirmation_token"],
        )
        version = _journey(journey["id"])["progress_version"]
        feed_count = len(_updates(user_id))
        assert feed_count > 0
        follow = create_activity(user_id, _fields("300", happened_on=date(2024, 6, 2)))
        assert follow["credited_elevation_m"] is None
        assert follow["journey"] is None
        assert follow["activity"]["elevation_gain_m"] == _ft("300")
        assert _credits(follow["activity"]["id"]) == []
        assert _journey(journey["id"])["status"] == "completed"
        assert _journey(journey["id"])["progress_version"] == version
        assert _total(journey["id"]) == _ft("1000")
        assert len(_updates(user_id)) == feed_count


def test_one_activity_cannot_receive_two_journey_credits(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        saved = create_activity(user_id, _fields("100"))
        with pytest.raises(psycopg.Error) as caught:
            get_db().execute(
                """
                INSERT INTO journey_credits (
                    activity_id, journey_id, user_id, credited_elevation_m
                )
                VALUES (?, ?, ?, ?)
                """,
                (saved["activity"]["id"], journey["id"], user_id, _ft("100")),
            )
        assert getattr(caught.value, "sqlstate", None) == "23505"
        get_db().rollback()
        assert len(_credits(saved["activity"]["id"])) == 1


def test_starting_a_journey_does_not_credit_an_older_activity(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        saved = create_activity(user_id, _fields("125"))
        journey = start_journey(user_id, route_id)
        assert _credits(saved["activity"]["id"]) == []
        assert _total(journey["id"]) == Decimal("0")
        assert _journey(journey["id"])["progress_version"] == 1
        assert get_activity(user_id, saved["activity"]["id"])["credit"] is None


def test_invalid_activity_fields_are_rejected(app):
    with app.app_context():
        user_id = _user("Sam")
        tomorrow = date.today() + timedelta(days=1)
        samples = [
            _fields("0"),
            _fields("-5"),
            _fields("10001"),
            _fields("28532"),
            {"elevation_ft": "abc", "happened_on": DAY},
            {"elevation_ft": "", "happened_on": DAY},
            {"elevation_ft": 12.5, "happened_on": DAY},
            {"happened_on": DAY},
            _fields("10", distance_km="0"),
            _fields("10", distance_km="-1"),
            _fields("10", distance_km="121"),
            _fields("10", distance_km="nope"),
            _fields("10", happened_on=tomorrow),
            _fields("10", happened_on=date(1999, 12, 31)),
            _fields("10", happened_on="nope"),
            _fields("10", note="x" * 281),
            _fields("10", activity_type="swim"),
            _fields("10", duration_s=0),
            _fields("10", duration_s=-5),
            _fields("10", duration_s="soon"),
            _fields("10", started_at=datetime(2024, 6, 1, 12, 0)),
            _fields(
                "10",
                started_at=datetime(2024, 6, 1, 13, 0),
                ended_at=datetime(2024, 6, 1, 12, 0),
            ),
            _fields("10", mystery=1),
        ]
        for fields in samples:
            before = _count("activities")
            with pytest.raises(ActivityError) as caught:
                create_activity(user_id, fields)
            assert caught.value.code == "invalid"
            assert caught.value.status == 400
            assert _count("activities") == before
        saved = create_activity(
            user_id,
            {
                "elevation_ft": "1,000",
                "happened_on": "2000-01-01",
                "distance_km": "120",
                "note": "  kept  ",
                "activity_type": "walk",
            },
        )
        assert saved["activity"]["elevation_gain_m"] == _ft("1000")
        assert saved["activity"]["distance_m"] == kilometers_to_meters(Decimal("120"))
        assert saved["activity"]["note"] == "kept"
        assert saved["activity"]["activity_date"] == date(2000, 1, 1)
        assert saved["activity"]["activity_type"] == "walk"


def test_preview_is_read_only_and_a_short_activity_needs_no_token(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        before = (_count("activities"), _count("journey_credits"), _journey(journey["id"])["progress_version"])
        preview = preview_activity(user_id, _fields("100"))
        assert preview["will_complete"] is False
        assert preview["confirmation_token"] is None
        assert preview["credited_elevation_m"] == _ft("100")
        assert preview["saved_without_credit"] is False
        assert (_count("activities"), _count("journey_credits"), _journey(journey["id"])["progress_version"]) == before
        saved = create_activity(user_id, _fields("100"))
        assert saved["credited_elevation_m"] == _ft("100")
        assert saved["journey"]["status"] == "active"
        assert saved["journey"]["progress_version"] == 2


def test_completing_without_a_token_writes_nothing(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        with pytest.raises(ActivityError) as caught:
            create_activity(user_id, _fields("1000"))
        assert caught.value.code == "confirmation_required"
        assert caught.value.status == 409
        preview = caught.value.details["preview"]
        assert preview["will_complete"] is True
        assert preview["credited_elevation_m"] == _ft("1000")
        assert _count("activities") == 0
        assert _journey(journey["id"])["status"] == "active"
        assert _journey(journey["id"])["progress_version"] == 1
        saved = create_activity(
            user_id,
            _fields("1000"),
            confirmation_token=preview["confirmation_token"],
        )
        assert saved["journey"]["status"] == "completed"
        assert saved["journey"]["progress_version"] == 2
        assert saved["activity"]["elevation_gain_m"] == _ft("1000")


def test_bad_summit_tokens_write_nothing(app):
    with app.app_context():
        user_id = _user("Sam")
        other_id = _user("Alex")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        preview = preview_activity(user_id, _fields("1000"))
        token = preview["confirmation_token"]
        payload, signature = token.rsplit("|", 1)
        flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")
        samples = [
            None,
            "not-a-token",
            payload,
            f"{payload}|{flipped}",
            _issue_confirmation_token(
                user_id, journey["id"], 1, _ft("1000"), int(time.time()) - 30
            ),
            _issue_confirmation_token(
                other_id, journey["id"], 1, _ft("1000"), int(time.time()) + 600
            ),
            _issue_confirmation_token(
                user_id, journey["id"] + 100000, 1, _ft("1000"), int(time.time()) + 600
            ),
            _issue_confirmation_token(
                user_id, journey["id"], 99, _ft("1000"), int(time.time()) + 600
            ),
            _issue_confirmation_token(
                user_id, journey["id"], 1, _ft("999"), int(time.time()) + 600
            ),
        ]
        for sample in samples:
            with pytest.raises(ActivityError) as caught:
                create_activity(user_id, _fields("1000"), confirmation_token=sample)
            assert caught.value.code == "confirmation_required"
            assert caught.value.status == 409
            assert "preview" in caught.value.details
            assert _count("activities") == 0
            assert _journey(journey["id"])["progress_version"] == 1
        saved = create_activity(user_id, _fields("1000"), confirmation_token=token)
        assert saved["journey"]["status"] == "completed"
        with pytest.raises(ActivityError) as caught:
            create_activity(user_id, _fields("100"), confirmation_token=token)
        assert caught.value.code == "confirmation_required"
        assert caught.value.details["preview"]["journey"] is None
        assert caught.value.details["preview"]["saved_without_credit"] is True
        assert _count("activities") == 1


def test_a_stale_token_returns_a_fresh_preview(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        preview = preview_activity(user_id, _fields("1000"))
        get_db().execute(
            "UPDATE route_journeys SET progress_version = 5 WHERE id = ?",
            (journey["id"],),
        )
        get_db().commit()
        with pytest.raises(ActivityError) as caught:
            create_activity(
                user_id,
                _fields("1000"),
                confirmation_token=preview["confirmation_token"],
            )
        fresh = caught.value.details["preview"]
        assert fresh["journey"]["progress_version"] == 5
        assert fresh["confirmation_token"] != preview["confirmation_token"]
        assert fresh["credited_elevation_m"] == _ft("1000")
        assert _count("activities") == 0
        saved = create_activity(
            user_id,
            _fields("1000"),
            confirmation_token=fresh["confirmation_token"],
        )
        assert saved["journey"]["status"] == "completed"
        assert saved["journey"]["progress_version"] == 6
        with pytest.raises(ActivityError) as caught:
            create_activity(
                user_id,
                _fields("1000"),
                confirmation_token=fresh["confirmation_token"],
            )
        assert caught.value.code == "confirmation_required"
        assert _count("activities") == 1


def test_overlapping_confirmations_complete_the_journey_once(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        token = preview_activity(user_id, _fields("1000"))["confirmation_token"]
        get_db().rollback()
    outcome = {"ok": 0, "codes": [], "errors": []}
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def run() -> None:
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                create_activity(user_id, _fields("1000"), confirmation_token=token)
                with lock:
                    outcome["ok"] += 1
            except ActivityError as exc:
                with lock:
                    outcome["codes"].append(exc.code)
            except Exception as exc:
                with lock:
                    outcome["errors"].append(repr(exc))
            finally:
                get_db().close()

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    with app.app_context():
        assert outcome["errors"] == []
        assert outcome["ok"] == 1
        assert outcome["codes"] == ["confirmation_required"]
        assert _count("activities") == 1
        assert _count("journey_credits") == 1
        assert _total(journey["id"]) == _ft("1000")
        assert _journey(journey["id"])["status"] == "completed"
        assert _journey(journey["id"])["progress_version"] == 2


def test_metadata_edits_do_not_change_journey_progress(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        saved = create_activity(user_id, _fields("200", note="first", activity_type="hike"))
        updated = update_activity(
            user_id,
            saved["activity"]["id"],
            {
                "note": "  second   note ",
                "distance_km": "4",
                "happened_on": date(2024, 5, 2),
                "activity_type": "walk",
                "duration_s": 90,
            },
        )
        assert updated["activity"]["note"] == "second note"
        assert updated["activity"]["distance_m"] == kilometers_to_meters(Decimal("4"))
        assert updated["activity"]["activity_date"] == date(2024, 5, 2)
        assert updated["activity"]["activity_type"] == "walk"
        assert updated["activity"]["duration_s"] == 90
        assert updated["activity"]["elevation_gain_m"] == _ft("200")
        assert updated["credited_elevation_m"] == _ft("200")
        assert updated["journey"]["progress_version"] == 2
        assert updated["journey"]["total_m"] == _ft("200")
        assert _journey(journey["id"])["status"] == "active"
        cleared = update_activity(
            user_id,
            saved["activity"]["id"],
            {"note": "", "distance_km": None},
        )
        assert cleared["activity"]["note"] == ""
        assert cleared["activity"]["distance_m"] is None
        assert _journey(journey["id"])["progress_version"] == 2


def test_elevation_edits_recalculate_the_existing_credit(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        saved = create_activity(user_id, _fields("400"))
        lowered = update_activity(
            user_id,
            saved["activity"]["id"],
            {"elevation_ft": "150"},
        )
        assert lowered["activity"]["elevation_gain_m"] == _ft("150")
        assert lowered["credited_elevation_m"] == _ft("150")
        assert lowered["journey"]["total_m"] == _ft("150")
        assert lowered["journey"]["progress_version"] == 3
        assert lowered["journey"]["status"] == "active"
        raised = update_activity(
            user_id,
            saved["activity"]["id"],
            {"elevation_ft": "350"},
        )
        assert raised["activity"]["elevation_gain_m"] == _ft("350")
        assert raised["credited_elevation_m"] == _ft("350")
        assert raised["journey"]["progress_version"] == 4
        assert _journey(journey["id"])["status"] == "active"


def test_raising_elevation_stops_at_the_remaining_journey_capacity(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        expedition_id = _private_expedition("ridge-club", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey["id"])
        create_activity(user_id, _fields("700"))
        second = create_activity(user_id, _fields("100"))
        feed_count = len(_updates(user_id))
        preview = preview_activity_update(
            user_id,
            second["activity"]["id"],
            {"elevation_ft": "500", "note": "should wait"},
        )
        assert preview["will_complete"] is True
        assert preview["activity_elevation_m"] == _ft("500")
        assert preview["credited_elevation_m"] == _ft("300")
        assert preview["resulting_total_m"] == _ft("1000")
        assert preview["confirmation_token"]
        with pytest.raises(ActivityError) as caught:
            update_activity(
                user_id,
                second["activity"]["id"],
                {"elevation_ft": "500", "note": "should wait"},
            )
        assert caught.value.code == "confirmation_required"
        assert get_activity(user_id, second["activity"]["id"])["elevation_gain_m"] == _ft("100")
        assert get_activity(user_id, second["activity"]["id"])["note"] == ""
        assert len(_updates(user_id)) == feed_count
        saved = update_activity(
            user_id,
            second["activity"]["id"],
            {"elevation_ft": "500", "note": "summit"},
            confirmation_token=preview["confirmation_token"],
        )
        assert saved["activity"]["elevation_gain_m"] == _ft("500")
        assert saved["activity"]["note"] == "summit"
        assert saved["credited_elevation_m"] == _ft("300")
        assert saved["journey"]["status"] == "completed"
        assert saved["journey"]["total_m"] == _ft("1000")
        assert len(_updates(user_id)) == feed_count


def test_editing_an_uncredited_activity_does_not_create_a_credit(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        saved = create_activity(user_id, _fields("80"))
        journey = start_journey(user_id, route_id)
        updated = update_activity(
            user_id,
            saved["activity"]["id"],
            {"elevation_ft": "90"},
        )
        assert updated["activity"]["elevation_gain_m"] == _ft("90")
        assert updated["credited_elevation_m"] is None
        assert updated["journey"] is None
        assert _credits(saved["activity"]["id"]) == []
        assert _total(journey["id"]) == Decimal("0")
        assert _journey(journey["id"])["progress_version"] == 1


def test_paused_and_abandoned_edits_cannot_complete_the_journey(app):
    with app.app_context():
        user_id = _user("Sam")
        ridge = _route("ridge", "1000")
        kili = _route("kili", "2000")
        journey = start_journey(user_id, ridge)
        saved = create_activity(user_id, _fields("400", note="kept"))
        switch_journey(user_id, kili)
        preview = preview_activity_update(
            user_id,
            saved["activity"]["id"],
            {"elevation_ft": "1000"},
        )
        assert preview["resume_required"] is True
        assert preview["will_complete"] is False
        assert preview["confirmation_token"] is None
        with pytest.raises(ActivityError) as caught:
            update_activity(
                user_id,
                saved["activity"]["id"],
                {"elevation_ft": "1000", "note": "changed"},
            )
        assert caught.value.code == "resume_required"
        assert caught.value.status == 409
        assert get_activity(user_id, saved["activity"]["id"])["elevation_gain_m"] == _ft("400")
        assert get_activity(user_id, saved["activity"]["id"])["note"] == "kept"
        assert _journey(journey["id"])["status"] == "paused"
        assert _total(journey["id"]) == _ft("400")
        lowered = update_activity(
            user_id,
            saved["activity"]["id"],
            {"elevation_ft": "250"},
        )
        assert lowered["credited_elevation_m"] == _ft("250")
        assert lowered["journey"]["status"] == "paused"
        assert _journey(journey["id"])["progress_version"] == 3
        switch_journey(user_id, ridge)
        abandon_journey(user_id, ridge)
        with pytest.raises(ActivityError) as caught:
            update_activity(user_id, saved["activity"]["id"], {"elevation_ft": "1000"})
        assert caught.value.code == "resume_required"
        assert _journey(journey["id"])["status"] == "abandoned"
        assert get_activity(user_id, saved["activity"]["id"])["elevation_gain_m"] == _ft("250")
        allowed = update_activity(
            user_id,
            saved["activity"]["id"],
            {"elevation_ft": "260"},
        )
        assert allowed["credited_elevation_m"] == _ft("260")
        assert allowed["journey"]["status"] == "abandoned"


def test_completed_journey_elevation_is_fixed_and_metadata_remains_editable(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        token = preview_activity(user_id, _fields("1000"))["confirmation_token"]
        saved = create_activity(user_id, _fields("1000", note="summit"), confirmation_token=token)
        with pytest.raises(ActivityError) as caught:
            preview_activity_update(
                user_id,
                saved["activity"]["id"],
                {"elevation_ft": "900"},
            )
        assert caught.value.code == "invalid_state"
        assert caught.value.status == 409
        with pytest.raises(ActivityError) as caught:
            update_activity(user_id, saved["activity"]["id"], {"elevation_ft": "900"})
        assert caught.value.code == "invalid_state"
        assert get_activity(user_id, saved["activity"]["id"])["elevation_gain_m"] == _ft("1000")
        assert _total(journey["id"]) == _ft("1000")
        updated = update_activity(
            user_id,
            saved["activity"]["id"],
            {"note": "still the summit", "activity_type": "hike", "distance_km": "3"},
        )
        assert updated["activity"]["note"] == "still the summit"
        assert updated["activity"]["activity_type"] == "hike"
        assert updated["activity"]["distance_m"] == kilometers_to_meters(Decimal("3"))
        assert updated["activity"]["elevation_gain_m"] == _ft("1000")
        assert updated["credited_elevation_m"] == _ft("1000")
        assert updated["journey"]["status"] == "completed"
        assert updated["journey"]["progress_version"] == saved["journey"]["progress_version"]


def test_deleting_an_activity_removes_its_credit(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        first = create_activity(user_id, _fields("300", happened_on=date(2024, 6, 1)))
        second = create_activity(user_id, _fields("200", happened_on=date(2024, 6, 2)))
        deleted = delete_activity(user_id, first["activity"]["id"])
        assert deleted["activity_id"] == first["activity"]["id"]
        assert deleted["journey"]["total_m"] == _ft("200")
        assert deleted["journey"]["status"] == "active"
        assert deleted["journey"]["progress_version"] == 4
        assert _credits(first["activity"]["id"]) == []
        assert get_db().execute(
            "SELECT COUNT(*) AS n FROM activities WHERE id = ?",
            (first["activity"]["id"],),
        ).fetchone()["n"] == 0
        assert get_db().execute(
            "SELECT COUNT(*) AS n FROM activity_verification_events WHERE activity_id = ?",
            (first["activity"]["id"],),
        ).fetchone()["n"] == 0
        assert _total(journey["id"]) == _ft("200")
        assert len(_credits(second["activity"]["id"])) == 1


def test_deleting_paused_and_abandoned_credits_does_not_change_status(app):
    with app.app_context():
        user_id = _user("Sam")
        ridge = _route("ridge", "1000")
        kili = _route("kili", "1000")
        paused = start_journey(user_id, ridge)
        paused_activity = create_activity(user_id, _fields("300"))
        switch_journey(user_id, kili)
        result = delete_activity(user_id, paused_activity["activity"]["id"])
        assert result["journey"]["status"] == "paused"
        assert result["journey"]["total_m"] == Decimal("0")
        assert _journey(paused["id"])["progress_version"] == 3
        abandoned = switch_journey(user_id, ridge)
        activity = create_activity(user_id, _fields("120"))
        abandon_journey(user_id, ridge)
        result = delete_activity(user_id, activity["activity"]["id"])
        assert result["journey"]["id"] == abandoned["id"]
        assert result["journey"]["status"] == "abandoned"
        assert result["journey"]["total_m"] == Decimal("0")


def test_deleting_an_uncredited_activity_removes_its_history(app):
    with app.app_context():
        user_id = _user("Sam")
        saved = create_activity(user_id, _fields("40"))
        deleted = delete_activity(user_id, saved["activity"]["id"])
        assert deleted["journey"] is None
        assert _count("activities") == 0
        assert _count("activity_verification_events") == 0
        assert _count("journey_credits") == 0


def test_a_completed_journey_rejects_activity_deletion(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        token = preview_activity(user_id, _fields("1000"))["confirmation_token"]
        saved = create_activity(user_id, _fields("1000"), confirmation_token=token)
        with pytest.raises(ActivityError) as caught:
            delete_activity(user_id, saved["activity"]["id"])
        assert caught.value.code == "invalid_state"
        assert caught.value.status == 409
        assert _count("activities") == 1
        assert _total(journey["id"]) == _ft("1000")
        setting = get_db().execute(
            "SELECT current_setting('adventurer.account_deletion', true) AS value"
        ).fetchone()["value"]
        assert setting != "on"


def test_the_database_trigger_blocks_deleting_a_completed_credit(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        token = preview_activity(user_id, _fields("1000"))["confirmation_token"]
        saved = create_activity(user_id, _fields("1000"), confirmation_token=token)
        with pytest.raises(psycopg.Error) as caught:
            get_db().execute(
                "DELETE FROM activities WHERE id = ?",
                (saved["activity"]["id"],),
            )
        assert "cannot be deleted" in str(caught.value)
        get_db().rollback()
        assert _count("activities") == 1
        assert _journey(journey["id"])["status"] == "completed"
        follow = create_activity(user_id, _fields("15"))
        assert follow["credited_elevation_m"] is None


def test_feed_rows_follow_the_credited_journey_and_its_checkpoints(app):
    with app.app_context():
        user_id = _user("Sam")
        ridge = _route("ridge", "1000")
        kili = _route("kili", "2000")
        _checkpoint(ridge, "camp", "Camp", 1, "900")
        _checkpoint(ridge, "mirage", "Mirage", 2, "1200")
        _checkpoint(kili, "gate", "Gate", 1, "100")
        journey = start_journey(user_id, ridge)
        create_activity(user_id, _fields("800"))
        assert _updates(user_id) == []
        live = _private_expedition("live", ridge, user_id)
        left = _private_expedition("left", ridge, user_id)
        closed = _private_expedition("closed", ridge, user_id, status="archived")
        finished = _completed_expedition("finished", ridge)
        also_live = _private_expedition("also-live", ridge, user_id)
        _participation(user_id, live, ridge, journey["id"])
        _participation(user_id, left, ridge, journey["id"], status="withdrawn")
        _participation(user_id, closed, ridge, journey["id"])
        _participation(user_id, finished, ridge, journey["id"])
        _participation(user_id, also_live, ridge, journey["id"])
        token = preview_activity(user_id, _fields("900"))["confirmation_token"]
        saved = create_activity(user_id, _fields("900"), confirmation_token=token)
        rows = _updates(user_id)
        assert {row["expedition_id"] for row in rows} == {live, also_live}
        for expedition_id in (live, also_live):
            kinds = [
                (row["kind"], row["checkpoint_name"], Decimal(row["elevation_m"]))
                for row in rows
                if row["expedition_id"] == expedition_id
            ]
            assert kinds == [
                ("activity_logged", None, _ft("200")),
                ("checkpoint_reached", "Camp", _ft("1000")),
                ("summit_reached", None, _ft("1000")),
            ]
            assert all(row["activity_id"] == saved["activity"]["id"] for row in rows if row["expedition_id"] == expedition_id)
        assert saved["activity"]["elevation_gain_m"] == _ft("900")
        assert saved["journey"]["status"] == "completed"

        switch_journey(user_id, kili)
        active = get_db().execute(
            """
            SELECT id FROM route_journeys
            WHERE user_id = ? AND route_id = ? AND status = 'active'
            """,
            (user_id, kili),
        ).fetchone()["id"]
        kili_expedition = _private_expedition("kili-club", kili, user_id)
        _participation(user_id, kili_expedition, kili, active)
        create_activity(user_id, _fields("150"))
        fresh = [row for row in _updates(user_id) if row["activity_id"] != saved["activity"]["id"]]
        assert [row["expedition_id"] for row in fresh] == [kili_expedition, kili_expedition]
        assert fresh[0]["kind"] == "activity_logged"
        assert Decimal(fresh[0]["elevation_m"]) == _ft("150")
        assert fresh[1]["kind"] == "checkpoint_reached"
        assert fresh[1]["checkpoint_name"] == "Gate"
        assert Decimal(fresh[1]["elevation_m"]) == _ft("150")


def test_a_paused_journey_participation_receives_no_update(app):
    with app.app_context():
        user_id = _user("Sam")
        ridge = _route("ridge", "2000")
        kili = _route("kili", "2000")
        _checkpoint(kili, "gate", "Gate", 1, "100")
        journey = start_journey(user_id, ridge)
        paused_expedition = _private_expedition("paused-club", ridge, user_id)
        _participation(user_id, paused_expedition, ridge, journey["id"])
        switch_journey(user_id, kili)
        active = get_db().execute(
            """
            SELECT id FROM route_journeys
            WHERE user_id = ? AND status = 'active'
            """,
            (user_id,),
        ).fetchone()["id"]
        active_expedition = _private_expedition("active-club", kili, user_id)
        _participation(user_id, active_expedition, kili, active)
        saved = create_activity(user_id, _fields("150"))
        rows = _updates(user_id)
        assert [row["expedition_id"] for row in rows] == [active_expedition, active_expedition]
        assert rows[0]["kind"] == "activity_logged"
        assert Decimal(rows[0]["elevation_m"]) == _ft("150")
        assert rows[0]["activity_id"] == saved["activity"]["id"]
        assert rows[1]["checkpoint_name"] == "Gate"
        assert _journey(journey["id"])["status"] == "paused"
        assert _total(journey["id"]) == Decimal("0")


def test_a_feed_insert_failure_rolls_back_the_activity_and_credit(app, monkeypatch):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        expedition_id = _private_expedition("ridge-club", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey["id"])
        database = get_db()
        original = database.execute

        def fail_feed(sql, params=()):
            if "INSERT INTO expedition_updates" in sql:
                raise RuntimeError("feed failed")
            return original(sql, params)

        monkeypatch.setattr(database, "execute", fail_feed)
        with pytest.raises(RuntimeError, match="feed failed"):
            create_activity(user_id, _fields("100"))
        monkeypatch.undo()
        assert _count("activities") == 0
        assert _count("journey_credits") == 0
        assert _count("expedition_updates") == 0
        assert _journey(journey["id"])["progress_version"] == 1
        saved = create_activity(user_id, _fields("100"))
        assert saved["credited_elevation_m"] == _ft("100")
        assert len(_updates(user_id)) == 1


def test_personal_history_is_owner_scoped(app):
    with app.app_context():
        owner = _user("Sam")
        other = _user("Alex")
        route_id = _route("ridge", "1000")
        journey = start_journey(owner, route_id)
        older = create_activity(owner, _fields("100", happened_on=date(2024, 6, 1), note="older"))
        newer = create_activity(owner, _fields("50", happened_on=date(2024, 6, 3), note="newer"))
        same_day = create_activity(owner, _fields("25", happened_on=date(2024, 6, 3), note="later"))
        create_activity(other, _fields("70", note="private"))
        history = list_activities(owner)
        assert [row["note"] for row in history] == ["later", "newer", "older"]
        assert history[0]["elevation_gain_m"] == _ft("25")
        assert history[2]["credit"]["credited_elevation_m"] == _ft("100")
        assert history[2]["credit"]["journey_id"] == journey["id"]
        assert history[2]["credit"]["journey_status"] == "active"
        assert list_activities(other)[0]["note"] == "private"
        assert all(row["id"] != older["activity"]["id"] for row in list_activities(other))
        with pytest.raises(ActivityError) as caught:
            get_activity(other, newer["activity"]["id"])
        assert caught.value.code == "forbidden"
        assert caught.value.status == 403
        with pytest.raises(ActivityError) as caught:
            preview_activity_update(other, newer["activity"]["id"], {"note": "no"})
        assert caught.value.code == "forbidden"
        with pytest.raises(ActivityError) as caught:
            update_activity(other, newer["activity"]["id"], {"note": "no"})
        assert caught.value.code == "forbidden"
        with pytest.raises(ActivityError) as caught:
            delete_activity(other, newer["activity"]["id"])
        assert caught.value.code == "forbidden"
        assert get_activity(owner, newer["activity"]["id"])["note"] == "newer"
        other_preview = preview_activity(other, _fields("10"))
        assert other_preview["journey"] is None
        assert other_preview["saved_without_credit"] is True


def test_a_deleting_account_cannot_write_activities(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        saved = create_activity(user_id, _fields("100"))
        get_db().execute(
            "UPDATE users SET deletion_state = 'deleting' WHERE id = ?",
            (user_id,),
        )
        get_db().commit()
        for call in (
            lambda: preview_activity(user_id, _fields("10")),
            lambda: create_activity(user_id, _fields("10")),
            lambda: update_activity(user_id, saved["activity"]["id"], {"note": "no"}),
            lambda: delete_activity(user_id, saved["activity"]["id"]),
        ):
            with pytest.raises(ActivityError) as caught:
                call()
            assert caught.value.code == "forbidden"
            assert caught.value.status == 403
        assert _count("activities") == 1
        assert _journey(journey["id"])["progress_version"] == 2
        with pytest.raises(ActivityError) as caught:
            create_activity(999999, _fields("10"))
        assert caught.value.code == "not_found"


def test_concurrent_writes_do_not_overshoot_the_journey(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
        get_db().rollback()
    outcome = {"ok": 0, "codes": [], "errors": [], "preview": None}
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def run() -> None:
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                create_activity(user_id, _fields("600"))
                with lock:
                    outcome["ok"] += 1
            except ActivityError as exc:
                with lock:
                    outcome["codes"].append(exc.code)
                    outcome["preview"] = exc.details.get("preview")
            except Exception as exc:
                with lock:
                    outcome["errors"].append(repr(exc))
            finally:
                get_db().close()

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    with app.app_context():
        assert outcome["errors"] == []
        assert outcome["ok"] == 1
        assert outcome["codes"] == ["confirmation_required"]
        assert _count("activities") == 1
        assert _total(journey["id"]) == _ft("600")
        assert _journey(journey["id"])["status"] == "active"
        preview = outcome["preview"]
        assert preview["will_complete"] is True
        assert preview["credited_elevation_m"] == _ft("400")
        assert preview["activity_elevation_m"] == _ft("600")
        saved = create_activity(
            user_id,
            _fields("600"),
            confirmation_token=preview["confirmation_token"],
        )
        assert saved["activity"]["elevation_gain_m"] == _ft("600")
        assert saved["credited_elevation_m"] == _ft("400")
        assert saved["journey"]["status"] == "completed"
        assert saved["journey"]["total_m"] == _ft("1000")


def test_an_activity_racing_a_switch_credits_only_the_journey_that_wins(app, monkeypatch):
    with app.app_context():
        user_id = _user("Sam")
        everest, _target = _everest()
        kili = _route("kili", "2000")
        original = start_journey(user_id, everest)
        main = get_db()
        reached = threading.Event()
        allow = threading.Event()
        original_execute = DB.execute
        seen = {"paused": False}

        def wrapped(self, sql, params=()):
            normalized = " ".join(sql.split()).lower()
            if (
                not seen["paused"]
                and self is not main
                and "for update" in normalized
                and "from users" in normalized
            ):
                seen["paused"] = True
                reached.set()
                assert allow.wait(timeout=10)
            return original_execute(self, sql, params)

        monkeypatch.setattr(DB, "execute", wrapped)
        holder = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["result"] = create_activity(user_id, _fields("100"))
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            switched = switch_journey(user_id, kili)
        finally:
            allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        monkeypatch.undo()
        assert "error" not in holder
        credits = _credits(holder["result"]["activity"]["id"])
        assert len(credits) == 1
        assert credits[0]["journey_id"] == switched["id"]
        assert credits[0]["user_id"] == user_id
        assert _total(original["id"]) == Decimal("0")
        assert _journey(original["id"])["status"] == "paused"


def test_an_activity_racing_a_restart_credits_the_new_attempt(app, monkeypatch):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        original = start_journey(user_id, route_id)
        main = get_db()
        reached = threading.Event()
        allow = threading.Event()
        original_execute = DB.execute
        seen = {"paused": False}

        def wrapped(self, sql, params=()):
            normalized = " ".join(sql.split()).lower()
            if (
                not seen["paused"]
                and self is not main
                and "for update" in normalized
                and "from users" in normalized
            ):
                seen["paused"] = True
                reached.set()
                assert allow.wait(timeout=10)
            return original_execute(self, sql, params)

        monkeypatch.setattr(DB, "execute", wrapped)
        holder = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["result"] = create_activity(user_id, _fields("80"))
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            restarted = restart_journey(user_id, route_id)
        finally:
            allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        monkeypatch.undo()
        assert "error" not in holder
        credits = _credits(holder["result"]["activity"]["id"])
        assert len(credits) == 1
        assert credits[0]["journey_id"] == restarted["journey"]["id"]
        assert credits[0]["journey_id"] != original["id"]
        assert _total(original["id"]) == Decimal("0")
        assert _journey(original["id"])["status"] == "abandoned"


def test_a_commit_failure_rolls_back_and_leaves_the_connection_usable(app, monkeypatch):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        journey = start_journey(user_id, route_id)
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
            create_activity(user_id, _fields("100"))
        assert caught.value.__cause__ is None
        assert rollbacks
        monkeypatch.undo()
        assert _count("activities") == 0
        assert _journey(journey["id"])["progress_version"] == 1
        saved = create_activity(user_id, _fields("100"))
        assert saved["credited_elevation_m"] == _ft("100")


def test_a_rollback_failure_keeps_the_original_exception(app, monkeypatch):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("ridge", "1000")
        start_journey(user_id, route_id)
        database = get_db()

        def fail_commit():
            raise RuntimeError("commit failed")

        def fail_rollback():
            raise RuntimeError("rollback failed")

        monkeypatch.setattr(database, "commit", fail_commit)
        monkeypatch.setattr(database, "rollback", fail_rollback)
        try:
            with pytest.raises(RuntimeError, match="commit failed") as caught:
                create_activity(user_id, _fields("100"))
            assert str(caught.value.__cause__) == "rollback failed"
        finally:
            monkeypatch.undo()
            database.rollback()
