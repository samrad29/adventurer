"""Isolated-database checks for the multi-metric progress schema.

Uses postgresql://postgres:postgres@127.0.0.1:54329/adventurer_migration_test
only. Never reads DATABASE_URL and never contacts a hosted database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
TEST_URL = "postgresql://postgres:postgres@127.0.0.1:54329/adventurer_migration_test"
EVEREST_TARGET = Decimal("8848.95360")


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["TEST_DATABASE_URL"] = TEST_URL
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _upgrade() -> None:
    result = _alembic("upgrade", "head")
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed\n{result.stdout}\n{result.stderr}")


def _connect():
    try:
        return psycopg.connect(TEST_URL, row_factory=dict_row, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.fail(f"isolated Postgres is not listening on 127.0.0.1:54329: {exc}")


@pytest.fixture()
def db():
    _upgrade()
    connection = _connect()
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _expect_error(conn, sql: str, params: tuple = (), message: str | None = None) -> None:
    conn.execute("SAVEPOINT probe")
    try:
        conn.execute(sql, params)
    except psycopg.Error as exc:
        conn.execute("ROLLBACK TO SAVEPOINT probe")
        if message is not None and message not in str(exc):
            raise AssertionError(f"expected {message!r} in {exc}") from exc
        return
    conn.execute("ROLLBACK TO SAVEPOINT probe")
    raise AssertionError("statement should have failed: " + sql)


def _expect_deferred(conn, statements: list[tuple[str, tuple]], message: str) -> None:
    conn.execute("SAVEPOINT probe")
    try:
        for sql, params in statements:
            conn.execute(sql, params)
        _check_shape(conn)
    except psycopg.Error as exc:
        conn.execute("ROLLBACK TO SAVEPOINT probe")
        if message not in str(exc):
            raise AssertionError(f"expected {message!r} in {exc}") from exc
        return
    conn.execute("ROLLBACK TO SAVEPOINT probe")
    raise AssertionError(f"deferred constraint should have failed with {message!r}")


def _columns(conn, table: str) -> set[str]:
    return {
        row["column_name"]
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'adventurer'
              AND table_name = %s
            """,
            (table,),
        ).fetchall()
    }


def _user(conn, name: str) -> int:
    return conn.execute(
        """
        INSERT INTO adventurer.users (username, username_key, password_hash)
        VALUES (%s, %s, 'hash')
        RETURNING id
        """,
        (name, name.casefold()),
    ).fetchone()["id"]


def _activity(conn, user_id: int, elevation, distance) -> int:
    activity_id = conn.execute(
        """
        INSERT INTO adventurer.activities (
            user_id, activity_date, elevation_gain_m, distance_m, note
        )
        VALUES (%s, DATE '2026-01-01', %s, %s, 'note')
        RETURNING id
        """,
        (user_id, elevation, distance),
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


def _draft(conn, slug: str) -> int:
    return conn.execute(
        """
        INSERT INTO adventurer.routes (slug, name, region, summary, status)
        VALUES (%s, %s, 'Test', 'A test route', 'draft')
        RETURNING id
        """,
        (slug, slug),
    ).fetchone()["id"]


def _require(conn, route_id: int, metric: str, amount: str) -> None:
    conn.execute(
        """
        INSERT INTO adventurer.route_requirements (
            route_id, metric_code, target_amount
        )
        VALUES (%s, %s, %s)
        """,
        (route_id, metric, amount),
    )


def _checkpoint(conn, route_id: int, slug: str, position: int) -> int:
    return conn.execute(
        """
        INSERT INTO adventurer.route_checkpoints (route_id, slug, name, position)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (route_id, slug, slug, position),
    ).fetchone()["id"]


def _threshold(conn, checkpoint_id: int, route_id: int, metric: str, amount: str) -> None:
    conn.execute(
        """
        INSERT INTO adventurer.checkpoint_requirements (
            checkpoint_id, route_id, metric_code, threshold_amount
        )
        VALUES (%s, %s, %s, %s)
        """,
        (checkpoint_id, route_id, metric, amount),
    )


_SHAPE = """
    adventurer.route_progress_shape_routes,
    adventurer.route_progress_shape_requirements,
    adventurer.route_progress_shape_checkpoints,
    adventurer.route_progress_shape_checkpoint_requirements
"""


def _check_shape(conn) -> None:
    conn.execute(f"SET CONSTRAINTS {_SHAPE} IMMEDIATE")
    conn.execute(f"SET CONSTRAINTS {_SHAPE} DEFERRED")


def _publish(conn, route_id: int) -> None:
    conn.execute(
        "UPDATE adventurer.routes SET status = 'published' WHERE id = %s",
        (route_id,),
    )
    _check_shape(conn)


def _journey(conn, user_id: int, route_id: int, status: str = "active") -> int:
    return conn.execute(
        """
        INSERT INTO adventurer.route_journeys (
            user_id, route_id, status, attempt_number, is_current
        )
        VALUES (%s, %s, %s, 1, true)
        RETURNING id
        """,
        (user_id, route_id, status),
    ).fetchone()["id"]


def _assign(conn, activity_id: int, journey_id: int, user_id: int, route_id: int) -> int:
    return conn.execute(
        """
        INSERT INTO adventurer.activity_journey_assignments (
            activity_id, journey_id, user_id, route_id
        )
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (activity_id, journey_id, user_id, route_id),
    ).fetchone()["id"]


def _credit(conn, assignment_id: int, journey_id: int, route_id: int, metric: str, amount: str) -> int:
    return conn.execute(
        """
        INSERT INTO adventurer.journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (assignment_id, journey_id, route_id, metric, amount),
    ).fetchone()["id"]


def _clear_progress_for_downgrade(conn) -> None:
    """Remove committed rows a safe downgrade is not allowed to invent or drop."""
    conn.execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
    conn.execute(
        """
        UPDATE adventurer.users
        SET deletion_state = 'deleting'
        WHERE deletion_state IS DISTINCT FROM 'deleting'
        """
    )
    conn.execute("DELETE FROM adventurer.expedition_update_metrics")
    conn.execute("DELETE FROM adventurer.expedition_updates")
    conn.execute("DELETE FROM adventurer.expedition_result_metrics")
    conn.execute("DELETE FROM adventurer.expedition_result_entries")
    conn.execute("DELETE FROM adventurer.journey_credits")
    if (
        conn.execute(
            "SELECT to_regclass('adventurer.journey_metric_totals') AS name"
        ).fetchone()["name"]
        is not None
    ):
        conn.execute("DELETE FROM adventurer.journey_metric_totals")
    conn.execute("DELETE FROM adventurer.activity_journey_assignments")
    conn.execute("DELETE FROM adventurer.expedition_participations")
    conn.execute("DELETE FROM adventurer.route_journeys")
    conn.execute("DELETE FROM adventurer.routes WHERE slug <> 'everest'")
    conn.execute(
        """
        DELETE FROM adventurer.activities
        WHERE elevation_gain_m IS NULL
           OR elevation_gain_m <= 0
           OR elevation_gain_m IS DISTINCT FROM elevation_gain_m::numeric(12, 5)
           OR (
               distance_m IS NOT NULL
               AND (
                   distance_m <= 0
                   OR distance_m IS DISTINCT FROM distance_m::numeric(14, 3)
               )
           )
        """
    )
    conn.execute(
        """
        UPDATE adventurer.users
        SET deletion_state = NULL
        WHERE deletion_state = 'deleting'
        """
    )
    conn.commit()


def test_upgrade_preserves_everest_checkpoints_and_activity_history():
    _upgrade()
    with _connect() as conn:
        _clear_progress_for_downgrade(conn)
    downgrade = _alembic("downgrade", "0008_privacy_removals")
    if downgrade.returncode != 0:
        _upgrade()
        pytest.fail(f"downgrade to 0008 failed\n{downgrade.stdout}\n{downgrade.stderr}")
    try:
        with _connect() as conn:
            route = conn.execute(
                """
                SELECT id, slug, status, target_elevation_m
                FROM adventurer.routes
                WHERE slug = 'everest'
                """
            ).fetchone()
            checkpoints = conn.execute(
                """
                SELECT id, slug, position, elevation_m
                FROM adventurer.route_checkpoints
                WHERE route_id = %s
                ORDER BY position
                """,
                (route["id"],),
            ).fetchall()
            expedition_id = conn.execute(
                "SELECT id FROM adventurer.expeditions WHERE slug = 'everest'"
            ).fetchone()["id"]
            user_id = _user(conn, "metric-history")
            activity_id = conn.execute(
                """
                INSERT INTO adventurer.activities (
                    user_id, activity_date, elevation_gain_m, distance_m, note
                )
                VALUES (%s, DATE '2026-02-02', 30.48000, 2500.500, 'kept')
                RETURNING id
                """,
                (user_id,),
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
            conn.commit()
        upgrade = _alembic("upgrade", "head")
        if upgrade.returncode != 0:
            pytest.fail(f"upgrade failed\n{upgrade.stdout}\n{upgrade.stderr}")
        with _connect() as conn:
            migrated = conn.execute(
                """
                SELECT id, slug, status
                FROM adventurer.routes
                WHERE slug = 'everest'
                """
            ).fetchone()
            assert migrated["id"] == route["id"]
            assert migrated["status"] == "published"
            assert "target_elevation_m" not in _columns(conn, "routes")
            target = conn.execute(
                """
                SELECT target_amount
                FROM adventurer.route_requirements
                WHERE route_id = %s AND metric_code = 'elevation'
                """,
                (route["id"],),
            ).fetchone()["target_amount"]
            assert target == route["target_elevation_m"] == EVEREST_TARGET
            migrated_checkpoints = conn.execute(
                """
                SELECT checkpoint.id, checkpoint.slug, checkpoint.position,
                       requirement.threshold_amount
                FROM adventurer.route_checkpoints AS checkpoint
                JOIN adventurer.checkpoint_requirements AS requirement
                    ON requirement.checkpoint_id = checkpoint.id
                WHERE checkpoint.route_id = %s
                ORDER BY checkpoint.position
                """,
                (route["id"],),
            ).fetchall()
            assert [
                (row["id"], row["slug"], row["position"], row["threshold_amount"])
                for row in migrated_checkpoints
            ] == [
                (row["id"], row["slug"], row["position"], row["elevation_m"])
                for row in checkpoints
            ]
            assert migrated_checkpoints[-1]["slug"] == "summit"
            assert migrated_checkpoints[-1]["threshold_amount"] == EVEREST_TARGET
            assert "elevation_m" not in _columns(conn, "route_checkpoints")
            assert conn.execute(
                "SELECT id FROM adventurer.expeditions WHERE slug = 'everest'"
            ).fetchone()["id"] == expedition_id
            activity = conn.execute(
                """
                SELECT id, user_id, elevation_gain_m, distance_m, note
                FROM adventurer.activities
                WHERE id = %s
                """,
                (activity_id,),
            ).fetchone()
            assert activity["user_id"] == user_id
            assert activity["elevation_gain_m"] == Decimal("30.48000")
            assert activity["distance_m"] == Decimal("2500.500")
            assert activity["note"] == "kept"
            elevation = conn.execute(
                """
                SELECT numeric_precision, numeric_scale, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'adventurer'
                  AND table_name = 'activities'
                  AND column_name = 'elevation_gain_m'
                """
            ).fetchone()
            distance = conn.execute(
                """
                SELECT numeric_precision, numeric_scale, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'adventurer'
                  AND table_name = 'activities'
                  AND column_name = 'distance_m'
                """
            ).fetchone()
            assert (elevation["numeric_precision"], elevation["numeric_scale"], elevation["is_nullable"]) == (16, 6, "YES")
            assert (distance["numeric_precision"], distance["numeric_scale"], distance["is_nullable"]) == (16, 6, "YES")
            metrics = {
                row["code"]: (row["storage_unit"], row["display_unit"])
                for row in conn.execute(
                    "SELECT code, storage_unit, display_unit FROM adventurer.progress_metrics"
                ).fetchall()
            }
            assert metrics == {"elevation": ("m", "ft"), "distance": ("m", "mi")}
            assert "credited_elevation_m" not in _columns(conn, "journey_credits")
            assert "credited_amount" in _columns(conn, "journey_credits")
            assert "activity_id" not in _columns(conn, "journey_credits")
            assert "final_rank" in _columns(conn, "expedition_result_entries")
            assert "rank" not in _columns(conn, "expedition_result_entries")
            assert "total_m" not in _columns(conn, "expedition_result_entries")
            assert "elevation_m" not in _columns(conn, "expedition_updates")
            for name in (
                "progress_metrics",
                "route_requirements",
                "checkpoint_requirements",
                "activity_journey_assignments",
                "expedition_result_metrics",
                "expedition_update_metrics",
            ):
                assert conn.execute(
                    "SELECT to_regclass(%s) AS name",
                    (f"adventurer.{name}",),
                ).fetchone()["name"] is not None
            conn.execute("DELETE FROM adventurer.users WHERE id = %s", (user_id,))
            conn.commit()
    finally:
        _upgrade()


def test_downgrade_restores_elevation_columns_without_inventing_credits():
    _upgrade()
    with _connect() as conn:
        _clear_progress_for_downgrade(conn)
    try:
        result = _alembic("downgrade", "0008_privacy_removals")
        if result.returncode != 0:
            pytest.fail(f"downgrade failed\n{result.stdout}\n{result.stderr}")
        with _connect() as conn:
            target = conn.execute(
                """
                SELECT target_elevation_m
                FROM adventurer.routes
                WHERE slug = 'everest'
                """
            ).fetchone()["target_elevation_m"]
            summit = conn.execute(
                """
                SELECT elevation_m
                FROM adventurer.route_checkpoints
                WHERE slug = 'summit'
                """
            ).fetchone()["elevation_m"]
            assert target == summit == EVEREST_TARGET
            assert "credited_elevation_m" in _columns(conn, "journey_credits")
            assert conn.execute(
                "SELECT count(*) AS n FROM adventurer.journey_credits"
            ).fetchone()["n"] == 0
            assert conn.execute(
                "SELECT to_regclass('adventurer.progress_metrics') AS name"
            ).fetchone()["name"] is None
            assert conn.execute(
                "SELECT to_regclass('adventurer.activity_journey_assignments') AS name"
            ).fetchone()["name"] is None
            assert "rank" in _columns(conn, "expedition_result_entries")
            assert "elevation_m" in _columns(conn, "expedition_updates")
    finally:
        _upgrade()


def test_downgrade_refuses_when_a_distance_requirement_exists():
    _upgrade()
    with _connect() as conn:
        route_id = _draft(conn, "distance-only-downgrade")
        _require(conn, route_id, "distance", "1609.344000")
        conn.commit()
    try:
        result = _alembic("downgrade", "0008_privacy_removals")
        assert result.returncode != 0
        assert "cannot be downgraded" in result.stderr
        with _connect() as conn:
            assert conn.execute(
                "SELECT to_regclass('adventurer.progress_metrics') AS name"
            ).fetchone()["name"] is not None
            conn.execute(
                "DELETE FROM adventurer.routes WHERE slug = 'distance-only-downgrade'"
            )
            conn.commit()
    finally:
        _upgrade()


def test_requirement_rules_accept_both_metrics_and_reject_invalid_shapes(db):
    route_id = _draft(db, "both-metrics")
    _require(db, route_id, "elevation", "100")
    _require(db, route_id, "distance", "50")
    first = _checkpoint(db, route_id, "gate", 0)
    last = _checkpoint(db, route_id, "finish", 1)
    _threshold(db, first, route_id, "elevation", "40")
    _threshold(db, last, route_id, "elevation", "100")
    _threshold(db, last, route_id, "distance", "50")
    _publish(db, route_id)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_requirements (route_id, metric_code, target_amount)
        VALUES (%s, 'elevation', 25)
        """,
        (route_id,),
        "duplicate key",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.checkpoint_requirements (
            checkpoint_id, route_id, metric_code, threshold_amount
        )
        VALUES (%s, %s, 'pace', 1)
        """,
        (first, route_id),
        "violates foreign key",
    )
    _expect_deferred(
        db,
        [
            (
                """
                INSERT INTO adventurer.routes (slug, name, region, summary, status)
                VALUES ('empty-published', 'Empty', 'Test', 'A test route', 'published')
                """,
                (),
            )
        ],
        "a published route needs at least one progress requirement",
    )
    bare = _draft(db, "bare-checkpoint")
    _require(db, bare, "elevation", "10")
    _expect_deferred(
        db,
        [
            (
                """
                INSERT INTO adventurer.route_checkpoints (route_id, slug, name, position)
                VALUES (%s, 'nowhere', 'Nowhere', 0)
                """,
                (bare,),
            )
        ],
        "a checkpoint needs at least one progress requirement",
    )
    _expect_deferred(
        db,
        [
            (
                """
                WITH route AS (
                    INSERT INTO adventurer.routes (slug, name, region, summary, status)
                    VALUES ('threshold-too-high', 'High', 'Test', 'A test route', 'draft')
                    RETURNING id
                ),
                requirement AS (
                    INSERT INTO adventurer.route_requirements (
                        route_id, metric_code, target_amount
                    )
                    SELECT id, 'elevation', 10 FROM route
                ),
                checkpoint AS (
                    INSERT INTO adventurer.route_checkpoints (
                        route_id, slug, name, position
                    )
                    SELECT id, 'over', 'Over', 0 FROM route
                    RETURNING id, route_id
                )
                INSERT INTO adventurer.checkpoint_requirements (
                    checkpoint_id, route_id, metric_code, threshold_amount
                )
                SELECT id, route_id, 'elevation', 11 FROM checkpoint
                """,
                (),
            )
        ],
        "a checkpoint threshold cannot exceed the route target",
    )
    _expect_deferred(
        db,
        [
            (
                """
                WITH route AS (
                    INSERT INTO adventurer.routes (slug, name, region, summary, status)
                    VALUES ('backwards', 'Backwards', 'Test', 'A test route', 'draft')
                    RETURNING id
                ),
                requirement AS (
                    INSERT INTO adventurer.route_requirements (
                        route_id, metric_code, target_amount
                    )
                    SELECT id, 'elevation', 20 FROM route
                ),
                early AS (
                    INSERT INTO adventurer.route_checkpoints (
                        route_id, slug, name, position
                    )
                    SELECT id, 'early', 'Early', 0 FROM route
                    RETURNING id, route_id
                ),
                late AS (
                    INSERT INTO adventurer.route_checkpoints (
                        route_id, slug, name, position
                    )
                    SELECT id, 'late', 'Late', 1 FROM route
                    RETURNING id, route_id
                ),
                early_requirement AS (
                    INSERT INTO adventurer.checkpoint_requirements (
                        checkpoint_id, route_id, metric_code, threshold_amount
                    )
                    SELECT id, route_id, 'elevation', 20 FROM early
                )
                INSERT INTO adventurer.checkpoint_requirements (
                    checkpoint_id, route_id, metric_code, threshold_amount
                )
                SELECT id, route_id, 'elevation', 10 FROM late
                """,
                (),
            )
        ],
        "checkpoint requirements must stay monotonic",
    )
    _expect_deferred(
        db,
        [
            (
                """
                WITH route AS (
                    INSERT INTO adventurer.routes (slug, name, region, summary, status)
                    VALUES ('missing-later-metric', 'Missing', 'Test', 'A test route', 'draft')
                    RETURNING id
                ),
                elevation_requirement AS (
                    INSERT INTO adventurer.route_requirements (
                        route_id, metric_code, target_amount
                    )
                    SELECT id, 'elevation', 20 FROM route
                ),
                distance_requirement AS (
                    INSERT INTO adventurer.route_requirements (
                        route_id, metric_code, target_amount
                    )
                    SELECT id, 'distance', 5 FROM route
                ),
                early AS (
                    INSERT INTO adventurer.route_checkpoints (
                        route_id, slug, name, position
                    )
                    SELECT id, 'early', 'Early', 0 FROM route
                    RETURNING id, route_id
                ),
                late AS (
                    INSERT INTO adventurer.route_checkpoints (
                        route_id, slug, name, position
                    )
                    SELECT id, 'late', 'Late', 1 FROM route
                    RETURNING id, route_id
                ),
                early_requirement AS (
                    INSERT INTO adventurer.checkpoint_requirements (
                        checkpoint_id, route_id, metric_code, threshold_amount
                    )
                    SELECT id, route_id, 'elevation', 10 FROM early
                )
                INSERT INTO adventurer.checkpoint_requirements (
                    checkpoint_id, route_id, metric_code, threshold_amount
                )
                SELECT id, route_id, 'distance', 5 FROM late
                """,
                (),
            )
        ],
        "checkpoint requirements must stay monotonic",
    )
    _expect_deferred(
        db,
        [
            (
                """
                WITH route AS (
                    INSERT INTO adventurer.routes (slug, name, region, summary, status)
                    VALUES ('short-of-target', 'Short', 'Test', 'A test route', 'draft')
                    RETURNING id
                ),
                requirement AS (
                    INSERT INTO adventurer.route_requirements (
                        route_id, metric_code, target_amount
                    )
                    SELECT id, 'elevation', 20 FROM route
                ),
                checkpoint AS (
                    INSERT INTO adventurer.route_checkpoints (
                        route_id, slug, name, position
                    )
                    SELECT id, 'almost', 'Almost', 0 FROM route
                    RETURNING id, route_id
                )
                INSERT INTO adventurer.checkpoint_requirements (
                    checkpoint_id, route_id, metric_code, threshold_amount
                )
                SELECT id, route_id, 'elevation', 15 FROM checkpoint
                """,
                (),
            )
        ],
        "the last checkpoint must require every route metric at its target",
    )


def test_requirements_freeze_after_the_first_journey(db):
    route_id = _draft(db, "frozen-route")
    _require(db, route_id, "elevation", "100")
    checkpoint_id = _checkpoint(db, route_id, "summit", 0)
    _threshold(db, checkpoint_id, route_id, "elevation", "100")
    _publish(db, route_id)
    user_id = _user(db, "freezer")
    _journey(db, user_id, route_id)
    _expect_error(
        db,
        """
        UPDATE adventurer.route_requirements
        SET target_amount = 90
        WHERE route_id = %s AND metric_code = 'elevation'
        """,
        (route_id,),
        "route requirements are immutable once a journey exists",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_requirements (route_id, metric_code, target_amount)
        VALUES (%s, 'distance', 10)
        """,
        (route_id,),
        "route requirements are immutable once a journey exists",
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.checkpoint_requirements
        SET threshold_amount = 80
        WHERE checkpoint_id = %s
        """,
        (checkpoint_id,),
        "route requirements are immutable once a journey exists",
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.route_checkpoints
        SET position = 2
        WHERE id = %s
        """,
        (checkpoint_id,),
        "route requirements are immutable once a journey exists",
    )


def test_measurements_allow_null_and_zero_but_not_negative(db):
    user_id = _user(db, "measurer")
    _activity(db, user_id, None, None)
    _activity(db, user_id, 0, 0)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.activities (
            user_id, activity_date, elevation_gain_m
        )
        VALUES (%s, DATE '2026-01-01', -1)
        """,
        (user_id,),
        "activities_elevation_chk",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.activities (
            user_id, activity_date, elevation_gain_m, distance_m
        )
        VALUES (%s, DATE '2026-01-01', 1, -1)
        """,
        (user_id,),
        "activities_distance_chk",
    )


def test_assignment_and_credit_rules(db):
    route_id = _draft(db, "credit-route")
    _require(db, route_id, "elevation", "100")
    _require(db, route_id, "distance", "50")
    gate = _checkpoint(db, route_id, "gate", 0)
    finish = _checkpoint(db, route_id, "finish", 1)
    _threshold(db, gate, route_id, "elevation", "40")
    _threshold(db, finish, route_id, "elevation", "100")
    _threshold(db, finish, route_id, "distance", "50")
    _publish(db, route_id)
    other_id = _draft(db, "other-credit-route")
    _require(db, other_id, "elevation", "100")
    other_checkpoint = _checkpoint(db, other_id, "summit", 0)
    _threshold(db, other_checkpoint, other_id, "elevation", "100")
    _publish(db, other_id)

    owner = _user(db, "owner")
    stranger = _user(db, "stranger")
    activity_id = _activity(db, owner, "80", "30")
    journey_id = _journey(db, owner, route_id)
    other_journey = _journey(db, owner, other_id, status="paused")
    assignment_id = _assign(db, activity_id, journey_id, owner, route_id)
    _credit(db, assignment_id, journey_id, route_id, "elevation", "60")
    _credit(db, assignment_id, journey_id, route_id, "distance", "20")
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (%s, %s, %s, 'elevation', 5)
        """,
        (assignment_id, journey_id, route_id),
        "duplicate key",
    )
    wrong_activity = _activity(db, owner, "15", None)
    wrong_assignment = _assign(db, wrong_activity, journey_id, owner, route_id)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (%s, %s, %s, 'elevation', 5)
        """,
        (wrong_assignment, other_journey, route_id),
        "violates foreign key",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.activity_journey_assignments (
            activity_id, journey_id, user_id, route_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (activity_id, other_journey, owner, other_id),
        "duplicate key",
    )
    stranger_activity = _activity(db, stranger, "10", None)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.activity_journey_assignments (
            activity_id, journey_id, user_id, route_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (stranger_activity, journey_id, stranger, route_id),
        "violates foreign key",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.activity_journey_assignments (
            activity_id, journey_id, user_id, route_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (stranger_activity, journey_id, owner, route_id),
        "violates foreign key",
    )
    elevation_only = _activity(db, owner, "10", "10")
    elevation_assignment = _assign(db, elevation_only, other_journey, owner, other_id)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (%s, %s, %s, 'distance', 1)
        """,
        (elevation_assignment, other_journey, other_id),
        "violates foreign key",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (%s, %s, %s, 'elevation', 11)
        """,
        (elevation_assignment, other_journey, other_id),
        "credited amount cannot exceed the activity measurement",
    )
    extra = _activity(db, owner, "50", None)
    third_route = _draft(db, "cap-route")
    _require(db, third_route, "elevation", "100")
    third_checkpoint = _checkpoint(db, third_route, "summit", 0)
    _threshold(db, third_checkpoint, third_route, "elevation", "100")
    _publish(db, third_route)
    third_journey = _journey(db, owner, third_route, status="paused")
    third_assignment = _assign(db, extra, third_journey, owner, third_route)
    _credit(db, third_assignment, third_journey, third_route, "elevation", "40")
    another = _activity(db, owner, "70", None)
    another_assignment = _assign(db, another, third_journey, owner, third_route)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (%s, %s, %s, 'elevation', 70)
        """,
        (another_assignment, third_journey, third_route),
        "credit exceeds the route target",
    )


def test_completed_journey_protects_both_metrics_and_allows_account_deletion(db):
    route_id = _draft(db, "completed-route")
    _require(db, route_id, "elevation", "100")
    _require(db, route_id, "distance", "50")
    checkpoint_id = _checkpoint(db, route_id, "summit", 0)
    _threshold(db, checkpoint_id, route_id, "elevation", "100")
    _threshold(db, checkpoint_id, route_id, "distance", "50")
    _publish(db, route_id)
    user_id = _user(db, "finisher")
    activity_id = _activity(db, user_id, "80", "30")
    journey_id = _journey(db, user_id, route_id)
    assignment_id = _assign(db, activity_id, journey_id, user_id, route_id)
    elevation_credit = _credit(db, assignment_id, journey_id, route_id, "elevation", "80")
    distance_credit = _credit(db, assignment_id, journey_id, route_id, "distance", "30")
    db.execute(
        """
        UPDATE adventurer.route_journeys
        SET status = 'completed', completed_at = now()
        WHERE id = %s
        """,
        (journey_id,),
    )
    db.execute(
        "UPDATE adventurer.activities SET note = 'still editable' WHERE id = %s",
        (activity_id,),
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.activities
        SET elevation_gain_m = 90
        WHERE id = %s
        """,
        (activity_id,),
        "elevation credited to a completed journey cannot change",
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.activities
        SET distance_m = 40
        WHERE id = %s
        """,
        (activity_id,),
        "distance credited to a completed journey cannot change",
    )
    _expect_error(
        db,
        "DELETE FROM adventurer.journey_credits WHERE id = %s",
        (elevation_credit,),
        "credits on a completed journey cannot be changed",
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.journey_credits
        SET credited_amount = 70
        WHERE id = %s
        """,
        (distance_credit,),
        "credits on a completed journey cannot be changed",
    )
    _expect_error(
        db,
        "DELETE FROM adventurer.activities WHERE id = %s",
        (activity_id,),
        "an activity credited to a completed journey cannot be deleted",
    )
    db.execute(
        """
        UPDATE adventurer.users
        SET deletion_state = 'deleting'
        WHERE id = %s
        """,
        (user_id,),
    )
    db.execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
    db.execute(
        "DELETE FROM adventurer.journey_credits WHERE id = %s",
        (elevation_credit,),
    )
    db.execute(
        "DELETE FROM adventurer.journey_credits WHERE id = %s",
        (distance_credit,),
    )
    assert db.execute(
        "SELECT count(*) AS n FROM adventurer.journey_credits WHERE assignment_id = %s",
        (assignment_id,),
    ).fetchone()["n"] == 0


def test_result_and_update_metrics_follow_snapshot_rules(db):
    route_id = db.execute(
        "SELECT id FROM adventurer.routes WHERE slug = 'everest'"
    ).fetchone()["id"]
    expedition_id = db.execute(
        "SELECT id FROM adventurer.expeditions WHERE slug = 'everest'"
    ).fetchone()["id"]
    user_id = _user(db, "snapshot-user")
    journey_id = _journey(db, user_id, route_id)
    participation_id = db.execute(
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id
        )
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (user_id, expedition_id, route_id, journey_id),
    ).fetchone()["id"]
    entry_id = db.execute(
        """
        INSERT INTO adventurer.expedition_result_entries (
            expedition_id, participation_id, user_id, journey_id,
            display_name, final_rank, checkpoint_name, captured_at
        )
        VALUES (%s, %s, %s, %s, 'Snapshot User', NULL, 'Summit', now())
        RETURNING id
        """,
        (expedition_id, participation_id, user_id, journey_id),
    ).fetchone()["id"]
    db.execute(
        """
        INSERT INTO adventurer.expedition_result_metrics (
            result_entry_id, metric_code, total_amount
        )
        VALUES (%s, 'elevation', 10), (%s, 'distance', 4)
        """,
        (entry_id, entry_id),
    )
    stored_rank = db.execute(
        "SELECT final_rank FROM adventurer.expedition_result_entries WHERE id = %s",
        (entry_id,),
    ).fetchone()["final_rank"]
    assert stored_rank is None
    _expect_error(
        db,
        """
        UPDATE adventurer.expedition_result_entries
        SET final_rank = 1
        WHERE id = %s
        """,
        (entry_id,),
        "immutable",
    )
    other_user = _user(db, "snapshot-rank")
    other_journey = _journey(db, other_user, route_id)
    other_participation = db.execute(
        """
        INSERT INTO adventurer.expedition_participations (
            user_id, expedition_id, route_id, journey_id
        )
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (other_user, expedition_id, route_id, other_journey),
    ).fetchone()["id"]
    _expect_error(
        db,
        """
        INSERT INTO adventurer.expedition_result_entries (
            expedition_id, participation_id, user_id, journey_id,
            display_name, final_rank, checkpoint_name, captured_at
        )
        VALUES (%s, %s, %s, %s, 'Rank User', 0, 'Summit', now())
        """,
        (expedition_id, other_participation, other_user, other_journey),
        "expedition_result_entries_final_rank_chk",
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.expedition_result_metrics
        SET total_amount = 11
        WHERE result_entry_id = %s AND metric_code = 'elevation'
        """,
        (entry_id,),
        "expedition result metrics are immutable",
    )
    _expect_error(
        db,
        """
        DELETE FROM adventurer.expedition_result_metrics
        WHERE result_entry_id = %s AND metric_code = 'distance'
        """,
        (entry_id,),
        "expedition result metrics are immutable",
    )
    update_id = db.execute(
        """
        INSERT INTO adventurer.expedition_updates (
            expedition_id, user_id, participation_id, kind, checkpoint_name
        )
        VALUES (%s, %s, %s, 'checkpoint_reached', 'Base Camp')
        RETURNING id
        """,
        (expedition_id, user_id, participation_id),
    ).fetchone()["id"]
    db.execute(
        """
        INSERT INTO adventurer.expedition_update_metrics (
            update_id, metric_code, credited_amount
        )
        VALUES (%s, 'elevation', 3), (%s, 'distance', 1)
        """,
        (update_id, update_id),
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.expedition_update_metrics
        SET credited_amount = 4
        WHERE update_id = %s AND metric_code = 'elevation'
        """,
        (update_id,),
        "expedition update metrics are immutable",
    )
    _expect_error(
        db,
        "DELETE FROM adventurer.expedition_updates WHERE id = %s",
        (update_id,),
        "immutable",
    )
    db.execute("SELECT set_config('adventurer.privacy_removal', 'on', true)")
    db.execute(
        "SELECT set_config('adventurer.privacy_removal_user_id', %s, true)",
        (str(user_id),),
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.expedition_update_metrics
        SET credited_amount = 9
        WHERE update_id = %s AND metric_code = 'distance'
        """,
        (update_id,),
        "expedition update metrics are immutable",
    )
    db.execute("DELETE FROM adventurer.expedition_updates WHERE id = %s", (update_id,))
    db.execute(
        """
        DELETE FROM adventurer.expedition_result_metrics
        WHERE result_entry_id = %s
        """,
        (entry_id,),
    )
    assert db.execute(
        "SELECT count(*) AS n FROM adventurer.expedition_update_metrics WHERE update_id = %s",
        (update_id,),
    ).fetchone()["n"] == 0
    assert db.execute(
        """
        SELECT count(*) AS n
        FROM adventurer.expedition_result_metrics
        WHERE result_entry_id = %s
        """,
        (entry_id,),
    ).fetchone()["n"] == 0


def test_credit_edits_recalculate_both_metric_totals(db):
    route_id = _draft(db, "retotal-route")
    _require(db, route_id, "elevation", "100")
    _require(db, route_id, "distance", "80")
    checkpoint_id = _checkpoint(db, route_id, "summit", 0)
    _threshold(db, checkpoint_id, route_id, "elevation", "100")
    _threshold(db, checkpoint_id, route_id, "distance", "80")
    _publish(db, route_id)
    user_id = _user(db, "retotal-user")
    activity_id = _activity(db, user_id, "90", "70")
    journey_id = _journey(db, user_id, route_id)
    assignment_id = _assign(db, activity_id, journey_id, user_id, route_id)
    credit_id = _credit(db, assignment_id, journey_id, route_id, "elevation", "40")
    db.execute(
        """
        UPDATE adventurer.journey_credits
        SET metric_code = 'distance', credited_amount = 50
        WHERE id = %s
        """,
        (credit_id,),
    )
    totals = {
        row["metric_code"]: row["credited_total"]
        for row in db.execute(
            """
            SELECT metric_code, credited_total
            FROM adventurer.journey_metric_totals
            WHERE journey_id = %s
            """,
            (journey_id,),
        ).fetchall()
    }
    assert totals == {"distance": Decimal("50")}
    _credit(db, assignment_id, journey_id, route_id, "elevation", "90")
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (%s, %s, %s, 'distance', 40)
        """,
        (assignment_id, journey_id, route_id),
        "duplicate key",
    )
    other = _activity(db, user_id, "40", "40")
    other_assignment = _assign(db, other, journey_id, user_id, route_id)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (%s, %s, %s, 'distance', 40)
        """,
        (other_assignment, journey_id, route_id),
        "credit exceeds the route target",
    )
    db.execute("DELETE FROM adventurer.journey_credits WHERE id = %s", (credit_id,))
    distance_total = db.execute(
        """
        SELECT credited_total
        FROM adventurer.journey_metric_totals
        WHERE journey_id = %s AND metric_code = 'distance'
        """,
        (journey_id,),
    ).fetchone()
    assert distance_total is None
    _credit(db, other_assignment, journey_id, route_id, "distance", "40")


def _overlap_writes(statements: list[tuple]) -> list[str]:
    """Run one write per connection and keep them overlapped on the credit lock."""
    release = threading.Event()
    holding = threading.Event()
    outcomes: list[str] = []
    gate = threading.Lock()

    def attempt(sql: str, params: tuple) -> None:
        connection = _connect()
        try:
            connection.execute(sql, params)
            holding.set()
            if not release.wait(timeout=8):
                raise AssertionError("the overlapping credit was not released")
            connection.commit()
            result = "committed"
        except Exception as exc:
            connection.rollback()
            result = str(exc)
        finally:
            connection.close()
        with gate:
            outcomes.append(result)

    threads = [
        threading.Thread(target=attempt, args=statement) for statement in statements
    ]
    for thread in threads:
        thread.start()
    if not holding.wait(timeout=8):
        release.set()
        for thread in threads:
            thread.join(timeout=8)
        raise AssertionError(f"no credit write finished; outcomes={outcomes}")
    deadline = time.time() + 8
    waiter = None
    monitor = _connect()
    monitor.autocommit = True
    try:
        while time.time() < deadline:
            waiter = monitor.execute(
                """
                SELECT wait_event_type, wait_event
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND cardinality(pg_blocking_pids(pid)) > 0
                """
            ).fetchone()
            if waiter is not None:
                break
            time.sleep(0.05)
    finally:
        monitor.close()
    release.set()
    for thread in threads:
        thread.join(timeout=8)
    if waiter is None:
        raise AssertionError(f"the second credit did not wait; outcomes={outcomes}")
    return outcomes


def test_concurrent_credits_cannot_exceed_the_route_target():
    _upgrade()
    with _connect() as conn:
        route_id = _draft(conn, "credit-race")
        _require(conn, route_id, "elevation", "100")
        checkpoint_id = _checkpoint(conn, route_id, "summit", 0)
        _threshold(conn, checkpoint_id, route_id, "elevation", "100")
        _publish(conn, route_id)
        user_id = _user(conn, "race-user")
        journey_id = _journey(conn, user_id, route_id)
        first = _activity(conn, user_id, "70", None)
        second = _activity(conn, user_id, "70", None)
        isolation = conn.execute("SHOW transaction_isolation").fetchone()[
            "transaction_isolation"
        ]
        assert isolation == "read committed"
        first_assignment = _assign(conn, first, journey_id, user_id, route_id)
        second_assignment = _assign(conn, second, journey_id, user_id, route_id)
        conn.commit()
    insert = """
        INSERT INTO adventurer.journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (%s, %s, %s, 'elevation', %s)
    """
    try:
        contested = _overlap_writes(
            [
                (insert, (first_assignment, journey_id, route_id, "60")),
                (insert, (second_assignment, journey_id, route_id, "60")),
            ]
        )
        assert sorted(item.startswith("committed") for item in contested) == [False, True]
        assert any("credit exceeds the route target" in item for item in contested)
        with _connect() as conn:
            total = conn.execute(
                """
                SELECT COALESCE(SUM(credited_amount), 0) AS total
                FROM adventurer.journey_credits
                WHERE journey_id = %s AND metric_code = 'elevation'
                """,
                (journey_id,),
            ).fetchone()["total"]
            assert total == Decimal("60")
            conn.execute(
                "DELETE FROM adventurer.journey_credits WHERE journey_id = %s",
                (journey_id,),
            )
            conn.commit()
        shared = _overlap_writes(
            [
                (insert, (first_assignment, journey_id, route_id, "40")),
                (insert, (second_assignment, journey_id, route_id, "40")),
            ]
        )
        assert sorted(shared) == ["committed", "committed"]
        with _connect() as conn:
            total = conn.execute(
                """
                SELECT COALESCE(SUM(credited_amount), 0) AS total
                FROM adventurer.journey_credits
                WHERE journey_id = %s AND metric_code = 'elevation'
                """,
                (journey_id,),
            ).fetchone()["total"]
            assert total == Decimal("80")
            conn.execute(
                "DELETE FROM adventurer.journey_credits WHERE journey_id = %s",
                (journey_id,),
            )
            first_credit = _credit(
                conn, first_assignment, journey_id, route_id, "elevation", "30"
            )
            second_credit = _credit(
                conn, second_assignment, journey_id, route_id, "elevation", "30"
            )
            conn.commit()
        revise = """
            UPDATE adventurer.journey_credits
            SET credited_amount = 70
            WHERE id = %s
        """
        revised = _overlap_writes(
            [(revise, (first_credit,)), (revise, (second_credit,))]
        )
        assert sorted(item.startswith("committed") for item in revised) == [False, True]
        assert any("credit exceeds the route target" in item for item in revised)
        with _connect() as conn:
            total = conn.execute(
                """
                SELECT COALESCE(SUM(credited_amount), 0) AS total
                FROM adventurer.journey_credits
                WHERE journey_id = %s AND metric_code = 'elevation'
                """,
                (journey_id,),
            ).fetchone()["total"]
            assert total == Decimal("100")
    finally:
        with _connect() as conn:
            conn.execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
            conn.execute(
                """
                UPDATE adventurer.users
                SET deletion_state = 'deleting'
                WHERE id = %s
                """,
                (user_id,),
            )
            conn.execute(
                "DELETE FROM adventurer.journey_credits WHERE journey_id = %s",
                (journey_id,),
            )
            conn.execute("DELETE FROM adventurer.route_journeys WHERE id = %s", (journey_id,))
            conn.execute("DELETE FROM adventurer.routes WHERE id = %s", (route_id,))
            conn.execute("DELETE FROM adventurer.users WHERE id = %s", (user_id,))
            conn.commit()


def test_snapshot_metrics_close_after_the_creating_transaction_commits():
    _upgrade()
    with _connect() as conn:
        route_id = _draft(conn, "snapshot-close")
        _require(conn, route_id, "elevation", "100")
        _require(conn, route_id, "distance", "50")
        checkpoint_id = _checkpoint(conn, route_id, "summit", 0)
        _threshold(conn, checkpoint_id, route_id, "elevation", "100")
        _threshold(conn, checkpoint_id, route_id, "distance", "50")
        _publish(conn, route_id)
        user_id = _user(conn, "snapshot-close-user")
        journey_id = _journey(conn, user_id, route_id)
        expedition_id = conn.execute(
            """
            INSERT INTO adventurer.expeditions (
                slug, name, route_id, visibility, status, kind, owner_user_id
            )
            VALUES (
                'snapshot-close', 'Snapshot Close', %s,
                'private', 'active', 'private', %s
            )
            RETURNING id
            """,
            (route_id, user_id),
        ).fetchone()["id"]
        participation_id = conn.execute(
            """
            INSERT INTO adventurer.expedition_participations (
                user_id, expedition_id, route_id, journey_id
            )
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, expedition_id, route_id, journey_id),
        ).fetchone()["id"]
        entry_id = conn.execute(
            """
            INSERT INTO adventurer.expedition_result_entries (
                expedition_id, participation_id, user_id, journey_id,
                display_name, final_rank, checkpoint_name, captured_at
            )
            VALUES (%s, %s, %s, %s, 'Closer', NULL, 'Summit', now())
            RETURNING id
            """,
            (expedition_id, participation_id, user_id, journey_id),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO adventurer.expedition_result_metrics (
                result_entry_id, metric_code, total_amount
            )
            VALUES (%s, 'elevation', 10)
            """,
            (entry_id,),
        )
        update_id = conn.execute(
            """
            INSERT INTO adventurer.expedition_updates (
                expedition_id, user_id, participation_id, kind, checkpoint_name
            )
            VALUES (%s, %s, %s, 'activity_logged', NULL)
            RETURNING id
            """,
            (expedition_id, user_id, participation_id),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO adventurer.expedition_update_metrics (
                update_id, metric_code, credited_amount
            )
            VALUES (%s, 'elevation', 4)
            """,
            (update_id,),
        )
        assert conn.execute(
            """
            SELECT creating_xid = pg_current_xact_id() AS same
            FROM adventurer.expedition_result_entries
            WHERE id = %s
            """,
            (entry_id,),
        ).fetchone()["same"] is True
        conn.execute("SAVEPOINT released_update")
        released_update_id = conn.execute(
            """
            INSERT INTO adventurer.expedition_updates (
                expedition_id, user_id, participation_id, kind,
                checkpoint_name, creating_xid
            )
            VALUES (%s, %s, %s, 'activity_logged', NULL, '0'::xid8)
            RETURNING id
            """,
            (expedition_id, user_id, participation_id),
        ).fetchone()["id"]
        conn.execute("RELEASE SAVEPOINT released_update")
        assert conn.execute(
            """
            SELECT creating_xid = pg_current_xact_id() AS same
            FROM adventurer.expedition_updates
            WHERE id = %s
            """,
            (released_update_id,),
        ).fetchone()["same"] is True
        conn.execute(
            """
            INSERT INTO adventurer.expedition_update_metrics (
                update_id, metric_code, credited_amount
            )
            VALUES (%s, 'distance', 2)
            """,
            (released_update_id,),
        )
        conn.execute("SAVEPOINT rolled_back_update")
        conn.execute(
            """
            INSERT INTO adventurer.expedition_updates (
                expedition_id, user_id, participation_id, kind, checkpoint_name
            )
            VALUES (%s, %s, %s, 'checkpoint_reached', 'Camp')
            """,
            (expedition_id, user_id, participation_id),
        )
        conn.execute("ROLLBACK TO SAVEPOINT rolled_back_update")
        conn.commit()
    try:
        with _connect() as conn:
            conn.execute(
                "SELECT set_config('adventurer.open_result_entries', %s, true)",
                (f",{entry_id},",),
            )
            conn.execute(
                "SELECT set_config('adventurer.open_expedition_updates', %s, true)",
                (f",{update_id},{released_update_id},",),
            )
            _expect_error(
                conn,
                """
                UPDATE adventurer.expedition_result_entries
                SET creating_xid = pg_current_xact_id()
                WHERE id = %s
                """,
                (entry_id,),
                "immutable",
            )
            _expect_error(
                conn,
                """
                INSERT INTO adventurer.expedition_result_metrics (
                    result_entry_id, metric_code, total_amount
                )
                VALUES (%s, 'distance', 3)
                """,
                (entry_id,),
                "a result snapshot cannot gain metric rows after it is created",
            )
            _expect_error(
                conn,
                """
                INSERT INTO adventurer.expedition_update_metrics (
                    update_id, metric_code, credited_amount
                )
                VALUES (%s, 'distance', 2)
                """,
                (update_id,),
                "an expedition update cannot gain metric rows after it is created",
            )
            _expect_error(
                conn,
                """
                UPDATE adventurer.expedition_result_metrics
                SET total_amount = 11
                WHERE result_entry_id = %s
                """,
                (entry_id,),
                "expedition result metrics are immutable",
            )
            _expect_error(
                conn,
                """
                DELETE FROM adventurer.expedition_update_metrics
                WHERE update_id = %s
                """,
                (update_id,),
                "expedition update metrics are immutable",
            )
            conn.execute("SELECT set_config('adventurer.privacy_removal', 'on', true)")
            conn.execute(
                "SELECT set_config('adventurer.privacy_removal_user_id', %s, true)",
                (str(user_id),),
            )
            conn.execute(
                "DELETE FROM adventurer.expedition_updates WHERE id = %s",
                (update_id,),
            )
            assert conn.execute(
                """
                SELECT count(*) AS n
                FROM adventurer.expedition_update_metrics
                WHERE update_id = %s
                """,
                (update_id,),
            ).fetchone()["n"] == 0
            conn.execute(
                """
                UPDATE adventurer.users
                SET deletion_state = 'deleting'
                WHERE id = %s
                """,
                (user_id,),
            )
            conn.execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
            conn.execute(
                "DELETE FROM adventurer.expedition_result_entries WHERE id = %s",
                (entry_id,),
            )
            assert conn.execute(
                """
                SELECT count(*) AS n
                FROM adventurer.expedition_result_metrics
                WHERE result_entry_id = %s
                """,
                (entry_id,),
            ).fetchone()["n"] == 0
            conn.commit()
    finally:
        with _connect() as conn:
            conn.execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
            conn.execute("SELECT set_config('adventurer.privacy_removal', 'on', true)")
            conn.execute(
                "SELECT set_config('adventurer.privacy_removal_user_id', %s, true)",
                (str(user_id),),
            )
            conn.execute(
                """
                UPDATE adventurer.users
                SET deletion_state = 'deleting'
                WHERE id = %s
                """,
                (user_id,),
            )
            conn.execute(
                "DELETE FROM adventurer.expedition_updates WHERE user_id = %s",
                (user_id,),
            )
            conn.execute(
                "DELETE FROM adventurer.expedition_result_entries WHERE user_id = %s",
                (user_id,),
            )
            conn.execute(
                "DELETE FROM adventurer.expedition_participations WHERE user_id = %s",
                (user_id,),
            )
            conn.execute(
                "DELETE FROM adventurer.route_journeys WHERE user_id = %s",
                (user_id,),
            )
            conn.execute(
                "DELETE FROM adventurer.expeditions WHERE id = %s",
                (expedition_id,),
            )
            conn.execute("DELETE FROM adventurer.routes WHERE id = %s", (route_id,))
            conn.execute("DELETE FROM adventurer.users WHERE id = %s", (user_id,))
            conn.commit()


def _advisory_halves(journey_id: int) -> tuple[int, int]:
    return (journey_id >> 32) & 0xFFFFFFFF, journey_id & 0xFFFFFFFF


def test_advisory_lock_uses_the_full_bigint_journey_id():
    _upgrade()
    low_id = 2147483648
    aliased_id = low_id + 2**32
    min_id = -9223372036854775808
    max_id = 9223372036854775807
    with _connect() as conn:
        sequence = conn.execute(
            """
            SELECT last_value, is_called
            FROM adventurer.route_journeys_id_seq
            """
        ).fetchone()
        route_id = _draft(conn, "lock-range")
        _require(conn, route_id, "elevation", "100")
        checkpoint_id = _checkpoint(conn, route_id, "summit", 0)
        _threshold(conn, checkpoint_id, route_id, "elevation", "100")
        _publish(conn, route_id)
        user_id = _user(conn, "lock-range-user")
        journeys = (
            (min_id, 1),
            (low_id, 2),
            (aliased_id, 3),
            (max_id, 4),
        )
        for journey_id, attempt in journeys:
            conn.execute(
                """
                INSERT INTO adventurer.route_journeys (
                    id, user_id, route_id, status, attempt_number, is_current
                )
                VALUES (%s, %s, %s, 'abandoned', %s, false)
                """,
                (journey_id, user_id, route_id, attempt),
            )
        activities = {
            journey_id: _activity(conn, user_id, "10", None)
            for journey_id, _attempt in journeys
        }
        assignments = {
            journey_id: _assign(conn, activity_id, journey_id, user_id, route_id)
            for journey_id, activity_id in activities.items()
        }
        for journey_id in (min_id, max_id):
            _credit(
                conn, assignments[journey_id], journey_id, route_id, "elevation", "10"
            )
        conn.commit()
    insert = """
        INSERT INTO adventurer.journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (%s, %s, %s, 'elevation', 10)
    """
    release = threading.Event()
    holding = threading.Event()
    holder_state: dict = {}

    def hold() -> None:
        connection = _connect()
        try:
            holder_state["pid"] = connection.execute(
                "SELECT pg_backend_pid() AS pid"
            ).fetchone()["pid"]
            connection.execute(insert, (assignments[low_id], low_id, route_id))
            holding.set()
            if not release.wait(timeout=8):
                raise AssertionError("the held credit lock was not released")
            connection.commit()
            holder_state["result"] = "committed"
        except Exception as exc:
            connection.rollback()
            holder_state["result"] = str(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=hold)
    try:
        thread.start()
        assert holding.wait(timeout=8)
        with _connect() as monitor:
            monitor.autocommit = True
            locks = monitor.execute(
                """
                SELECT classid::bigint AS high_bits,
                       objid::bigint AS low_bits,
                       objsubid
                FROM pg_locks
                WHERE locktype = 'advisory'
                  AND pid = %s
                  AND granted
                  AND objsubid = 1
                """,
                (holder_state["pid"],),
            ).fetchall()
        high_bits, low_bits = _advisory_halves(low_id)
        assert any(
            row["high_bits"] == high_bits and row["low_bits"] == low_bits
            for row in locks
        )
        with _connect() as other:
            other.execute("SET statement_timeout = '3s'")
            other.execute(insert, (assignments[aliased_id], aliased_id, route_id))
            other.commit()
    finally:
        release.set()
        thread.join(timeout=8)
        with _connect() as conn:
            conn.execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
            conn.execute(
                """
                UPDATE adventurer.users
                SET deletion_state = 'deleting'
                WHERE id = %s
                """,
                (user_id,),
            )
            conn.execute(
                "DELETE FROM adventurer.journey_credits WHERE route_id = %s",
                (route_id,),
            )
            conn.execute(
                "DELETE FROM adventurer.route_journeys WHERE user_id = %s",
                (user_id,),
            )
            conn.execute("DELETE FROM adventurer.routes WHERE id = %s", (route_id,))
            conn.execute("DELETE FROM adventurer.users WHERE id = %s", (user_id,))
            conn.execute(
                "SELECT setval('adventurer.route_journeys_id_seq', %s, %s)",
                (sequence["last_value"], sequence["is_called"]),
            )
            conn.commit()
    assert holder_state["result"] == "committed"


def test_credit_moves_between_journeys_lock_lower_id_first():
    _upgrade()
    with _connect() as conn:
        route_id = _draft(conn, "lock-move")
        _require(conn, route_id, "elevation", "100")
        checkpoint_id = _checkpoint(conn, route_id, "summit", 0)
        _threshold(conn, checkpoint_id, route_id, "elevation", "100")
        _publish(conn, route_id)
        user_id = _user(conn, "lock-move-user")
        low_journey = _journey(conn, user_id, route_id)
        high_journey = conn.execute(
            """
            INSERT INTO adventurer.route_journeys (
                user_id, route_id, status, attempt_number, is_current
            )
            VALUES (%s, %s, 'abandoned', 2, false)
            RETURNING id
            """,
            (user_id, route_id),
        ).fetchone()["id"]
        assert low_journey < high_journey
        low_source = _assign(
            conn, _activity(conn, user_id, "50", None), low_journey, user_id, route_id
        )
        low_destination = _assign(
            conn, _activity(conn, user_id, "50", None), low_journey, user_id, route_id
        )
        high_source = _assign(
            conn, _activity(conn, user_id, "50", None), high_journey, user_id, route_id
        )
        high_destination = _assign(
            conn, _activity(conn, user_id, "50", None), high_journey, user_id, route_id
        )
        low_credit = _credit(
            conn, low_source, low_journey, route_id, "elevation", "30"
        )
        high_credit = _credit(
            conn, high_source, high_journey, route_id, "elevation", "50"
        )
        conn.commit()
    move = """
        WITH moved AS (
            UPDATE adventurer.journey_credits AS credit
            SET assignment_id = %s, journey_id = %s
            FROM (
                SELECT id, assignment_id AS old_assignment_id, metric_code
                FROM adventurer.journey_credits
                WHERE id = %s
            ) AS target
            WHERE credit.id = target.id
            RETURNING credit.assignment_id, credit.metric_code, target.old_assignment_id
        )
        INSERT INTO adventurer.journey_credit_origins (
            assignment_id, metric_code, credited_at
        )
        SELECT moved.assignment_id, moved.metric_code, origin.credited_at
        FROM moved
        JOIN adventurer.journey_credit_origins AS origin
          ON origin.assignment_id = moved.old_assignment_id
         AND origin.metric_code = moved.metric_code
        ON CONFLICT (assignment_id, metric_code) DO NOTHING
    """
    try:
        moved = _overlap_writes(
            [
                (move, (high_destination, high_journey, low_credit)),
                (move, (low_destination, low_journey, high_credit)),
            ]
        )
        assert sorted(moved) == ["committed", "committed"]
        with _connect() as conn:
            totals = {
                row["journey_id"]: row["credited_total"]
                for row in conn.execute(
                    """
                    SELECT journey_id, credited_total
                    FROM adventurer.journey_metric_totals
                    WHERE journey_id IN (%s, %s)
                    """,
                    (low_journey, high_journey),
                ).fetchall()
            }
            assert totals[low_journey] == Decimal("50")
            assert totals[high_journey] == Decimal("30")
    finally:
        with _connect() as conn:
            conn.execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
            conn.execute(
                """
                UPDATE adventurer.users
                SET deletion_state = 'deleting'
                WHERE id = %s
                """,
                (user_id,),
            )
            conn.execute(
                "DELETE FROM adventurer.journey_credits WHERE route_id = %s",
                (route_id,),
            )
            conn.execute(
                "DELETE FROM adventurer.route_journeys WHERE user_id = %s",
                (user_id,),
            )
            conn.execute("DELETE FROM adventurer.routes WHERE id = %s", (route_id,))
            conn.execute("DELETE FROM adventurer.users WHERE id = %s", (user_id,))
            conn.commit()
