"""App fixtures against the isolated Phase 1 database.

This module never reads DATABASE_URL. Alembic refuses any host other than
localhost and any database other than adventurer_migration_test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from adventurer import create_app

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = "postgresql://postgres:postgres@127.0.0.1:54329/postgres"
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


def _prepare_database() -> None:
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.fail(f"isolated Postgres is not listening on 127.0.0.1:54329: {exc}")
    exists = admin.execute(
        "SELECT 1 FROM pg_database WHERE datname = 'adventurer_migration_test'"
    ).fetchone()
    if exists is None:
        admin.execute("CREATE DATABASE adventurer_migration_test")
    admin.close()

    ready = False
    try:
        with psycopg.connect(TEST_URL, connect_timeout=3) as conn:
            constraint = conn.execute(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname = 'expedition_invitations_status_chk'
                """
            ).fetchone()
            ready = (
                conn.execute("SELECT to_regclass('adventurer.users') AS name").fetchone()[0]
                is not None
                and conn.execute("SELECT to_regclass('adventurer.routes') AS name").fetchone()[0]
                is not None
                and constraint is not None
                and "expired" in constraint[0]
            )
    except psycopg.Error:
        ready = False
    if not ready:
        _upgrade()


def _wipe(application) -> None:
    with application.app_context():
        from adventurer.db import get_db

        database = get_db()
        database.execute("DELETE FROM expedition_invitations")
        database.execute(
            "UPDATE expeditions SET owner_user_id = NULL WHERE owner_user_id IS NOT NULL"
        )
        database.execute("DELETE FROM users")
        database.execute("DELETE FROM expeditions WHERE slug <> 'everest'")
        database.execute("DELETE FROM routes WHERE slug <> 'everest'")
        database.execute(
            """
            UPDATE expeditions
            SET name = 'Global Everest Challenge',
                description = 'The public Everest challenge. Everyone who joins shares one board.',
                visibility = 'public',
                status = 'active',
                owner_user_id = NULL,
                starts_at = NULL,
                ends_at = NULL
            WHERE slug = 'everest'
            """
        )
        database.execute(
            """
            UPDATE routes
            SET status = 'published'
            WHERE slug = 'everest'
            """
        )
        database.commit()


@pytest.fixture()
def app():
    _prepare_database()
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE_URL": TEST_URL,
            "INVITE_CODE": "",
        }
    )
    _wipe(application)
    yield application
    _wipe(application)


@pytest.fixture()
def client(app):
    return app.test_client()
