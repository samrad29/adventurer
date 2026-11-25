from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest

from adventurer.achievements import AchievementError, delete_earned_achievements, issue_achievement
from adventurer.db import get_db
from adventurer.expeditions import create_private_expedition, remove_participation_privacy
from adventurer.services import create_adventurer

from test_expeditions import _route

SOURCE = "expedition_completion"


def _definition(database, *, repeatable=False):
    return database.execute(
        """
        INSERT INTO achievement_definitions
            (slug, name, description, scope, source, status, repeatable, presentation)
        VALUES ('founder', 'Founding Finisher', 'Finished the first climb.',
                'expedition', ?, 'active', ?, '{"badge": "founder"}')
        RETURNING id
        """,
        (SOURCE, repeatable),
    ).fetchone()["id"]


def _edition(database, definition, slug, size):
    return database.execute(
        """
        INSERT INTO achievement_editions (definition_id, slug, size)
        VALUES (?, ?, ?) RETURNING id
        """,
        (definition, slug, size),
    ).fetchone()["id"]


def _user(name):
    return create_adventurer(name, "trailhead")["id"]


def _issue(database, user, definition, *, event="first-finish", **options):
    return issue_achievement(
        database, user_id=user, definition_id=definition,
        source=SOURCE, event_key=event, **options,
    )


def test_snapshot_survives_definition_edits_and_expedition_privacy_removal(app):
    with app.app_context():
        database = get_db()
        user = _user("AwardOwner")
        route = _route("award-route")
        expedition = create_private_expedition(
            user, slug="award-party", name="Award Party", route_slug="award-route"
        )
        expedition_id = expedition["expedition"]["id"]
        definition = _definition(database)
        edition = _edition(database, definition, "first", 3)
        second_edition = _edition(database, definition, "second", 5)
        database.commit()
        options = {"expedition_id": expedition_id, "route_id": route}
        unlimited = _issue(database, user, definition, **options)
        first = _issue(database, user, definition, edition_id=edition, **options)
        second = _issue(database, user, definition, edition_id=second_edition, **options)
        assert unlimited["sequence_number"] is None
        assert (first["sequence_number"], first["edition_size"]) == (1, 3)
        assert (second["sequence_number"], second["edition_size"]) == (1, 5)
        assert _issue(database, user, definition, edition_id=edition) == first
        database.execute(
            """
            UPDATE achievement_definitions
            SET name = 'New Name', presentation = '{"badge": "new"}',
                default_edition_size = 100, status = 'retired'
            WHERE id = ?
            """,
            (definition,),
        )
        database.commit()

        assert remove_participation_privacy(user, "award-party", confirm=True)["expedition"] is None
        retry = _issue(database, user, definition, edition_id=edition, **options)
        assert retry["id"] == first["id"]
        assert retry["expedition_id"] is None
        assert retry["award_snapshot"] == first["award_snapshot"]
        assert retry["award_snapshot"]["name"] == "Founding Finisher"
        assert retry["award_snapshot"]["presentation"] == {"badge": "founder"}
        assert retry["context_snapshot"] == first["context_snapshot"]
        assert retry["context_snapshot"]["expedition"] == {"name": "Award Party", "slug": "award-party"}
        with pytest.raises(psycopg.errors.CheckViolation, match="definition is unavailable"):
            _issue(database, user, definition, event="other", edition_id=edition)
        database.rollback()
        with pytest.raises(psycopg.errors.CheckViolation, match="edition cannot be created"):
            _edition(database, definition, "retired", 5)
        database.rollback()


def test_edition_rollback_and_account_deletion_leave_consumed_numbers_only(app):
    with app.app_context():
        database = get_db()
        first_user = _user("AwardFirst")
        second_user = _user("AwardSecond")
        definition = _definition(database)
        edition = _edition(database, definition, "first", 3)
        database.commit()

        assert _issue(database, first_user, definition, edition_id=edition)["sequence_number"] == 1
        database.rollback()
        first = _issue(database, first_user, definition, edition_id=edition)
        assert first["sequence_number"] == 1
        database.commit()
        with pytest.raises(psycopg.errors.CheckViolation, match="edition history"):
            database.execute("UPDATE achievement_editions SET size = 4 WHERE id = ?", (edition,))
        database.rollback()
        with pytest.raises(psycopg.errors.CheckViolation, match="history cannot change"):
            database.execute("UPDATE earned_achievements SET event_key = 'changed' WHERE id = ?", (first["id"],))
        database.rollback()
        with pytest.raises(psycopg.errors.CheckViolation, match="account deletion batch"):
            delete_earned_achievements(database, first_user)
        database.rollback()

        database.execute("UPDATE users SET deletion_state = 'deleting' WHERE id = ?", (first_user,))
        database.commit()
        with pytest.raises(AchievementError, match="recipient is unavailable"):
            _issue(database, first_user, definition, edition_id=edition)
        database.rollback()
        database.execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
        delete_earned_achievements(database, first_user)
        delete_earned_achievements(database, first_user)
        database.execute("DELETE FROM users WHERE id = ?", (first_user,))
        database.commit()
        assert database.execute("SELECT id FROM earned_achievements").fetchall() == []
        assert _issue(database, second_user, definition, edition_id=edition)["sequence_number"] == 2
        database.commit()


def test_concurrent_retry_and_competing_recipients_respect_edition_capacity(app):
    with app.app_context():
        database = get_db()
        first_user, second_user, third_user = [_user(f"AwardRace{n}") for n in range(3)]
        definition = _definition(database)
        edition = _edition(database, definition, "race", 2)
        retry_edition = _edition(database, definition, "retry", 1)
        database.commit()

    def award(user, edition_id, ready):
        with app.app_context():
            database = get_db()
            database.execute("SET LOCAL statement_timeout = '10s'")
            ready.wait()
            try:
                result = _issue(database, user, definition, edition_id=edition_id)
            except psycopg.errors.CheckViolation as exc:
                database.rollback()
                assert "edition is exhausted" in str(exc)
                return user, None, None
            database.commit()
            return user, result["id"], result["sequence_number"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        ready = Barrier(2, timeout=10)
        retries = [pool.submit(award, first_user, retry_edition, ready) for _ in range(2)]
        first, retry = [future.result() for future in retries]
        assert retry == first
        assert first[2] == 1
        ready = Barrier(3, timeout=10)
        competitors = [
            pool.submit(award, user, edition, ready)
            for user in (first_user, second_user, third_user)
        ]
        results = [future.result() for future in competitors]
    successful = {(award_id, number) for _, award_id, number in results if award_id is not None}
    assert sorted(number for _, number in successful) == [1, 2]


def test_repeatable_event_uses_explicit_retry_identity_and_edition_window(app):
    with app.app_context():
        database = get_db()
        user = _user("AwardRepeat")
        definition = _definition(database, repeatable=True)
        first = _issue(database, user, definition, repeat_key="first")
        second = _issue(database, user, definition, repeat_key="second")
        assert first["id"] != second["id"]
        assert _issue(database, user, definition, repeat_key="first") == first
        edition = _edition(database, definition, "closed", 3)
        database.execute(
            "UPDATE achievement_editions SET available_until = now() - interval '1 hour' WHERE id = ?",
            (edition,),
        )
        database.commit()
        with pytest.raises(psycopg.errors.CheckViolation, match="outside its issuance window"):
            _issue(database, user, definition, edition_id=edition)
        database.rollback()
        assert database.execute(
            "SELECT issued_count FROM achievement_editions WHERE id = ?", (edition,)
        ).fetchone()["issued_count"] == 0


def test_achievement_and_route_recommendation_tables_are_private(app):
    with app.app_context():
        database = get_db()
        tables = (
            "achievement_definitions", "achievement_editions", "earned_achievements",
            "route_activity_recommendations",
        )
        for table in tables:
            assert database.execute(
                "SELECT relrowsecurity FROM pg_class WHERE oid = ?::regclass", (table,)
            ).fetchone()["relrowsecurity"]
            assert database.execute(
                """
                SELECT rolname FROM pg_roles
                WHERE rolname IN ('anon', 'authenticated')
                  AND has_table_privilege(oid, ?::regclass, 'SELECT, INSERT, UPDATE, DELETE')
                """,
                (table,),
            ).fetchall() == []
