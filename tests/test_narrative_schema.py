"""Isolated-database checks for the narrative catalog in migration 0010.

Uses postgresql://postgres:postgres@127.0.0.1:54329/adventurer_migration_test
only. Never reads DATABASE_URL and never contacts a hosted database.
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

_SPANS = """
    adventurer.route_chapter_spans,
    adventurer.route_chapter_spans_chapters
"""

_SHAPE = """
    adventurer.route_progress_shape_routes,
    adventurer.route_progress_shape_requirements,
    adventurer.route_progress_shape_checkpoints,
    adventurer.route_progress_shape_checkpoint_requirements
"""


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
        _check_spans(conn)
    except psycopg.Error as exc:
        conn.execute("ROLLBACK TO SAVEPOINT probe")
        if message is not None and message not in str(exc):
            raise AssertionError(f"expected {message!r} in {exc}") from exc
        return
    conn.execute("ROLLBACK TO SAVEPOINT probe")
    raise AssertionError("statement should have failed: " + sql)


def _check_shape(conn) -> None:
    conn.execute(f"SET CONSTRAINTS {_SHAPE} IMMEDIATE")
    conn.execute(f"SET CONSTRAINTS {_SHAPE} DEFERRED")


def _check_spans(conn) -> None:
    conn.execute(f"SET CONSTRAINTS {_SPANS} IMMEDIATE")
    conn.execute(f"SET CONSTRAINTS {_SPANS} DEFERRED")


def _columns(conn, table: str) -> set[str]:
    return {
        row["column_name"]
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'adventurer' AND table_name = %s
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


def _route(conn, slug: str) -> tuple[int, list[int]]:
    route_id = conn.execute(
        """
        INSERT INTO adventurer.routes (slug, name, region, summary, status)
        VALUES (%s, %s, 'Test', 'A test route', 'draft')
        RETURNING id
        """,
        (slug, slug),
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO adventurer.route_requirements (route_id, metric_code, target_amount)
        VALUES (%s, 'elevation', 304.8)
        """,
        (route_id,),
    )
    stops = []
    for position, threshold in ((0, "100"), (1, "200"), (2, "304.8")):
        checkpoint_id = conn.execute(
            """
            INSERT INTO adventurer.route_checkpoints (route_id, slug, name, position)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (route_id, f"{slug}-{position}", f"{slug}-{position}", position),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO adventurer.checkpoint_requirements (
                checkpoint_id, route_id, metric_code, threshold_amount
            )
            VALUES (%s, %s, 'elevation', %s)
            """,
            (checkpoint_id, route_id, threshold),
        )
        stops.append(checkpoint_id)
    _check_shape(conn)
    return route_id, stops


def _chapter(conn, route_id: int, slug: str, position: int, title: str = "Approach") -> int:
    return conn.execute(
        """
        INSERT INTO adventurer.route_chapters (route_id, slug, title, position, status)
        VALUES (%s, %s, %s, %s, 'published')
        RETURNING id
        """,
        (route_id, slug, title, position),
    ).fetchone()["id"]


def _assign(conn, checkpoint_id: int, chapter_id: int) -> None:
    conn.execute(
        """
        UPDATE adventurer.route_checkpoints
        SET chapter_id = %s
        WHERE id = %s
        """,
        (chapter_id, checkpoint_id),
    )


def _story(conn, checkpoint_id: int, route_id: int, **extra) -> int:
    fields = {
        "arrival_title": "Arrival",
        "arrival_body": "A short reveal.",
        "journal_title": None,
        "journal_body": None,
        "status": "published",
    }
    fields.update(extra)
    return conn.execute(
        """
        INSERT INTO adventurer.checkpoint_stories (
            checkpoint_id, route_id, arrival_title, arrival_body,
            journal_title, journal_body, status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            checkpoint_id,
            route_id,
            fields["arrival_title"],
            fields["arrival_body"],
            fields["journal_title"],
            fields["journal_body"],
            fields["status"],
        ),
    ).fetchone()["id"]


def _publish(conn, route_id: int) -> None:
    conn.execute(
        "UPDATE adventurer.routes SET status = 'published' WHERE id = %s",
        (route_id,),
    )
    _check_shape(conn)


def _journey(conn, user_id: int, route_id: int, *, attempt: int = 1, status: str = "active", current: bool = True) -> int:
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


def _credit(conn, user_id: int, journey_id: int, route_id: int) -> None:
    activity_id = conn.execute(
        """
        INSERT INTO adventurer.activities (
            user_id, activity_date, elevation_gain_m, note
        )
        VALUES (%s, DATE '2026-01-01', 30.48, 'note')
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
    assignment_id = conn.execute(
        """
        INSERT INTO adventurer.activity_journey_assignments (
            activity_id, journey_id, user_id, route_id
        )
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (activity_id, journey_id, user_id, route_id),
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO adventurer.journey_credits (
            assignment_id, journey_id, route_id, metric_code, credited_amount
        )
        VALUES (%s, %s, %s, 'elevation', 30.48)
        """,
        (assignment_id, journey_id, route_id),
    )


def _total(conn, journey_id: int):
    row = conn.execute(
        """
        SELECT credited_total
        FROM adventurer.journey_metric_totals
        WHERE journey_id = %s AND metric_code = 'elevation'
        """,
        (journey_id,),
    ).fetchone()
    return None if row is None else row["credited_total"]


def _media(conn, route_id: int, **extra) -> int:
    fields = {
        "checkpoint_id": None,
        "discovery_id": None,
        "role": "route_hero",
        "asset_path": "/static/photos/hero.jpg",
        "alt_text": "The approach",
        "rights_basis": "undocumented",
        "author": None,
        "license": None,
        "license_url": None,
        "source_url": None,
        "permission_note": None,
        "position": 0,
        "status": "draft",
    }
    fields.update(extra)
    return conn.execute(
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, discovery_id, role, asset_path, alt_text,
            rights_basis, author, license, license_url, source_url, permission_note,
            position, status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            route_id,
            fields["checkpoint_id"],
            fields["discovery_id"],
            fields["role"],
            fields["asset_path"],
            fields["alt_text"],
            fields["rights_basis"],
            fields["author"],
            fields["license"],
            fields["license_url"],
            fields["source_url"],
            fields["permission_note"],
            fields["position"],
            fields["status"],
        ),
    ).fetchone()["id"]


def test_migration_adds_the_narrative_catalog_and_leaves_everest(db):
    version = db.execute("SELECT version_num FROM adventurer.alembic_version").fetchone()
    assert version["version_num"] == "0010_narrative_content"
    for table in (
        "route_chapters",
        "checkpoint_stories",
        "story_discoveries",
        "route_media",
        "journey_story_views",
    ):
        assert db.execute(
            "SELECT to_regclass(%s) AS name",
            (f"adventurer.{table}",),
        ).fetchone()["name"] == f"adventurer.{table}"
        locked = db.execute(
            """
            SELECT relrowsecurity
            FROM pg_class
            JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
            WHERE nspname = 'adventurer' AND relname = %s
            """,
            (table,),
        ).fetchone()
        assert locked["relrowsecurity"] is True
    assert "chapter_id" in _columns(db, "route_checkpoints")
    assert db.execute(
        """
        SELECT is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'adventurer'
          AND table_name = 'route_checkpoints'
          AND column_name = 'chapter_id'
        """
    ).fetchone()["is_nullable"] == "YES"
    everest = db.execute(
        """
        SELECT COUNT(*) AS n
        FROM adventurer.route_checkpoints AS checkpoint
        JOIN adventurer.routes AS route ON route.id = checkpoint.route_id
        WHERE route.slug = 'everest' AND checkpoint.chapter_id IS NULL
        """
    ).fetchone()["n"]
    assert everest >= 1
    for table in (
        "route_chapters",
        "checkpoint_stories",
        "story_discoveries",
        "route_media",
        "journey_story_views",
    ):
        assert db.execute(f"SELECT COUNT(*) AS n FROM adventurer.{table}").fetchone()["n"] == 0
    names = {
        row["conname"]
        for row in db.execute(
            """
            SELECT conname
            FROM pg_constraint
            JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
            JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
            WHERE nspname = 'adventurer'
              AND conname IN (
                'route_chapters_route_slug_key',
                'route_chapters_route_position_key',
                'route_checkpoints_chapter_route_fkey',
                'checkpoint_stories_checkpoint_key',
                'story_discoveries_checkpoint_slug_key',
                'story_discoveries_checkpoint_position_key',
                'route_media_publish_rights',
                'journey_story_views_journey_checkpoint_key',
                'journey_story_views_journey_fkey'
              )
            """
        ).fetchall()
    }
    assert "route_media_publish_rights" in names
    assert "route_checkpoints_chapter_route_fkey" in names
    assert len(names) == 9


def test_valid_chapter_span_and_cross_route_rejection(db):
    route_id, stops = _route(db, "narrative-span")
    other_id, _other_stops = _route(db, "narrative-other")
    first = _chapter(db, route_id, "approach", 0)
    second = _chapter(db, route_id, "ridge", 1)
    _assign(db, stops[0], first)
    _assign(db, stops[1], first)
    _assign(db, stops[2], second)
    _check_spans(db)
    _expect_error(
        db,
        """
        UPDATE adventurer.route_checkpoints
        SET chapter_id = CASE id
            WHEN %s THEN %s
            WHEN %s THEN %s
            ELSE chapter_id
        END
        WHERE id IN (%s, %s)
        """,
        (stops[0], second, stops[2], first, stops[0], stops[2]),
        "chapter order must follow checkpoint position",
    )
    foreign = _chapter(db, other_id, "foreign", 0)
    _expect_error(
        db,
        """
        UPDATE adventurer.route_checkpoints
        SET chapter_id = %s
        WHERE id = %s
        """,
        (foreign, stops[0]),
        "route_checkpoints_chapter_route_fkey",
    )


def test_duplicate_chapter_identity_and_broken_spans_fail(db):
    route_id, stops = _route(db, "narrative-dup")
    _chapter(db, route_id, "approach", 0)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_chapters (route_id, slug, title, position)
        VALUES (%s, 'approach', 'Again', 1)
        """,
        (route_id,),
        "route_chapters_route_slug_key",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_chapters (route_id, slug, title, position)
        VALUES (%s, 'other', 'Other', 0)
        """,
        (route_id,),
        "route_chapters_route_position_key",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_chapters (route_id, slug, title, position)
        VALUES (%s, 'blank', ' ', 2)
        """,
        (route_id,),
        "route_chapters_title_chk",
    )
    chapter_id = _chapter(db, route_id, "ends", 1)
    _assign(db, stops[0], chapter_id)
    _expect_error(
        db,
        """
        UPDATE adventurer.route_checkpoints
        SET chapter_id = %s
        WHERE id = %s
        """,
        (chapter_id, stops[2]),
        "chapter checkpoints must form a contiguous span",
    )


def test_deleting_a_chapter_clears_only_its_assignment(db):
    route_id, stops = _route(db, "narrative-delete")
    _publish(db, route_id)
    user_id = _user(db, "NDelete")
    journey_id = _journey(db, user_id, route_id)
    _credit(db, user_id, journey_id, route_id)
    before = _total(db, journey_id)
    version = db.execute(
        "SELECT progress_version FROM adventurer.route_journeys WHERE id = %s",
        (journey_id,),
    ).fetchone()["progress_version"]
    requirements = db.execute(
        """
        SELECT COUNT(*) AS n
        FROM adventurer.checkpoint_requirements
        WHERE route_id = %s
        """,
        (route_id,),
    ).fetchone()["n"]
    chapter_id = _chapter(db, route_id, "approach", 0)
    later_id = _chapter(db, route_id, "ridge", 1)
    _assign(db, stops[0], chapter_id)
    _assign(db, stops[1], chapter_id)
    _check_spans(db)
    _assign(db, stops[0], later_id)
    _assign(db, stops[1], later_id)
    _check_spans(db)
    assert _total(db, journey_id) == before
    story_id = _story(db, stops[0], route_id)
    _media(
        db,
        route_id,
        checkpoint_id=stops[0],
        role="checkpoint_scene",
        status="draft",
    )
    _check_spans(db)
    _expect_error(
        db,
        """
        UPDATE adventurer.checkpoint_requirements
        SET threshold_amount = 50
        WHERE checkpoint_id = %s
        """,
        (stops[0],),
        "route requirements are immutable once a journey exists",
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.route_checkpoints
        SET position = 4
        WHERE id = %s
        """,
        (stops[2],),
        "route requirements are immutable once a journey exists",
    )
    db.execute("DELETE FROM adventurer.route_chapters WHERE id = %s", (later_id,))
    _check_spans(db)
    _check_shape(db)
    assigned = db.execute(
        """
        SELECT id, route_id, position, chapter_id
        FROM adventurer.route_checkpoints
        WHERE route_id = %s
        ORDER BY position
        """,
        (route_id,),
    ).fetchall()
    assert [row["chapter_id"] for row in assigned] == [None, None, None]
    assert [row["route_id"] for row in assigned] == [route_id, route_id, route_id]
    assert [row["position"] for row in assigned] == [0, 1, 2]
    assert db.execute(
        "SELECT COUNT(*) AS n FROM adventurer.checkpoint_stories WHERE id = %s",
        (story_id,),
    ).fetchone()["n"] == 1
    assert db.execute(
        "SELECT COUNT(*) AS n FROM adventurer.route_media WHERE route_id = %s",
        (route_id,),
    ).fetchone()["n"] == 1
    assert db.execute(
        "SELECT COUNT(*) AS n FROM adventurer.checkpoint_requirements WHERE route_id = %s",
        (route_id,),
    ).fetchone()["n"] == requirements
    assert _total(db, journey_id) == before
    assert db.execute(
        "SELECT progress_version FROM adventurer.route_journeys WHERE id = %s",
        (journey_id,),
    ).fetchone()["progress_version"] == version


def test_story_and_discovery_constraints(db):
    route_id, stops = _route(db, "narrative-story")
    other_id, other_stops = _route(db, "narrative-story-other")
    _story(db, stops[0], route_id, journal_title="Below", journal_body="The longer entry.")
    _expect_error(
        db,
        """
        INSERT INTO adventurer.checkpoint_stories (
            checkpoint_id, route_id, arrival_title, arrival_body
        )
        VALUES (%s, %s, 'Again', 'Again')
        """,
        (stops[0], route_id),
        "checkpoint_stories_checkpoint_key",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.checkpoint_stories (
            checkpoint_id, route_id, arrival_title, arrival_body
        )
        VALUES (%s, %s, 'Wrong', 'Route')
        """,
        (stops[1], other_id),
        "checkpoint_stories_checkpoint_route_fkey",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.checkpoint_stories (
            checkpoint_id, route_id, arrival_title, arrival_body, status
        )
        VALUES (%s, %s, 'Bad', 'Status', 'secret')
        """,
        (stops[1], route_id),
        "checkpoint_stories_status_chk",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.checkpoint_stories (
            checkpoint_id, route_id, arrival_title, arrival_body, journal_title
        )
        VALUES (%s, %s, 'Half', 'Pair', 'Title only')
        """,
        (stops[1], route_id),
        "checkpoint_stories_journal_pair_chk",
    )
    db.execute(
        """
        INSERT INTO adventurer.story_discoveries (
            checkpoint_id, route_id, slug, kind, title, body, position
        )
        VALUES (%s, %s, 'stone', 'historical', 'The gate', 'A note.', 0)
        """,
        (stops[0], route_id),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.story_discoveries (
            checkpoint_id, route_id, slug, kind, title, body, position
        )
        VALUES (%s, %s, 'stone', 'cultural', 'Again', 'Again.', 1)
        """,
        (stops[0], route_id),
        "story_discoveries_checkpoint_slug_key",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.story_discoveries (
            checkpoint_id, route_id, slug, kind, title, body, position
        )
        VALUES (%s, %s, 'next', 'cultural', 'Next', 'Next.', 0)
        """,
        (stops[0], route_id),
        "story_discoveries_checkpoint_position_key",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.story_discoveries (
            checkpoint_id, route_id, slug, kind, title, body, position
        )
        VALUES (%s, %s, 'food', 'recipe', 'Soup', 'Soup.', 1)
        """,
        (stops[0], route_id),
        "story_discoveries_kind_chk",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.story_discoveries (
            checkpoint_id, route_id, slug, kind, title, body, position
        )
        VALUES (%s, %s, 'away', 'geographic', 'Away', 'Away.', 0)
        """,
        (other_stops[0], route_id),
        "story_discoveries_checkpoint_route_fkey",
    )


def test_media_roles_ordering_and_publication_rights(db):
    route_id, stops = _route(db, "narrative-media")
    discovery_id = db.execute(
        """
        INSERT INTO adventurer.story_discoveries (
            checkpoint_id, route_id, slug, kind, title, body, position
        )
        VALUES (%s, %s, 'photo', 'photographic', 'Picture', 'A picture.', 0)
        RETURNING id
        """,
        (stops[0], route_id),
    ).fetchone()["id"]
    _media(db, route_id)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, role, asset_path, alt_text
        )
        VALUES (%s, %s, 'route_hero', '/static/photos/no.jpg', 'No')
        """,
        (route_id, stops[0]),
        "route_media_hero_chk",
    )
    _media(
        db,
        route_id,
        checkpoint_id=stops[0],
        role="checkpoint_scene",
        asset_path="/static/photos/one.jpg",
        alt_text="The first stop",
    )
    documented = {
        "rights_basis": "creative_commons",
        "author": "Ada",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0",
        "source_url": "https://example.com/one",
        "status": "published",
    }
    _media(db, route_id, asset_path="/static/photos/hero-cc.jpg", **documented)
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, role, asset_path, alt_text, rights_basis, author, license,
            license_url, source_url, status
        )
        VALUES (
            %s, 'route_hero', '/static/photos/hero-2.jpg', 'Second',
            'creative_commons', 'Ada', 'CC BY 4.0',
            'https://creativecommons.org/licenses/by/4.0',
            'https://example.com/hero', 'published'
        )
        """,
        (route_id,),
        "route_media_one_published_hero",
    )
    _media(
        db,
        route_id,
        checkpoint_id=stops[0],
        role="checkpoint_scene",
        asset_path="/static/photos/scene-cc.jpg",
        alt_text="Published scene",
        **documented,
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, role, asset_path, alt_text, rights_basis,
            author, license, license_url, source_url, status
        )
        VALUES (
            %s, %s, 'checkpoint_scene', '/static/photos/one-b.jpg', 'Again',
            'creative_commons', 'Ada', 'CC BY 4.0',
            'https://creativecommons.org/licenses/by/4.0',
            'https://example.com/one', 'published'
        )
        """,
        (route_id, stops[0]),
        "route_media_one_published_scene",
    )
    _media(
        db,
        route_id,
        checkpoint_id=stops[0],
        role="journal",
        asset_path="/static/photos/journal-0.jpg",
        alt_text="Journal",
        position=0,
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, role, asset_path, alt_text, position
        )
        VALUES (%s, %s, 'journal', '/static/photos/journal-0b.jpg', 'Again', 0)
        """,
        (route_id, stops[0]),
        "route_media_journal_position_key",
    )
    _media(
        db,
        route_id,
        checkpoint_id=stops[0],
        discovery_id=discovery_id,
        role="discovery",
        asset_path="/static/photos/discovery-0.jpg",
        alt_text="Discovery",
        position=0,
        status="retired",
    )
    _media(
        db,
        route_id,
        checkpoint_id=stops[0],
        discovery_id=discovery_id,
        role="discovery",
        asset_path="/static/photos/discovery-0b.jpg",
        alt_text="Retired duplicate",
        position=0,
        status="retired",
    )
    _media(
        db,
        route_id,
        checkpoint_id=stops[0],
        discovery_id=discovery_id,
        role="discovery",
        asset_path="/static/photos/discovery-draft.jpg",
        alt_text="Current discovery",
        position=0,
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, discovery_id, role, asset_path, alt_text, position
        )
        VALUES (%s, %s, %s, 'discovery', '/static/photos/discovery-again.jpg', 'Again', 0)
        """,
        (route_id, stops[0], discovery_id),
        "route_media_discovery_position_key",
    )
    _other_route, other_stops = _route(db, "narrative-media-other")
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, role, asset_path, alt_text
        )
        VALUES (%s, %s, 'checkpoint_scene', '/static/photos/cross.jpg', 'Cross')
        """,
        (route_id, other_stops[0]),
        "route_media_checkpoint_route_fkey",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, discovery_id, role, asset_path, alt_text, position
        )
        VALUES (%s, %s, %s, 'discovery', '/static/photos/bad.jpg', 'Bad', 9)
        """,
        (route_id, stops[1], discovery_id),
        "route_media_discovery_checkpoint_fkey",
    )
    _media(
        db,
        route_id,
        checkpoint_id=stops[1],
        role="journal",
        rights_basis="public_domain",
        license="Public domain",
        source_url="https://example.com/pd",
        asset_path="/static/photos/pd.jpg",
        alt_text="Public domain",
        position=1,
        status="published",
    )
    _media(
        db,
        route_id,
        checkpoint_id=stops[1],
        role="journal",
        rights_basis="cc0",
        license="CC0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        source_url="https://example.com/cc0",
        asset_path="/static/photos/cc0.jpg",
        alt_text="CC0",
        position=2,
        status="published",
    )
    _media(
        db,
        route_id,
        checkpoint_id=stops[1],
        role="journal",
        rights_basis="creative_commons",
        author="Ada",
        license="CC BY 2.5",
        license_url="https://creativecommons.org/licenses/by/2.5",
        source_url="https://example.com/by-25",
        asset_path="/static/photos/by-25.jpg",
        alt_text="CC BY 2.5",
        position=3,
        status="published",
    )
    _media(
        db,
        route_id,
        checkpoint_id=stops[1],
        role="checkpoint_scene",
        rights_basis="commissioned",
        license="Commission grant",
        author="Sam",
        permission_note="Grant from Sam for commercial use",
        asset_path="/static/photos/grant.jpg",
        alt_text="Commissioned",
        status="published",
    )
    _expect_error(
        db,
        """
        UPDATE adventurer.route_media
        SET status = 'published'
        WHERE route_id = %s AND asset_path = '/static/photos/journal-0.jpg'
        """,
        (route_id,),
        "route_media_publish_rights",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, role, asset_path, alt_text, rights_basis,
            license, source_url, position, status
        )
        VALUES (
            %s, %s, 'journal', '/static/photos/pd-short.jpg', 'Missing source',
            'public_domain', 'Public domain', NULL, 4, 'published'
        )
        """,
        (route_id, stops[1]),
        "route_media_publish_rights",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, role, asset_path, alt_text, rights_basis,
            license, source_url, position, status
        )
        VALUES (
            %s, %s, 'journal', '/static/photos/cc0-short.jpg', 'Missing license',
            'cc0', 'CC0', 'https://example.com/cc0', 4, 'published'
        )
        """,
        (route_id, stops[1]),
        "route_media_publish_rights",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, role, asset_path, alt_text, rights_basis,
            author, license, position, status
        )
        VALUES (
            %s, %s, 'journal', '/static/photos/grant-short.jpg', 'Missing note',
            'commissioned', 'Sam', 'Commission grant', 4, 'published'
        )
        """,
        (route_id, stops[1]),
        "route_media_publish_rights",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, role, asset_path, alt_text, rights_basis, author, license,
            license_url, source_url, position, status
        )
        VALUES (
            %s, %s, 'journal', '/static/photos/nc.jpg', 'Noncommercial',
            'creative_commons', 'Ada', 'CC BY-NC 4.0',
            'https://creativecommons.org/licenses/by-nc/4.0',
            'https://example.com/nc', 4, 'published'
        )
        """,
        (route_id, stops[1]),
        "route_media_publish_rights",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.route_media (
            route_id, checkpoint_id, role, asset_path, alt_text, rights_basis, author, license,
            license_url, source_url, position, status
        )
        VALUES (
            %s, %s, 'journal', '/static/photos/nd.jpg', 'No derivatives',
            'creative_commons', 'Ada', 'CC BY-ND 4.0',
            'https://creativecommons.org/licenses/by-nd/4.0',
            'https://example.com/nd', 4, 'published'
        )
        """,
        (route_id, stops[1]),
        "route_media_publish_rights",
    )
    assert _other_route


def test_published_media_rejects_null_rights_metadata(db):
    route_id, stops = _route(db, "narrative-null-rights")
    checkpoint_id = stops[0]
    _media(
        db,
        route_id,
        checkpoint_id=checkpoint_id,
        role="journal",
        rights_basis="public_domain",
        license="Public domain",
        source_url="https://example.com/pd",
        asset_path="/static/photos/null-pd.jpg",
        alt_text="Public domain",
        position=0,
        status="published",
    )
    _media(
        db,
        route_id,
        checkpoint_id=checkpoint_id,
        role="journal",
        rights_basis="cc0",
        license="CC0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        source_url="https://example.com/cc0",
        asset_path="/static/photos/null-cc0.jpg",
        alt_text="CC0",
        position=1,
        status="published",
    )
    _media(
        db,
        route_id,
        checkpoint_id=checkpoint_id,
        role="journal",
        rights_basis="creative_commons",
        author="Ada",
        license="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0",
        source_url="https://example.com/by",
        asset_path="/static/photos/null-by.jpg",
        alt_text="CC BY",
        position=2,
        status="published",
    )
    _media(
        db,
        route_id,
        checkpoint_id=checkpoint_id,
        role="journal",
        rights_basis="commissioned",
        license="Commission grant",
        author="Sam",
        permission_note="Grant from Sam for commercial use",
        asset_path="/static/photos/null-grant.jpg",
        alt_text="Commissioned",
        position=3,
        status="published",
    )
    _media(
        db,
        route_id,
        checkpoint_id=checkpoint_id,
        role="journal",
        rights_basis="public_domain",
        license=None,
        source_url="https://example.com/draft",
        asset_path="/static/photos/null-draft.jpg",
        alt_text="Draft",
        position=4,
        status="draft",
    )
    _media(
        db,
        route_id,
        checkpoint_id=checkpoint_id,
        role="journal",
        rights_basis="public_domain",
        license=None,
        source_url=None,
        asset_path="/static/photos/null-retired.jpg",
        alt_text="Retired",
        position=4,
        status="retired",
    )
    incomplete = (
        (
            "public_domain",
            {"source_url": "https://example.com/pd"},
        ),
        (
            "public_domain",
            {"license": "Public domain"},
        ),
        (
            "cc0",
            {
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "source_url": "https://example.com/cc0",
            },
        ),
        (
            "cc0",
            {"license": "CC0", "source_url": "https://example.com/cc0"},
        ),
        (
            "cc0",
            {
                "license": "CC0",
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            },
        ),
        (
            "creative_commons",
            {
                "author": "Ada",
                "license_url": "https://creativecommons.org/licenses/by/4.0",
                "source_url": "https://example.com/by",
            },
        ),
        (
            "creative_commons",
            {
                "license": "CC BY 4.0",
                "license_url": "https://creativecommons.org/licenses/by/4.0",
                "source_url": "https://example.com/by",
            },
        ),
        (
            "creative_commons",
            {
                "author": "Ada",
                "license": "CC BY 4.0",
                "source_url": "https://example.com/by",
            },
        ),
        (
            "creative_commons",
            {
                "author": "Ada",
                "license": "CC BY 4.0",
                "license_url": "https://creativecommons.org/licenses/by/4.0",
            },
        ),
        (
            "commissioned",
            {
                "author": "Sam",
                "permission_note": "Grant from Sam for commercial use",
            },
        ),
        (
            "commissioned",
            {
                "license": "Commission grant",
                "permission_note": "Grant from Sam for commercial use",
            },
        ),
        (
            "commissioned",
            {"license": "Commission grant", "author": "Sam"},
        ),
    )
    for basis, fields in incomplete:
        _expect_error(
            db,
            """
            INSERT INTO adventurer.route_media (
                route_id, checkpoint_id, role, asset_path, alt_text, rights_basis,
                author, license, license_url, source_url, permission_note, position, status
            )
            VALUES (%s, %s, 'journal', %s, 'Incomplete', %s, %s, %s, %s, %s, %s, 9, 'published')
            """,
            (
                route_id,
                checkpoint_id,
                f"/static/photos/incomplete-{basis}.jpg",
                basis,
                fields.get("author"),
                fields.get("license"),
                fields.get("license_url"),
                fields.get("source_url"),
                fields.get("permission_note"),
            ),
            "route_media_publish_rights",
        )


def test_acknowledgments_follow_the_journey_attempt(db):
    route_id, stops = _route(db, "narrative-ack")
    _other_id, other_stops = _route(db, "narrative-ack-other")
    owner = _user(db, "AckOwner")
    stranger = _user(db, "AckStranger")
    original = _journey(db, owner, route_id, attempt=1, status="abandoned", current=False)
    restarted = _journey(db, owner, route_id, attempt=2, status="active", current=True)
    _story(db, stops[0], route_id)
    db.execute(
        """
        INSERT INTO adventurer.journey_story_views (
            journey_id, user_id, route_id, checkpoint_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (original, owner, route_id, stops[0]),
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_story_views (
            journey_id, user_id, route_id, checkpoint_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (original, owner, route_id, stops[0]),
        "journey_story_views_journey_checkpoint_key",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_story_views (
            journey_id, user_id, route_id, checkpoint_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (original, owner, route_id, other_stops[0]),
        "journey_story_views_checkpoint_fkey",
    )
    _expect_error(
        db,
        """
        INSERT INTO adventurer.journey_story_views (
            journey_id, user_id, route_id, checkpoint_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (original, stranger, route_id, stops[1]),
        "journey_story_views_journey_fkey",
    )
    assert db.execute(
        "SELECT COUNT(*) AS n FROM adventurer.journey_story_views WHERE journey_id = %s",
        (restarted,),
    ).fetchone()["n"] == 0
    db.execute("DELETE FROM adventurer.route_journeys WHERE id = %s", (original,))
    assert db.execute(
        "SELECT COUNT(*) AS n FROM adventurer.journey_story_views WHERE journey_id = %s",
        (original,),
    ).fetchone()["n"] == 0
    assert db.execute(
        "SELECT COUNT(*) AS n FROM adventurer.checkpoint_stories WHERE route_id = %s",
        (route_id,),
    ).fetchone()["n"] == 1
    assert db.execute(
        "SELECT COUNT(*) AS n FROM adventurer.route_journeys WHERE id = %s",
        (restarted,),
    ).fetchone()["n"] == 1
    _expect_error(
        db,
        "DELETE FROM adventurer.routes WHERE id = %s",
        (route_id,),
        "route_journeys_route_id_fkey",
    )
    db.execute(
        """
        INSERT INTO adventurer.journey_story_views (
            journey_id, user_id, route_id, checkpoint_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (restarted, owner, route_id, stops[0]),
    )
    db.execute("DELETE FROM adventurer.users WHERE id = %s", (owner,))
    assert db.execute(
        "SELECT COUNT(*) AS n FROM adventurer.journey_story_views WHERE user_id = %s",
        (owner,),
    ).fetchone()["n"] == 0
    assert db.execute(
        "SELECT COUNT(*) AS n FROM adventurer.checkpoint_stories WHERE route_id = %s",
        (route_id,),
    ).fetchone()["n"] == 1
    assert db.execute(
        "SELECT COUNT(*) AS n FROM adventurer.route_journeys WHERE route_id = %s",
        (route_id,),
    ).fetchone()["n"] == 0


def test_narrative_tables_do_not_reference_progress_or_expedition_results(db):
    referenced = {
        row["ref"]
        for row in db.execute(
            """
            SELECT ref.relname AS ref
            FROM pg_constraint AS fk
            JOIN pg_class AS src ON src.oid = fk.conrelid
            JOIN pg_class AS ref ON ref.oid = fk.confrelid
            JOIN pg_namespace AS src_ns ON src_ns.oid = src.relnamespace
            WHERE src_ns.nspname = 'adventurer'
              AND fk.contype = 'f'
              AND src.relname IN (
                'route_chapters',
                'checkpoint_stories',
                'story_discoveries',
                'route_media',
                'journey_story_views'
              )
            """
        ).fetchall()
    }
    assert referenced.isdisjoint(
        {
            "journey_credits",
            "journey_metric_totals",
            "expedition_result_entries",
            "expedition_result_metrics",
            "expedition_updates",
            "activity_journey_assignments",
        }
    )


def test_downgrade_removes_only_the_narrative_catalog():
    _upgrade()
    conn = _connect()
    try:
        everest = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM adventurer.route_checkpoints AS checkpoint
            JOIN adventurer.routes AS route ON route.id = checkpoint.route_id
            WHERE route.slug = 'everest'
            """
        ).fetchone()["n"]
        route_id = conn.execute(
            """
            INSERT INTO adventurer.routes (slug, name, region, summary, status)
            VALUES ('narrative-down', 'narrative-down', 'Test', 'A test route', 'draft')
            RETURNING id
            """
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO adventurer.route_chapters (route_id, slug, title, position)
            VALUES (%s, 'approach', 'Approach', 0)
            """,
            (route_id,),
        )
        conn.commit()
    finally:
        conn.close()
    down = _alembic("downgrade", "0009_multi_metric_progress")
    try:
        if down.returncode != 0:
            pytest.fail(f"downgrade failed\n{down.stdout}\n{down.stderr}")
        check = _connect()
        try:
            assert check.execute(
                "SELECT to_regclass('adventurer.route_chapters') AS name"
            ).fetchone()["name"] is None
            assert "chapter_id" not in _columns(check, "route_checkpoints")
            assert check.execute(
                "SELECT to_regclass('adventurer.progress_metrics') AS name"
            ).fetchone()["name"] == "adventurer.progress_metrics"
            assert check.execute(
                """
                SELECT COUNT(*) AS n
                FROM adventurer.route_checkpoints AS checkpoint
                JOIN adventurer.routes AS route ON route.id = checkpoint.route_id
                WHERE route.slug = 'everest'
                """
            ).fetchone()["n"] == everest
        finally:
            check.close()
    finally:
        restored = _alembic("upgrade", "head")
        if restored.returncode != 0:
            pytest.fail(f"upgrade after downgrade failed\n{restored.stdout}\n{restored.stderr}")
        cleanup = _connect()
        try:
            cleanup.execute("DELETE FROM adventurer.routes WHERE slug = 'narrative-down'")
            cleanup.commit()
        finally:
            cleanup.close()
