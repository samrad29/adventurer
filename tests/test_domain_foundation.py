"""Database constraints for the journey domain.

Uses the isolated local database only. Never reads DATABASE_URL.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
TEST_URL = "postgresql://postgres:postgres@127.0.0.1:54329/adventurer_migration_test"


def _upgrade() -> None:
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["TEST_DATABASE_URL"] = TEST_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed\n{result.stdout}\n{result.stderr}")


@pytest.fixture()
def db():
    _upgrade()
    try:
        connection = psycopg.connect(TEST_URL, row_factory=dict_row, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.fail(f"isolated Postgres is not listening on 127.0.0.1:54329: {exc}")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _expect_error(conn, sql: str, params: tuple = ()) -> None:
    conn.execute("SAVEPOINT probe")
    try:
        conn.execute(sql, params)
    except psycopg.Error:
        conn.execute("ROLLBACK TO SAVEPOINT probe")
        return
    conn.execute("ROLLBACK TO SAVEPOINT probe")
    raise AssertionError("statement should have failed: " + sql)


def _user(conn, name: str) -> int:
    row = conn.execute(
        """
        INSERT INTO adventurer.users (username, username_key, password_hash)
        VALUES (%s, %s, 'hash')
        RETURNING id
        """,
        (name, name.casefold()),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO adventurer.user_profiles (user_id, display_name)
        VALUES (%s, %s)
        """,
        (row["id"], name),
    )
    return row["id"]


def _route(conn, slug: str) -> int:
    return conn.execute(
        """
        INSERT INTO adventurer.routes (
            slug, name, region, summary, target_elevation_m, status
        )
        VALUES (%s, %s, 'Test', 'A test route', 1000, 'published')
        RETURNING id
        """,
        (slug, slug),
    ).fetchone()["id"]


def _journey(conn, user_id: int, route_id: int, status: str, attempt: int, current: bool) -> int:
    return conn.execute(
        """
        INSERT INTO adventurer.route_journeys (
            user_id, route_id, status, attempt_number, is_current
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (user_id, route_id, status, attempt, current),
    ).fetchone()["id"]


def _activity(conn, user_id: int, elevation: str = "100") -> int:
    activity_id = conn.execute(
        """
        INSERT INTO adventurer.activities (
            user_id, activity_date, elevation_gain_m, note
        )
        VALUES (%s, DATE '2026-01-01', %s, 'note')
        RETURNING id
        """,
        (user_id, elevation),
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO adventurer.activity_verification_events (
            activity_id, from_status, to_status, reason, actor_type
        )
        VALUES (
            %s, NULL, 'not_required',
            'Manual entry. Manual entries are not independently verified.',
            'system'
        )
        """,
        (activity_id,),
    )
    return activity_id


def test_clean_slate_keeps_everest_and_drops_activity_credits(db):
    assert db.execute(
        "SELECT to_regclass('adventurer.activity_credits') AS name"
    ).fetchone()["name"] is None
    assert db.execute(
        "SELECT to_regclass('adventurer.route_journeys') AS name"
    ).fetchone()["name"] is not None
    everest = db.execute(
        """
        SELECT expeditions.slug, expeditions.name, expeditions.kind,
               expeditions.visibility, expeditions.status, expeditions.owner_user_id,
               routes.slug AS route_slug
        FROM adventurer.expeditions AS expeditions
        JOIN adventurer.routes AS routes ON routes.id = expeditions.route_id
        WHERE expeditions.slug = 'everest'
        """
    ).fetchone()
    assert everest["name"] == "Global Everest Challenge"
    assert everest["kind"] == "global"
    assert everest["visibility"] == "public"
    assert everest["status"] == "active"
    assert everest["owner_user_id"] is None
    assert everest["route_slug"] == "everest"
    assert db.execute(
        """
        SELECT relrowsecurity
        FROM pg_class
        WHERE oid = 'adventurer.route_journeys'::regclass
        """
    ).fetchone()["relrowsecurity"] is True


def test_organization_membership_rules(db):
    owner = _user(db, "Owner")
    admin = _user(db, "Admin")
    other = _user(db, "Other")
    org_id = db.execute(
        """
        INSERT INTO adventurer.organizations (slug, name, colors)
        VALUES ('club', 'Club', '{"primary": "green"}'::jsonb)
        RETURNING id
        """
    ).fetchone()["id"]
    db.execute(
        """
        INSERT INTO adventurer.organization_memberships (organization_id, user_id, role)
        VALUES (%s, %s, 'owner')
        """,
        (org_id, owner),
    )
    db.execute(
        """
        INSERT INTO adventurer.organization_memberships (organization_id, user_id, role)
        VALUES (%s, %s, 'admin')
        """,
        (org_id, admin),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.organization_memberships (organization_id, user_id, role)
        VALUES (%s, %s, 'owner')
        """,
        (org_id, other),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.organizations (slug, name, colors)
        VALUES ('bad-colors', 'Bad', '[1, 2]'::jsonb)
        """,
    )


def test_journey_uniqueness_and_status(db):
    user_id = _user(db, "Climber")
    everest = db.execute(
        "SELECT id FROM adventurer.routes WHERE slug = 'everest'"
    ).fetchone()["id"]
    other = _route(db, "kilimanjaro")
    _journey(db, user_id, everest, "active", 1, True)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_journeys (
            user_id, route_id, status, attempt_number, is_current
        )
        VALUES (%s, %s, 'active', 1, true)
        """,
        (user_id, other),
    )
    paused = _journey(db, user_id, other, "paused", 1, True)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_journeys (
            user_id, route_id, status, attempt_number, is_current
        )
        VALUES (%s, %s, 'paused', 2, false)
        """,
        (user_id, other),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_journeys (
            user_id, route_id, status, attempt_number, is_current
        )
        VALUES (%s, %s, 'abandoned', 2, true)
        """,
        (user_id, other),
    )
    db.execute(
        """
        UPDATE adventurer.route_journeys
        SET status = 'abandoned', is_current = true
        WHERE id = %s
        """,
        (paused,),
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.route_journeys
        SET status = 'active'
        WHERE id = %s
        """,
        (paused,),
    )


def test_journey_credit_matches_one_activity_and_one_user(db):
    first = _user(db, "First")
    second = _user(db, "Second")
    route_id = db.execute(
        "SELECT id FROM adventurer.routes WHERE slug = 'everest'"
    ).fetchone()["id"]
    journey_id = _journey(db, first, route_id, "active", 1, True)
    other_journey = _journey(db, second, route_id, "active", 1, True)
    activity_id = _activity(db, first, "100")
    db.execute(
        """
        INSERT INTO adventurer.journey_credits (
            activity_id, journey_id, user_id, credited_elevation_m
        )
        VALUES (%s, %s, %s, 40)
        """,
        (activity_id, journey_id, first),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_credits (
            activity_id, journey_id, user_id, credited_elevation_m
        )
        VALUES (%s, %s, %s, 10)
        """,
        (activity_id, journey_id, first),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_credits (
            activity_id, journey_id, user_id, credited_elevation_m
        )
        VALUES (%s, %s, %s, 10)
        """,
        (activity_id, journey_id, first),
    )
    other_activity = _activity(db, first, "50")
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_credits (
            activity_id, journey_id, user_id, credited_elevation_m
        )
        VALUES (%s, %s, %s, 10)
        """,
        (other_activity, other_journey, first),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_credits (
            activity_id, journey_id, user_id, credited_elevation_m
        )
        VALUES (%s, %s, %s, 80)
        """,
        (other_activity, journey_id, first),
    )


def test_participation_must_match_user_and_route(db):
    first = _user(db, "Ada")
    second = _user(db, "Bea")
    everest = db.execute(
        "SELECT id FROM adventurer.routes WHERE slug = 'everest'"
    ).fetchone()["id"]
    kili = _route(db, "kili-route")
    everest_expedition = db.execute(
        "SELECT id FROM adventurer.expeditions WHERE slug = 'everest'"
    ).fetchone()["id"]
    private_id = db.execute(
        """
        INSERT INTO adventurer.expeditions (
            slug, name, route_id, visibility, status, kind, owner_user_id
        )
        VALUES ('friends', 'Friends', %s, 'private', 'active', 'private', %s)
        RETURNING id
        """,
        (kili, first),
    ).fetchone()["id"]
    everest_journey = _journey(db, first, everest, "paused", 1, True)
    kili_journey = _journey(db, first, kili, "active", 1, True)
    second_journey = _journey(db, second, kili, "active", 1, True)
    db.execute(
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (first, private_id, kili, kili_journey),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (first, private_id, kili, everest_journey),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (first, everest_expedition, everest, kili_journey),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (first, private_id, kili, second_journey),
    )


def test_one_active_organization_expedition_per_user(db):
    user_id = _user(db, "Member")
    everest = db.execute(
        "SELECT id FROM adventurer.routes WHERE slug = 'everest'"
    ).fetchone()["id"]
    kili = _route(db, "org-kili")
    org_id = db.execute(
        """
        INSERT INTO adventurer.organizations (slug, name)
        VALUES ('runners', 'Runners')
        RETURNING id
        """
    ).fetchone()["id"]
    first_expedition = db.execute(
        """
        INSERT INTO adventurer.expeditions (
            slug, name, route_id, visibility, status, kind, organization_id, enrollment
        )
        VALUES (
            'org-everest', 'Org Everest', %s, 'public', 'active',
            'organization', %s, 'open'
        )
        RETURNING id
        """,
        (everest, org_id),
    ).fetchone()["id"]
    second_expedition = db.execute(
        """
        INSERT INTO adventurer.expeditions (
            slug, name, route_id, visibility, status, kind, organization_id, enrollment
        )
        VALUES (
            'org-kili', 'Org Kili', %s, 'public', 'active',
            'organization', %s, 'invitation'
        )
        RETURNING id
        """,
        (kili, org_id),
    ).fetchone()["id"]
    private_route = _route(db, "private-route")
    private_id = db.execute(
        """
        INSERT INTO adventurer.expeditions (
            slug, name, route_id, visibility, status, kind, owner_user_id
        )
        VALUES ('private-one', 'Private', %s, 'private', 'active', 'private', %s)
        RETURNING id
        """,
        (private_route, user_id),
    ).fetchone()["id"]
    everest_journey = _journey(db, user_id, everest, "paused", 1, True)
    kili_journey = _journey(db, user_id, kili, "active", 1, True)
    private_journey = _journey(db, user_id, private_route, "abandoned", 1, True)
    global_expedition = db.execute(
        "SELECT id FROM adventurer.expeditions WHERE slug = 'everest'"
    ).fetchone()["id"]
    db.execute(
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (user_id, first_expedition, everest, everest_journey),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (user_id, second_expedition, kili, kili_journey),
    )
    db.execute(
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id
        )
        VALUES (%s, %s, %s, %s), (%s, %s, %s, %s)
        """,
        (
            user_id, global_expedition, everest, everest_journey,
            user_id, private_id, private_route, private_journey,
        ),
    )
    db.execute(
        """
        UPDATE adventurer.expeditions
        SET status = 'completed'
        WHERE id = %s
        """,
        (first_expedition,),
    )
    db.execute(
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (user_id, second_expedition, kili, kili_journey),
    )


def test_completed_journey_credits_stay_until_account_deletion(db):
    user_id = _user(db, "Finisher")
    route_id = db.execute(
        "SELECT id FROM adventurer.routes WHERE slug = 'everest'"
    ).fetchone()["id"]
    journey_id = _journey(db, user_id, route_id, "active", 1, True)
    activity_id = _activity(db, user_id, "100")
    credit_id = db.execute(
        """
        INSERT INTO adventurer.journey_credits (
            activity_id, journey_id, user_id, credited_elevation_m
        )
        VALUES (%s, %s, %s, 100)
        RETURNING id
        """,
        (activity_id, journey_id, user_id),
    ).fetchone()["id"]
    db.execute(
        """
        UPDATE adventurer.route_journeys
        SET status = 'completed', completed_at = now()
        WHERE id = %s
        """,
        (journey_id,),
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.journey_credits
        SET credited_elevation_m = 90
        WHERE id = %s
        """,
        (credit_id,),
    )
    _expect_error(
        db,
        "DELETE FROM adventurer.journey_credits WHERE id = %s",
        (credit_id,),
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.activities
        SET elevation_gain_m = 90
        WHERE id = %s
        """,
        (activity_id,),
    )
    _expect_error(
        db,
        "DELETE FROM adventurer.activities WHERE id = %s",
        (activity_id,),
    )
    db.execute(
        """
        UPDATE adventurer.activities
        SET note = 'still editable'
        WHERE id = %s
        """,
        (activity_id,),
    )
    db.execute("SELECT set_config('adventurer.privacy_removal', 'on', true)")
    db.execute(
        "SELECT set_config('adventurer.privacy_removal_user_id', %s, true)",
        (str(user_id),),
    )
    _expect_error(
        db,
        "DELETE FROM adventurer.journey_credits WHERE id = %s",
        (credit_id,),
    )
    db.execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
    _expect_error(
        db,
        "DELETE FROM adventurer.journey_credits WHERE id = %s",
        (credit_id,),
    )
    db.execute(
        """
        UPDATE adventurer.users
        SET deletion_state = 'deleting'
        WHERE id = %s
        """,
        (user_id,),
    )
    deleted = db.execute(
        "DELETE FROM adventurer.journey_credits WHERE id = %s",
        (credit_id,),
    )
    assert deleted.rowcount == 1


def test_snapshot_and_feed_rows_delete_only_for_the_subject(db):
    first = _user(db, "Lea")
    second = _user(db, "Ned")
    route_id = _route(db, "snapshot-route")
    owner = _user(db, "OwnerTwo")
    expedition_id = db.execute(
        """
        INSERT INTO adventurer.expeditions (
            slug, name, route_id, visibility, status, kind, owner_user_id
        )
        VALUES ('snap', 'Snap', %s, 'private', 'archived', 'private', %s)
        RETURNING id
        """,
        (route_id, owner),
    ).fetchone()["id"]
    first_journey = _journey(db, first, route_id, "completed", 1, True)
    second_journey = _journey(db, second, route_id, "completed", 1, True)
    # Completed journeys are not active, so both users can be current on the route.
    first_participation = db.execute(
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id, status,
            final_standings_eligible
        )
        VALUES (%s, %s, %s, %s, 'withdrawn', true)
        RETURNING id
        """,
        (first, expedition_id, route_id, first_journey),
    ).fetchone()["id"]
    second_participation = db.execute(
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id, status,
            final_standings_eligible
        )
        VALUES (%s, %s, %s, %s, 'withdrawn', true)
        RETURNING id
        """,
        (second, expedition_id, route_id, second_journey),
    ).fetchone()["id"]
    first_result = db.execute(
        """
        INSERT INTO adventurer.expedition_result_entries (
            expedition_id, participation_id, user_id, journey_id,
            display_name, total_m, rank, checkpoint_name, captured_at
        )
        VALUES (%s, %s, %s, %s, 'Lea', 1000, 1, 'Summit', now())
        RETURNING id
        """,
        (expedition_id, first_participation, first, first_journey),
    ).fetchone()["id"]
    second_result = db.execute(
        """
        INSERT INTO adventurer.expedition_result_entries (
            expedition_id, participation_id, user_id, journey_id,
            display_name, total_m, rank, checkpoint_name, captured_at
        )
        VALUES (%s, %s, %s, %s, 'Ned', 500, 2, 'Camp', now())
        RETURNING id
        """,
        (expedition_id, second_participation, second, second_journey),
    ).fetchone()["id"]
    db.execute(
        """
        INSERT INTO adventurer.expedition_updates (
            expedition_id, user_id, participation_id, kind, elevation_m
        )
        VALUES (%s, %s, %s, 'summit_reached', 1000)
        """,
        (expedition_id, first, first_participation),
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.expedition_result_entries
        SET display_name = 'Hidden'
        WHERE id = %s
        """,
        (first_result,),
    )
    _expect_error(
        db,
        "DELETE FROM adventurer.expedition_result_entries WHERE id = %s",
        (first_result,),
    )
    db.execute("SELECT set_config('adventurer.privacy_removal', 'on', true)")
    db.execute(
        "SELECT set_config('adventurer.privacy_removal_user_id', %s, true)",
        (str(first),),
    )
    db.execute(
        "DELETE FROM adventurer.expedition_result_entries WHERE id = %s",
        (first_result,),
    )
    db.execute(
        """
        DELETE FROM adventurer.expedition_updates
        WHERE user_id = %s AND expedition_id = %s
        """,
        (first, expedition_id),
    )
    _expect_error(
        db,
        "DELETE FROM adventurer.expedition_result_entries WHERE id = %s",
        (second_result,),
    )
    kept = db.execute(
        """
        SELECT display_name, total_m, rank
        FROM adventurer.expedition_result_entries
        WHERE id = %s
        """,
        (second_result,),
    ).fetchone()
    assert kept["display_name"] == "Ned"
    assert kept["rank"] == 2
    assert kept["total_m"] == 500
