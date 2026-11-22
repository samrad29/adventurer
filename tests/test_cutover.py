"""Phase 2 behavior against the isolated Phase 1 schema."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from adventurer.challenges import get_challenge
from adventurer.db import DB, get_db
from adventurer.measures import feet_to_meters, kilometers_to_meters
from adventurer.services import (
    authenticate,
    list_logs,
    load_route,
    total_elevation,
)

from test_app import _csrf, _join, _log


def _user_id(name_key: str) -> int:
    return get_db().execute(
        "SELECT id FROM users WHERE username_key = ?",
        (name_key,),
    ).fetchone()["id"]


def test_join_creates_a_user_profile_and_everest_participation(client):
    _join(client, "Sam", "trailhead")
    with client.application.app_context():
        row = get_db().execute(
            """
            SELECT users.username, users.username_key, profiles.display_name,
                   profiles.unit_system, expedition.slug, participation.status
            FROM users
            JOIN user_profiles AS profiles ON profiles.user_id = users.id
            JOIN expedition_participations AS participation
                ON participation.user_id = users.id
            JOIN expeditions AS expedition
                ON expedition.id = participation.expedition_id
            WHERE users.username_key = 'sam'
            """
        ).fetchone()
        assert authenticate("Sam", "trailhead")["name"] == "Sam"
        assert authenticate("Sam", "wrong-pass") is None
    assert row["username"] == "Sam"
    assert row["username_key"] == "sam"
    assert row["display_name"] == "Sam"
    assert row["unit_system"] == "imperial"
    assert row["slug"] == "everest"
    assert row["status"] == "active"


def test_manual_activity_stores_meters_and_a_full_credit(client):
    _join(client, "Sam")
    token = _csrf(client, "/everest/log")
    response = client.post(
        "/everest/log",
        data={
            "csrf": token,
            "elevation_ft": "1951.5",
            "distance_km": "5.5",
            "happened_on": "2026-11-01",
            "note": "ridge",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    expected_m = feet_to_meters(Decimal("1951.5"))
    with client.application.app_context():
        activity = get_db().execute(
            """
            SELECT activity.activity_type, activity.source, activity.verification_status,
                   activity.elevation_gain_m, activity.distance_m,
                   activity.started_at, activity.ended_at, activity.duration_s,
                   activity.external_id, credit.credited_elevation_m,
                   event.from_status, event.to_status, event.actor_type
            FROM activities AS activity
            JOIN activity_credits AS credit ON credit.activity_id = activity.id
            JOIN activity_verification_events AS event
                ON event.activity_id = activity.id
            WHERE activity.user_id = ?
            """,
            (_user_id("sam"),),
        ).fetchone()
    assert activity["activity_type"] == "unspecified"
    assert activity["source"] == "manual"
    assert activity["verification_status"] == "not_required"
    assert activity["elevation_gain_m"] == expected_m
    assert activity["credited_elevation_m"] == expected_m
    assert activity["distance_m"] == kilometers_to_meters(Decimal("5.5"))
    assert activity["started_at"] is None
    assert activity["ended_at"] is None
    assert activity["duration_s"] is None
    assert activity["external_id"] is None
    assert activity["from_status"] is None
    assert activity["to_status"] == "not_required"
    assert activity["actor_type"] == "user"


def test_activity_and_credit_inserts_roll_back_together(client, monkeypatch):
    _join(client, "Sam")
    original = DB.execute

    def execute(self, sql, params=()):
        if "INSERT INTO activity_credits" in sql:
            raise RuntimeError("credit failed")
        return original(self, sql, params)

    monkeypatch.setattr(DB, "execute", execute)
    with pytest.raises(RuntimeError, match="credit failed"):
        _log(client, 100, note="should vanish")
    with client.application.app_context():
        counts = get_db().execute(
            """
            SELECT
                (SELECT COUNT(*) FROM activities) AS activities,
                (SELECT COUNT(*) FROM activity_credits) AS credits,
                (SELECT COUNT(*) FROM activity_verification_events) AS events
            """
        ).fetchone()
    assert counts["activities"] == 0
    assert counts["credits"] == 0
    assert counts["events"] == 0


def test_progress_sums_credited_elevation(client):
    _join(client)
    _log(client, 100)
    _log(client, 250)
    with client.application.app_context():
        assert total_elevation(_user_id("sam"), "everest") == 350
        stored = get_db().execute(
            "SELECT COALESCE(SUM(credited_elevation_m), 0) AS total_m FROM activity_credits"
        ).fetchone()["total_m"]
    assert stored == feet_to_meters(Decimal("100")) + feet_to_meters(Decimal("250"))


def test_leaderboard_ranks_participants_and_skips_unenrolled_users(app):
    sam = app.test_client()
    alex = app.test_client()
    _join(sam, "Sam")
    _join(alex, "Alex")
    _log(sam, 100)
    _log(alex, 250)
    with app.app_context():
        get_db().execute(
            """
            INSERT INTO users (username, username_key, password_hash)
            VALUES ('Rae', 'rae', 'not-a-login')
            """
        )
        get_db().execute(
            """
            INSERT INTO user_profiles (user_id, display_name)
            SELECT id, 'Rae' FROM users WHERE username_key = 'rae'
            """
        )
        get_db().commit()
    text = sam.get("/everest/board").get_data(as_text=True)
    names = __import__("re").findall(r"<strong>([^<]+)</strong>", text)
    assert names == ["Alex", "Sam"]
    assert "Rae" not in text


def test_journal_lists_only_the_selected_expedition(client):
    _join(client, "Sam")
    _log(client, 80, note="everest day")
    with client.application.app_context():
        database = get_db()
        user_id = _user_id("sam")
        activity_id = database.execute(
            "SELECT id FROM activities WHERE user_id = ?",
            (user_id,),
        ).fetchone()["id"]
        database.execute(
            """
            INSERT INTO expeditions (slug, name, target_elevation_m)
            VALUES ('annapurna', 'Annapurna', 8091)
            """
        )
        database.execute(
            """
            INSERT INTO expedition_participations (user_id, expedition_id)
            SELECT ?, id FROM expeditions WHERE slug = 'annapurna'
            """,
            (user_id,),
        )
        database.execute(
            """
            INSERT INTO activities (
                user_id, activity_date, elevation_gain_m, source,
                verification_status, note
            )
            VALUES (?, ?, ?, 'manual', 'not_required', 'other mountain')
            """,
            (user_id, date.today().isoformat(), feet_to_meters(Decimal("40"))),
        )
        other_id = database.execute(
            "SELECT id FROM activities WHERE note = 'other mountain'"
        ).fetchone()["id"]
        database.execute(
            """
            INSERT INTO activity_verification_events (
                activity_id, from_status, to_status, reason, actor_type, actor_user_id
            )
            VALUES (?, NULL, 'not_required', 'Manual entry for the other expedition.', 'user', ?)
            """,
            (other_id, user_id),
        )
        database.execute(
            """
            INSERT INTO activity_credits (
                activity_id, participation_id, user_id, credited_elevation_m
            )
            SELECT ?, participation.id, ?, ?
            FROM expedition_participations AS participation
            JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
            WHERE participation.user_id = ? AND expedition.slug = 'annapurna'
            """,
            (other_id, user_id, feet_to_meters(Decimal("40")), user_id),
        )
        database.commit()
        everest = [entry["note"] for entry in list_logs(user_id, "everest")]
        other = [entry["note"] for entry in list_logs(user_id, "annapurna")]
        assert activity_id != other_id
    page = client.get("/everest/journal").get_data(as_text=True)
    assert everest == ["everest day"]
    assert other == ["other mountain"]
    assert "other mountain" not in page
    assert "everest day" in page


def test_deleting_one_credit_keeps_the_activity_and_the_other_expedition(client):
    _join(client, "Sam")
    _log(client, 125, note="shared day")
    with client.application.app_context():
        database = get_db()
        user_id = _user_id("sam")
        activity_id = database.execute(
            "SELECT id FROM activities WHERE user_id = ?",
            (user_id,),
        ).fetchone()["id"]
        elevation = database.execute(
            "SELECT elevation_gain_m FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()["elevation_gain_m"]
        database.execute(
            """
            INSERT INTO expeditions (slug, name, target_elevation_m)
            VALUES ('annapurna', 'Annapurna', 8091)
            """
        )
        database.execute(
            """
            INSERT INTO expedition_participations (user_id, expedition_id)
            SELECT ?, id FROM expeditions WHERE slug = 'annapurna'
            """,
            (user_id,),
        )
        database.execute(
            """
            INSERT INTO activity_credits (
                activity_id, participation_id, user_id, credited_elevation_m
            )
            SELECT ?, participation.id, ?, ?
            FROM expedition_participations AS participation
            JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
            WHERE participation.user_id = ? AND expedition.slug = 'annapurna'
            """,
            (activity_id, user_id, elevation, user_id),
        )
        database.commit()

    token = _csrf(client, "/everest/journal")
    removed = client.post(
        f"/everest/logs/{activity_id}/delete",
        data={"csrf": token},
        follow_redirects=True,
    )
    assert removed.status_code == 200
    assert "shared day" not in removed.get_data(as_text=True)
    with client.application.app_context():
        database = get_db()
        user_id = _user_id("sam")
        assert database.execute("SELECT COUNT(*) AS n FROM activities").fetchone()["n"] == 1
        remaining = database.execute(
            """
            SELECT expedition.slug
            FROM activity_credits AS credit
            JOIN expedition_participations AS participation
                ON participation.id = credit.participation_id
            JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
            WHERE credit.user_id = ?
            ORDER BY expedition.slug
            """,
            (user_id,),
        ).fetchall()
        assert [row["slug"] for row in remaining] == ["annapurna"]
        assert total_elevation(user_id, "everest") == 0
        assert total_elevation(user_id, "annapurna") == 125


def test_one_user_cannot_read_or_remove_another_users_credit(app):
    sam = app.test_client()
    alex = app.test_client()
    _join(sam, "Sam")
    _log(sam, 100, note="mine")
    _join(alex, "Alex")
    with app.app_context():
        sam_id = _user_id("sam")
        alex_id = _user_id("alex")
        activity_id = get_db().execute(
            "SELECT id FROM activities WHERE user_id = ?",
            (sam_id,),
        ).fetchone()["id"]
        assert [entry["note"] for entry in list_logs(alex_id, "everest")] == []
        assert list_logs(sam_id, "everest")[0]["note"] == "mine"
    token = _csrf(alex, "/everest/journal")
    stolen = alex.post(f"/everest/logs/{activity_id}/delete", data={"csrf": token})
    assert stolen.status_code == 404
    assert "mine" in sam.get("/everest/journal").get_data(as_text=True)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS n FROM activity_credits").fetchone()["n"] == 1


def test_static_checkpoints_match_the_database(app):
    everest = get_challenge("everest")
    with app.app_context():
        rows = get_db().execute(
            """
            SELECT checkpoint.slug, checkpoint.name, checkpoint.position, checkpoint.elevation_m,
                   expedition.target_elevation_m
            FROM expedition_checkpoints AS checkpoint
            JOIN expeditions AS expedition ON expedition.id = checkpoint.expedition_id
            WHERE expedition.slug = 'everest'
            ORDER BY checkpoint.position
            """
        ).fetchall()
        route = load_route("everest")
    assert [row["slug"] for row in rows] == [stop["slug"] for stop in everest["stops"]]
    assert [stop["slug"] for stop in route["stops"]] == [stop["slug"] for stop in everest["stops"]]
    for index, (row, stop) in enumerate(zip(rows, everest["stops"])):
        assert row["position"] == index
        assert row["name"] == stop["name"]
        assert row["elevation_m"] == feet_to_meters(Decimal(str(stop["elevation_ft"])))
        assert route["stops"][index]["elevation_ft"] == stop["elevation_ft"]
    assert rows[-1]["elevation_m"] == rows[-1]["target_elevation_m"]
    assert route["summit_ft"] == everest["summit_ft"]
