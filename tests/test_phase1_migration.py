"""Phase 1 migration, validation, and rollback against an isolated Postgres.

This module never reads DATABASE_URL. Alembic refuses any host other than
localhost and any database other than adventurer_migration_test.
"""

from __future__ import annotations

import os
import subprocess
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = "postgresql://postgres:postgres@127.0.0.1:54329/postgres"
TEST_URL = "postgresql://postgres:postgres@127.0.0.1:54329/adventurer_migration_test"
FOOT = Decimal("0.3048")


def _alembic(*args: str, url: str | None = TEST_URL) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    if url is None:
        env.pop("TEST_DATABASE_URL", None)
    else:
        env["TEST_DATABASE_URL"] = url
    return subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _ok(*args: str) -> None:
    result = _alembic(*args)
    if result.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed\n{result.stdout}\n{result.stderr}"
        )


def _connect():
    return psycopg.connect(TEST_URL, row_factory=dict_row)


def _legacy_snapshot(conn) -> list[tuple]:
    accounts = conn.execute(
        """
        SELECT id, name, name_key, password_hash, created_at
        FROM adventurer.adventurers
        ORDER BY id
        """
    ).fetchall()
    logs = conn.execute(
        """
        SELECT id, adventurer_id, challenge_slug, elevation_ft, distance_km,
               happened_on, note, created_at
        FROM adventurer.logs
        ORDER BY id
        """
    ).fetchall()
    return [("account", tuple(row.values())) for row in accounts] + [
        ("log", tuple(row.values())) for row in logs
    ]


def _expect_error(conn, sql: str, params: tuple = ()) -> None:
    conn.execute("SAVEPOINT probe")
    try:
        conn.execute(sql, params)
    except psycopg.Error:
        conn.execute("ROLLBACK TO SAVEPOINT probe")
        return
    conn.execute("ROLLBACK TO SAVEPOINT probe")
    raise AssertionError("statement should have failed: " + sql)


def test_alembic_refuses_anything_but_the_isolated_database():
    missing = _alembic("current", url=None)
    assert missing.returncode != 0
    assert "TEST_DATABASE_URL" in missing.stderr

    hosted = _alembic(
        "current",
        url="postgresql://postgres:postgres@db.supabase.co:5432/adventurer_migration_test",
    )
    assert hosted.returncode != 0
    assert "hosted" in hosted.stderr.lower() or "Refusing" in hosted.stderr

    wrong_name = _alembic(
        "current",
        url="postgresql://postgres:postgres@127.0.0.1:54329/postgres",
    )
    assert wrong_name.returncode != 0
    assert "adventurer_migration_test" in wrong_name.stderr


def test_phase1_migration_rollback_and_validation():
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.fail(f"isolated Postgres is not listening on 127.0.0.1:54329: {exc}")
    admin.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                CREATE ROLE anon NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                CREATE ROLE authenticated NOLOGIN;
            END IF;
        END
        $$
        """
    )
    admin.execute(
        """
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE datname = 'adventurer_migration_test' AND pid <> pg_backend_pid()
        """
    )
    admin.execute("DROP DATABASE IF EXISTS adventurer_migration_test")
    admin.execute("CREATE DATABASE adventurer_migration_test")
    admin.close()

    _ok("upgrade", "head")
    with _connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM adventurer.users").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM adventurer.competition_seasons").fetchone()["n"] == 0
        stops = conn.execute(
            """
            SELECT checkpoint.slug, checkpoint.position, checkpoint.elevation_m,
                   expedition.target_elevation_m
            FROM adventurer.expedition_checkpoints AS checkpoint
            JOIN adventurer.expeditions AS expedition
                ON expedition.id = checkpoint.expedition_id
            WHERE expedition.slug = 'everest'
            ORDER BY checkpoint.position
            """
        ).fetchall()
        assert len(stops) == 16
        assert stops[0]["slug"] == "timms-hill"
        assert stops[0]["elevation_m"] == (Decimal("1951.5") * FOOT).quantize(Decimal("0.00001"))
        assert stops[-1]["slug"] == "summit"
        assert stops[-1]["elevation_m"] == stops[-1]["target_elevation_m"]
        sequence = conn.execute("SELECT last_value, is_called FROM adventurer.users_id_seq").fetchone()
        assert sequence["is_called"] is False
        conn.execute("SELECT adventurer.assert_phase1_legacy_match()")
        conn.commit()

    _ok("downgrade", "0001_legacy_baseline")
    with _connect() as conn:
        assert conn.execute("SELECT to_regclass('adventurer.users') AS name").fetchone()["name"] is None
        assert conn.execute("SELECT COUNT(*) AS n FROM adventurer.adventurers").fetchone()["n"] == 0
        conn.commit()

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO adventurer.adventurers (id, name, name_key, password_hash, created_at)
            VALUES
                (3, 'Sam', 'sam', 'hash-sam', '2024-01-15T12:00:00+00:00'),
                (8, 'Alex', 'alex', 'hash-alex', '2024-03-01T08:30:00+00:00'),
                (9, 'Rae', 'rae', 'hash-rae', '2024-04-04T04:04:04+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO adventurer.logs (
                id, adventurer_id, challenge_slug, elevation_ft, distance_km,
                happened_on, note, created_at
            )
            VALUES
                (4, 3, 'everest', %s, 5.5, '2024-02-01', 'O''Brien ridge',
                 '2024-02-01T18:00:00+00:00'),
                (12, 3, 'everest', %s, NULL, '2024-02-02', '',
                 '2024-02-02T18:00:00+00:00'),
                (15, 8, 'everest', %s, 12.25, '2024-05-01', 'long day',
                 '2024-05-01T18:00:00+00:00'),
                (16, 8, 'everest', %s, 0.001, '2024-05-02', 'short',
                 '2024-05-02T18:00:00+00:00')
            """,
            (1951.5, 10.1, 7000.0, 0.1),
        )
        conn.commit()
        original = _legacy_snapshot(conn)

    _ok("upgrade", "head")
    with _connect() as conn:
        conn.execute("SELECT adventurer.assert_phase1_legacy_match()")
        assert _legacy_snapshot(conn) == original
        assert conn.execute("SELECT COUNT(*) AS n FROM adventurer.users").fetchone()["n"] == 3
        assert conn.execute("SELECT COUNT(*) AS n FROM adventurer.activities").fetchone()["n"] == 4
        assert conn.execute("SELECT COUNT(*) AS n FROM adventurer.activity_credits").fetchone()["n"] == 4
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM adventurer.competition_seasons"
        ).fetchone()["n"] == 0
        totals = conn.execute(
            """
            SELECT credit.user_id,
                   SUM(legacy_log.elevation_ft::numeric) AS legacy_ft,
                   SUM(credit.credited_elevation_m) / 0.3048 AS new_ft
            FROM adventurer.activity_credits AS credit
            JOIN adventurer.logs AS legacy_log ON legacy_log.id = credit.activity_id
            GROUP BY credit.user_id
            ORDER BY credit.user_id
            """
        ).fetchall()
        assert [(row["user_id"], row["legacy_ft"]) for row in totals] == [
            (3, Decimal("1961.6")),
            (8, Decimal("7000.1")),
        ]
        for row in totals:
            assert abs(row["legacy_ft"] - row["new_ft"]) < Decimal("0.0001")

        users_seq = conn.execute(
            "SELECT last_value, is_called FROM adventurer.users_id_seq"
        ).fetchone()
        activities_seq = conn.execute(
            "SELECT last_value, is_called FROM adventurer.activities_id_seq"
        ).fetchone()
        assert (users_seq["last_value"], users_seq["is_called"]) == (9, True)
        assert (activities_seq["last_value"], activities_seq["is_called"]) == (16, True)

        columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'adventurer' AND table_name = 'activity_credits'
                """
            ).fetchall()
        }
        assert "competition_eligibility" not in columns
        assert "eligibility_reason" not in columns
        source = conn.execute(
            """
            SELECT is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'adventurer' AND table_name = 'activities'
              AND column_name = 'source'
            """
        ).fetchone()
        activity_type = conn.execute(
            """
            SELECT is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'adventurer' AND table_name = 'activities'
              AND column_name = 'activity_type'
            """
        ).fetchone()
        assert source["is_nullable"] == "NO" and "manual" in source["column_default"]
        assert activity_type["is_nullable"] == "NO"
        assert "unspecified" in activity_type["column_default"]
        assert conn.execute(
            "SELECT has_table_privilege('anon', 'adventurer.users', 'SELECT') AS allowed"
        ).fetchone()["allowed"] is False
        assert conn.execute(
            """
            SELECT relrowsecurity
            FROM pg_class
            WHERE oid = 'adventurer.activities'::regclass
            """
        ).fetchone()["relrowsecurity"] is True

        everest_id = conn.execute(
            "SELECT id FROM adventurer.expeditions WHERE slug = 'everest'"
        ).fetchone()["id"]
        alex_participation = conn.execute(
            """
            SELECT id FROM adventurer.expedition_participations
            WHERE user_id = 8 AND expedition_id = %s
            """,
            (everest_id,),
        ).fetchone()["id"]

        _expect_error(
            conn,
            """
            INSERT INTO adventurer.expedition_checkpoints (
                expedition_id, slug, name, position, elevation_m
            )
            VALUES (%s, 'negative', 'Negative', -1, 10)
            """,
            (everest_id,),
        )
        _expect_error(
            conn,
            """
            INSERT INTO adventurer.expedition_checkpoints (
                expedition_id, slug, name, position, elevation_m
            )
            VALUES (%s, 'another-start', 'Another', 0, 10)
            """,
            (everest_id,),
        )
        _expect_error(
            conn,
            """
            INSERT INTO adventurer.activities (
                user_id, activity_date, elevation_gain_m, verification_status
            )
            VALUES (3, DATE '2024-01-01', 10, 'verified')
            """,
        )
        _expect_error(
            conn,
            """
            INSERT INTO adventurer.activities (
                user_id, activity_date, elevation_gain_m, source, verification_status
            )
            VALUES (3, DATE '2024-01-01', 10, 'garmin', 'not_required')
            """,
        )
        _expect_error(
            conn,
            """
            INSERT INTO adventurer.activities (
                user_id, activity_date, elevation_gain_m, source, verification_status
            )
            VALUES (3, DATE '2024-01-01', 10, 'garmin', 'pending')
            """,
        )
        _expect_error(
            conn,
            """
            INSERT INTO adventurer.activities (
                user_id, activity_date, elevation_gain_m, started_at
            )
            VALUES (3, DATE '2024-01-01', 10, '2024-01-01T00:00:00Z')
            """,
        )
        _expect_error(
            conn,
            """
            INSERT INTO adventurer.activities (
                user_id, activity_date, elevation_gain_m, started_at, ended_at
            )
            VALUES (
                3, DATE '2024-01-01', 10,
                '2024-01-01T02:00:00Z', '2024-01-01T01:00:00Z'
            )
            """,
        )
        _expect_error(
            conn,
            """
            UPDATE adventurer.activity_verification_events
            SET reason = 'changed'
            WHERE activity_id = 4
            """,
        )
        _expect_error(
            conn,
            "DELETE FROM adventurer.activity_verification_events WHERE activity_id = 4",
        )
        _expect_error(
            conn,
            """
            INSERT INTO adventurer.activity_credits (
                activity_id, participation_id, user_id, credited_elevation_m
            )
            VALUES (4, %s, 3, 10)
            """,
            (alex_participation,),
        )

        conn.execute("SAVEPOINT keep")
        conn.execute(
            """
            INSERT INTO adventurer.expeditions (slug, name, target_elevation_m, status)
            VALUES ('kilimanjaro', 'Kilimanjaro', 5895, 'draft')
            """
        )
        conn.execute(
            """
            INSERT INTO adventurer.expedition_participations (user_id, expedition_id)
            SELECT 3, id FROM adventurer.expeditions WHERE slug = 'kilimanjaro'
            """
        )
        conn.execute(
            """
            INSERT INTO adventurer.activity_credits (
                activity_id, participation_id, user_id, credited_elevation_m
            )
            SELECT 4, participation.id, 3, activity.elevation_gain_m
            FROM adventurer.activities AS activity
            JOIN adventurer.expedition_participations AS participation
                ON participation.user_id = 3
            JOIN adventurer.expeditions AS expedition
                ON expedition.id = participation.expedition_id
            WHERE activity.id = 4 AND expedition.slug = 'kilimanjaro'
            """
        )
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM adventurer.activity_credits WHERE activity_id = 4"
        ).fetchone()["n"] == 2
        conn.execute("ROLLBACK TO SAVEPOINT keep")

        conn.execute("SAVEPOINT keep")
        deleted = conn.execute("DELETE FROM adventurer.activities WHERE id = 4")
        assert deleted.rowcount == 1
        assert conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM adventurer.activity_verification_events
            WHERE activity_id = 4
            """
        ).fetchone()["n"] == 0
        conn.execute("ROLLBACK TO SAVEPOINT keep")

        conn.execute("SAVEPOINT keep")
        try:
            conn.execute(
                """
                INSERT INTO adventurer.activities (
                    user_id, activity_date, elevation_gain_m, source,
                    external_id, verification_status
                )
                VALUES (3, DATE '2024-06-01', 20, 'garmin', 'fit-1', 'pending')
                """
            )
            conn.execute(
                """
                INSERT INTO adventurer.activity_verification_events (
                    activity_id, from_status, to_status, reason, actor_type, actor_user_id
                )
                SELECT id, NULL, 'pending', 'Imported from Garmin.', 'provider', NULL
                FROM adventurer.activities
                WHERE external_id = 'fit-1'
                """
            )
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
            conn.execute("SELECT adventurer.reverse_sync_new_rows_to_legacy()")
        except psycopg.Error as exc:
            conn.execute("ROLLBACK TO SAVEPOINT keep")
            assert "cannot store" in str(exc)
        else:
            conn.execute("ROLLBACK TO SAVEPOINT keep")
            raise AssertionError("reverse sync should refuse a provider activity")

        assert _legacy_snapshot(conn) == original
        conn.commit()

    _ok("downgrade", "0001_legacy_baseline")
    with _connect() as conn:
        assert _legacy_snapshot(conn) == original
        assert conn.execute("SELECT to_regclass('adventurer.activities') AS name").fetchone()["name"] is None
        conn.commit()
        conn.execute(
            """
            INSERT INTO adventurer.logs (
                adventurer_id, challenge_slug, elevation_ft, happened_on, note, created_at
            )
            VALUES (3, 'kilimanjaro', 100, '2024-07-01', 'not seeded',
                    '2024-07-01T00:00:00+00:00')
            """
        )
        conn.commit()

    failed = _alembic("upgrade", "head")
    assert failed.returncode != 0
    assert "unmapped challenge_slug" in (failed.stdout + failed.stderr)
    assert "kilimanjaro" in (failed.stdout + failed.stderr)
    with _connect() as conn:
        version = conn.execute(
            "SELECT version_num FROM adventurer.alembic_version"
        ).fetchone()["version_num"]
        assert version == "0003_seed_everest"
        assert conn.execute("SELECT COUNT(*) AS n FROM adventurer.users").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM adventurer.logs WHERE challenge_slug = 'kilimanjaro'"
        ).fetchone()["n"] == 1
        conn.execute("DELETE FROM adventurer.logs WHERE challenge_slug = 'kilimanjaro'")
        conn.commit()

    _ok("upgrade", "head")
    with _connect() as conn:
        conn.execute("SELECT adventurer.assert_phase1_legacy_match()")
        everest_id = conn.execute(
            "SELECT id FROM adventurer.expeditions WHERE slug = 'everest'"
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO adventurer.users (username, username_key, password_hash)
            VALUES ('Jo', 'jo', 'hash-jo')
            """
        )
        user_id = conn.execute(
            "SELECT id FROM adventurer.users WHERE username_key = 'jo'"
        ).fetchone()["id"]
        assert user_id == 10
        conn.execute(
            """
            INSERT INTO adventurer.user_profiles (user_id, display_name)
            VALUES (%s, 'Jo')
            """,
            (user_id,),
        )
        conn.execute(
            """
            INSERT INTO adventurer.expedition_participations (user_id, expedition_id)
            VALUES (%s, %s)
            """,
            (user_id, everest_id),
        )
        conn.execute(
            """
            INSERT INTO adventurer.activities (
                user_id, activity_date, elevation_gain_m, note
            )
            VALUES (%s, DATE '2026-11-21', 30.48, 'after cutover')
            """,
            (user_id,),
        )
        activity_id = conn.execute(
            "SELECT id FROM adventurer.activities WHERE note = 'after cutover'"
        ).fetchone()["id"]
        assert activity_id == 17
        conn.execute(
            """
            INSERT INTO adventurer.activity_verification_events (
                activity_id, from_status, to_status, reason, actor_type
            )
            VALUES (
                %s, NULL, 'not_required',
                'Logged by hand. Not independently verified.', 'system'
            )
            """,
            (activity_id,),
        )
        conn.execute(
            """
            INSERT INTO adventurer.activity_credits (
                activity_id, participation_id, user_id, credited_elevation_m
            )
            SELECT %s, participation.id, %s, 30.48
            FROM adventurer.expedition_participations AS participation
            WHERE participation.user_id = %s AND participation.expedition_id = %s
            """,
            (activity_id, user_id, user_id, everest_id),
        )
        conn.execute("SELECT adventurer.reverse_sync_new_rows_to_legacy()")
        conn.commit()
        synced = conn.execute(
            """
            SELECT name, challenge_slug, elevation_ft, note
            FROM adventurer.adventurers AS account
            JOIN adventurer.logs AS legacy_log ON legacy_log.adventurer_id = account.id
            WHERE account.name = 'Jo'
            """
        ).fetchone()
        assert synced["challenge_slug"] == "everest"
        assert synced["note"] == "after cutover"
        assert synced["elevation_ft"] == pytest.approx(100.0)
        assert _rows_include(conn, original)

    _ok("downgrade", "0001_legacy_baseline")
    with _connect() as conn:
        assert conn.execute("SELECT to_regclass('adventurer.users') AS name").fetchone()["name"] is None
        assert conn.execute("SELECT to_regclass('adventurer.expeditions') AS name").fetchone()["name"] is None
        jo = conn.execute(
            """
            SELECT legacy_log.elevation_ft, legacy_log.note, legacy_log.challenge_slug
            FROM adventurer.logs AS legacy_log
            JOIN adventurer.adventurers AS account ON account.id = legacy_log.adventurer_id
            WHERE account.name = 'Jo'
            """
        ).fetchone()
        assert jo["challenge_slug"] == "everest"
        assert jo["note"] == "after cutover"
        assert jo["elevation_ft"] == pytest.approx(100.0)
        kept = conn.execute(
            "SELECT id FROM adventurer.adventurers ORDER BY id"
        ).fetchall()
        assert [row["id"] for row in kept] == [3, 8, 9, 10]
        conn.commit()


def _rows_include(conn, original: list[tuple]) -> bool:
    current = _legacy_snapshot(conn)
    return all(row in current for row in original)
