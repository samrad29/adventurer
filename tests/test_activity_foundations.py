"""Release 1 cycling types, zero-credit assignments, and credit origins.

Runs against the isolated PostgreSQL database named by TEST_DATABASE_URL.
Restricted expedition policies and credit replay are not part of this phase.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from adventurer.activities import (
    ActivityError,
    create_activity,
    delete_activity,
    get_activity,
    preview_activity,
    preview_activity_update,
    update_activity,
)
from adventurer.db import get_db
from adventurer.expeditions import (
    archive_private_expedition,
    board_metrics,
    create_private_expedition,
    get_leaderboard,
)
from adventurer.journeys import restart_journey, start_journey
from adventurer.metrics import parse_measurement
from adventurer.services import create_adventurer

DAY = date(2026, 6, 1)


def test_migration_accepts_cycling_types_and_backfills_origins(app):
    with app.app_context():
        version = get_db().execute("SELECT version_num FROM alembic_version").fetchone()
        assert version["version_num"] == "0012_deterministic_allocation"
        definition = get_db().execute(
            """
            SELECT pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE conname = 'activities_type_chk'
            """
        ).fetchone()["definition"]
        assert "cycle" in definition
        assert "indoor_cycle" in definition
        assert get_db().execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'adventurer'
              AND table_name = 'expeditions'
              AND column_name = 'activity_policy'
            """
        ).fetchone() is None
        user_id = _user("Origin Backfill")
        route_id = _route("found-backfill", {"elevation": _feet("1000")})
        journey_id = start_journey(user_id, route_id)["id"]
        saved = create_activity(user_id, _fields(elevation="100"))
        credit = _credit_row(saved["activity"]["id"])
        origin = _origin_row(saved["activity"]["id"], "elevation")
        assert origin["credited_at"] == credit["created_at"]
        assert credit["credited_amount"] == _feet("100")
        totals_before = _totals(journey_id)
        with pytest.raises(Exception, match="cannot be deleted"):
            get_db().execute(
                """
                DELETE FROM journey_credit_origins
                WHERE assignment_id = ? AND metric_code = 'elevation'
                """,
                (origin["assignment_id"],),
            )
        get_db().rollback()
        restored = _origin_row(saved["activity"]["id"], "elevation")
        assert restored["credited_at"] == credit["created_at"]
        assert _totals(journey_id) == totals_before
        assert _credit_row(saved["activity"]["id"])["credited_amount"] == _feet("100")


def test_cycling_distance_limits_follow_the_resulting_activity_type(app):
    with app.app_context():
        user_id = _user("Cycle Logger")
        route_id = _route(
            "found-cycle",
            {"distance": _miles("250"), "elevation": _feet("1000")},
        )
        start_journey(user_id, route_id)
        both = create_activity(
            user_id,
            _fields(distance="10", elevation="50", activity_type="cycle"),
        )
        assert both["activity"]["elevation_gain_m"] == _feet("50")
        assert both["activity"]["distance_m"] == _miles("10")
        assert set(_credit_map(both)) == {"distance", "elevation"}
        for activity_type in ("cycle", "indoor_cycle"):
            saved = create_activity(
                user_id,
                _fields(distance="200", activity_type=activity_type),
            )
            assert saved["activity"]["activity_type"] == activity_type
            assert saved["activity"]["distance_m"] == _miles("200")
            assert saved["activity"]["elevation_gain_m"] is None
            assert saved["credits"]
            assert saved["no_credit_reason"] is None
        with pytest.raises(ActivityError, match="200 mi"):
            create_activity(user_id, _fields(distance="200.001", activity_type="cycle"))
        with pytest.raises(ActivityError, match="75 mi"):
            create_activity(user_id, _fields(distance="75.001", activity_type="run"))
        with pytest.raises(ActivityError, match="10,000 ft"):
            create_activity(
                user_id,
                _fields(distance="1", elevation="10000.01", activity_type="indoor_cycle"),
            )
        ride = create_activity(user_id, _fields(distance="100", activity_type="cycle"))
        noted = update_activity(user_id, ride["activity"]["id"], {"note": "long"})
        assert noted["activity"]["note"] == "long"
        assert noted["activity"]["activity_type"] == "cycle"
        assert noted["activity"]["distance_m"] == _miles("100")
        with pytest.raises(ActivityError, match="75 mi"):
            update_activity(user_id, ride["activity"]["id"], {"activity_type": "run"})
        changed = update_activity(
            user_id,
            ride["activity"]["id"],
            {"activity_type": "run", "distance_mi": "40"},
        )
        assert changed["activity"]["activity_type"] == "run"
        assert changed["activity"]["distance_m"] == _miles("40")


def test_a_type_edit_does_not_change_credited_totals(app):
    with app.app_context():
        user_id = _user("Type Credit")
        route_id = _route("found-type-credit", {"elevation": _feet("1000")})
        journey_id = start_journey(user_id, route_id)["id"]
        saved = create_activity(user_id, _fields(elevation="100", activity_type="hike"))
        version = _journey_row(journey_id)["progress_version"]
        totals = _totals(journey_id)
        updated = update_activity(
            user_id, saved["activity"]["id"], {"activity_type": "cycle"}
        )
        assert updated["activity"]["activity_type"] == "cycle"
        assert _credit_map(updated) == {"elevation": _feet("100")}
        assert _totals(journey_id) == totals
        assert _journey_row(journey_id)["progress_version"] == version


def test_a_capped_activity_keeps_its_assignment_and_no_zero_credit_row(app):
    with app.app_context():
        user_id = _user("Capped Logger")
        route_id = _route(
            "found-capped",
            {"elevation": _feet("1000"), "distance": _miles("5")},
        )
        journey_id = start_journey(user_id, route_id)["id"]
        create_activity(user_id, _fields(elevation="1000"))
        version = _journey_row(journey_id)["progress_version"]
        preview = preview_activity(user_id, _fields(elevation="800"))
        assert preview["saved_without_credit"] is True
        assert preview["no_credit_reason"] == "metric_already_complete"
        assert preview["credits_journey"] is False
        saved = create_activity(user_id, _fields(elevation="800"))
        assert saved["credits"] == []
        assert saved["no_credit_reason"] == "metric_already_complete"
        assert saved["journey"]["id"] == journey_id
        assert saved["arrivals"] == []
        activity = get_activity(user_id, saved["activity"]["id"])
        assert activity["credit"] is None
        assert activity["association"]["journey_id"] == journey_id
        assert _assignment_count(saved["activity"]["id"]) == 1
        assert _credit_count(saved["activity"]["id"]) == 0
        assert _journey_row(journey_id)["progress_version"] == version + 1
        assert _totals(journey_id)["elevation"] == _feet("1000")


def test_clearing_every_credit_keeps_the_assignment_when_a_measurement_remains(app):
    with app.app_context():
        user_id = _user("Remain Logger")
        route_id = _route(
            "found-remain",
            {"elevation": _feet("1000"), "distance": _miles("5")},
        )
        journey_id = start_journey(user_id, route_id)["id"]
        first = create_activity(user_id, _fields(elevation="800"))
        second = create_activity(user_id, _fields(elevation="100", distance="1"))
        update_activity(user_id, second["activity"]["id"], {"elevation_ft": "0"})
        assert _credit_map(get_saved(user_id, second["activity"]["id"])) == {
            "distance": _miles("1")
        }
        assert _assignment_count(second["activity"]["id"]) == 1
        assert _totals(journey_id)["elevation"] == _feet("800")
        assert _credit_map(get_saved(user_id, first["activity"]["id"])) == {
            "elevation": _feet("800")
        }
        origin = _origin_row(second["activity"]["id"], "elevation")
        assert origin is not None
        assert _credit_count_metric(second["activity"]["id"], "elevation") == 0
        restored = update_activity(
            user_id, second["activity"]["id"], {"elevation_ft": "50"}
        )
        assert _credit_map(restored)["elevation"] == _feet("50")
        assert _origin_row(second["activity"]["id"], "elevation")["credited_at"] == origin[
            "credited_at"
        ]
        fresh = _credit_row_metric(second["activity"]["id"], "elevation")
        assert fresh["created_at"] != origin["credited_at"]


def test_unassigned_activities_stay_unassigned_and_restart_does_not_move_them(app):
    with app.app_context():
        user_id = _user("Loose Logger")
        early = create_activity(user_id, _fields(elevation="20"))
        assert early["no_credit_reason"] == "no_active_journey"
        assert early["journey"] is None
        assert get_activity(user_id, early["activity"]["id"])["association"] is None
        route_id = _route("found-loose", {"elevation": _feet("1000")})
        journey_id = start_journey(user_id, route_id)["id"]
        assert _assignment_count(early["activity"]["id"]) == 0
        zero = create_activity(user_id, _fields(elevation="0"))
        assert zero["no_credit_reason"] == "no_positive_measurement"
        assert _assignment_count(zero["activity"]["id"]) == 0
        credited = create_activity(user_id, _fields(elevation="100"))
        restarted = restart_journey(user_id, route_id)
        assert _assignment_journey(credited["activity"]["id"]) == journey_id
        assert _assignment_journey(credited["activity"]["id"]) != restarted["journey"]["id"]
        assert _assignment_count(early["activity"]["id"]) == 0


def test_deleting_one_credit_does_not_give_its_cap_to_another_activity(app):
    with app.app_context():
        user_id = _user("Cap Logger")
        route_id = _route("found-cap-order", {"elevation": _feet("2000")})
        journey_id = start_journey(user_id, route_id)["id"]
        first = create_activity(user_id, _fields(elevation="800"))
        second = create_activity(user_id, _fields(elevation="400"))
        assert _credit_map(second)["elevation"] == _feet("400")
        update_activity(user_id, second["activity"]["id"], {"elevation_ft": "0"})
        assert _assignment_count(second["activity"]["id"]) == 1
        assert _credit_map(get_saved(user_id, first["activity"]["id"]))["elevation"] == _feet(
            "800"
        )
        assert _totals(journey_id)["elevation"] == _feet("800")
        third = create_activity(user_id, _fields(elevation="400"))
        delete_activity(user_id, third["activity"]["id"])
        assert _credit_map(get_saved(user_id, first["activity"]["id"]))["elevation"] == _feet(
            "800"
        )
        assert _totals(journey_id)["elevation"] == _feet("800")


def test_a_note_on_a_zero_credit_activity_does_not_invalidate_the_summit_token(app):
    with app.app_context():
        user_id = _user("Preview Logger")
        route_id = _route(
            "found-preview",
            {"elevation": _feet("100"), "distance": _miles("2")},
        )
        journey_id = start_journey(user_id, route_id)["id"]
        create_activity(user_id, _fields(elevation="100"))
        capped = create_activity(user_id, _fields(elevation="10"))
        version = _journey_row(journey_id)["progress_version"]
        preview = preview_activity(user_id, _fields(distance="2"))
        assert preview["will_complete"] is True
        noted = update_activity(user_id, capped["activity"]["id"], {"note": "still"})
        assert noted["no_credit_reason"] == "metric_already_complete"
        assert _journey_row(journey_id)["progress_version"] == version
        saved = create_activity(
            user_id,
            _fields(distance="2"),
            confirmation_token=preview["confirmation_token"],
        )
        assert saved["journey"]["status"] == "completed"
        assert _feed_kinds(user_id) == []


def test_a_zero_credit_activity_invalidates_the_previous_summit_token(app):
    with app.app_context():
        user_id = _user("Stale Summit")
        route_id = _route(
            "found-stale",
            {"elevation": _feet("100"), "distance": _miles("2")},
        )
        journey_id = start_journey(user_id, route_id)["id"]
        create_activity(user_id, _fields(elevation="100"))
        preview = preview_activity(user_id, _fields(distance="2"))
        token = preview["confirmation_token"]
        version = _journey_row(journey_id)["progress_version"]
        create_activity(user_id, _fields(elevation="25"))
        assert _journey_row(journey_id)["progress_version"] == version + 1
        with pytest.raises(ActivityError) as caught:
            create_activity(
                user_id,
                _fields(distance="2"),
                confirmation_token=token,
            )
        assert caught.value.code == "confirmation_required"
        assert _journey_row(journey_id)["status"] == "active"
        fresh = preview_activity(user_id, _fields(distance="2"))
        saved = create_activity(
            user_id,
            _fields(distance="2"),
            confirmation_token=fresh["confirmation_token"],
        )
        assert saved["journey"]["status"] == "completed"
        assert saved["arrivals"] == [] or isinstance(saved["arrivals"], list)


def test_privacy_cutoff_uses_the_origin_and_a_restored_credit_keeps_it(app):
    with app.app_context():
        user_id = _user("Cutoff Logger")
        route_id = _route(
            "found-cutoff",
            {"elevation": _feet("5000"), "distance": _miles("10")},
        )
        journey_id = start_journey(user_id, route_id)["id"]
        slug = "found-cutoff-exp"
        _expedition(user_id, slug, "found-cutoff")
        other = create_private_expedition(
            user_id,
            slug="found-cutoff-two",
            name="Second Board",
            route_slug="found-cutoff",
        )
        early = create_activity(user_id, _fields(elevation="100", distance="1"))
        later = create_activity(user_id, _fields(elevation="40"))
        early_origin = _origin_row(early["activity"]["id"], "elevation")["credited_at"]
        later_origin = _origin_row(later["activity"]["id"], "elevation")["credited_at"]
        assert later_origin > early_origin
        midpoint = early_origin + (later_origin - early_origin) / 2
        _set_cutoff(user_id, slug, midpoint)
        visible = board_metrics(get_db(), journey_id, route_id, midpoint)
        elevation = next(item for item in visible if item["metric"] == "elevation")
        assert elevation["credited_m"] == _feet("40")
        open_board = board_metrics(get_db(), journey_id, route_id, None)
        open_elevation = next(item for item in open_board if item["metric"] == "elevation")
        assert open_elevation["credited_m"] == _feet("140")
        other_board = board_metrics(get_db(), journey_id, route_id, None)
        assert next(item for item in other_board if item["metric"] == "elevation")[
            "credited_m"
        ] == _feet("140")
        assert other["journey"]["id"] == journey_id
        personal = _totals(journey_id)["elevation"]
        assert personal == _feet("140")
        update_activity(user_id, early["activity"]["id"], {"elevation_ft": "0"})
        assert _origin_row(early["activity"]["id"], "elevation")["credited_at"] == early_origin
        update_activity(user_id, early["activity"]["id"], {"elevation_ft": "80"})
        assert _origin_row(early["activity"]["id"], "elevation")["credited_at"] == early_origin
        after = board_metrics(get_db(), journey_id, route_id, midpoint)
        assert next(item for item in after if item["metric"] == "elevation")[
            "credited_m"
        ] == _feet("40")
        assert _totals(journey_id)["elevation"] == _feet("120")


def test_a_closed_snapshot_keeps_the_origin_filtered_total(app):
    with app.app_context():
        user_id = _user("Snapshot Logger")
        _route("found-snap", {"elevation": _feet("1000")})
        created = _expedition(user_id, "found-snap-exp", "found-snap")
        journey_id = created["journey"]["id"]
        create_activity(user_id, _fields(elevation="100"))
        archive_private_expedition(user_id, "found-snap-exp")
        board = get_leaderboard(user_id, "found-snap-exp")
        credited = board["entries"][0]["metrics"][0]["total_m"]
        create_activity(user_id, _fields(elevation="50"))
        again = get_leaderboard(user_id, "found-snap-exp")
        assert again["entries"][0]["metrics"][0]["total_m"] == credited
        assert _totals(journey_id)["elevation"] == _feet("150")


def test_account_deletion_removes_credit_origins(app):
    with app.app_context():
        user_id = _user("Origin Delete")
        route_id = _route("found-delete-user", {"elevation": _feet("1000")})
        journey_id = start_journey(user_id, route_id)["id"]
        saved = create_activity(user_id, _fields(elevation="10"))
        assignment_id = get_db().execute(
            "SELECT id FROM activity_journey_assignments WHERE activity_id = ?",
            (saved["activity"]["id"],),
        ).fetchone()["id"]
        assert _origin_row(saved["activity"]["id"], "elevation") is not None
        get_db().execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
        get_db().execute(
            "UPDATE users SET deletion_state = 'deleting' WHERE id = ?",
            (user_id,),
        )
        get_db().execute(
            "DELETE FROM journey_credits WHERE assignment_id = ?",
            (assignment_id,),
        )
        assert _origin_row(saved["activity"]["id"], "elevation") is not None
        get_db().execute(
            "DELETE FROM journey_metric_totals WHERE journey_id = ?",
            (journey_id,),
        )
        get_db().execute("DELETE FROM users WHERE id = ?", (user_id,))
        get_db().commit()
        remaining = get_db().execute(
            "SELECT COUNT(*) AS n FROM journey_credit_origins"
        ).fetchone()["n"]
        assert remaining == 0


def test_a_completed_journey_freezes_a_zero_credit_assignment(app):
    with app.app_context():
        user_id = _user("Frozen Logger")
        route_id = _route(
            "found-frozen",
            {"elevation": _feet("100"), "distance": _miles("1")},
        )
        journey_id = start_journey(user_id, route_id)["id"]
        create_activity(user_id, _fields(elevation="100"))
        extra = create_activity(user_id, _fields(elevation="30"))
        preview = preview_activity(user_id, _fields(distance="1"))
        create_activity(
            user_id,
            _fields(distance="1"),
            confirmation_token=preview["confirmation_token"],
        )
        assert _journey_row(journey_id)["status"] == "completed"
        with pytest.raises(ActivityError) as edited:
            update_activity(user_id, extra["activity"]["id"], {"elevation_ft": "40"})
        assert edited.value.status == 409
        with pytest.raises(ActivityError) as deleted:
            delete_activity(user_id, extra["activity"]["id"])
        assert deleted.value.status == 409
        noted = update_activity(
            user_id,
            extra["activity"]["id"],
            {"note": "kept", "activity_type": "hike"},
        )
        assert noted["activity"]["note"] == "kept"
        assert noted["activity"]["activity_type"] == "hike"
        assert _assignment_count(extra["activity"]["id"]) == 1
        assert _credit_count(extra["activity"]["id"]) == 0
        assert _journey_row(journey_id)["status"] == "completed"


def _user(name: str) -> int:
    return create_adventurer(name, "trailhead")["id"]


def _fields(**extra) -> dict:
    payload = {"happened_on": DAY}
    if "elevation" in extra:
        payload["elevation_ft"] = extra.pop("elevation")
    if "distance" in extra:
        payload["distance_mi"] = extra.pop("distance")
    payload.update(extra)
    return payload


def _feet(amount: str) -> Decimal:
    return parse_measurement("elevation", amount)


def _miles(amount: str) -> Decimal:
    return parse_measurement("distance", amount)


def _route(slug: str, targets: dict[str, Decimal]) -> int:
    database = get_db()
    route_id = database.execute(
        """
        INSERT INTO routes (slug, name, region, summary, status)
        VALUES (?, ?, 'Test', 'A test route', 'draft')
        RETURNING id
        """,
        (slug, slug),
    ).fetchone()["id"]
    for metric, amount in targets.items():
        database.execute(
            """
            INSERT INTO route_requirements (route_id, metric_code, target_amount)
            VALUES (?, ?, ?)
            """,
            (route_id, metric, amount),
        )
    checkpoint_id = database.execute(
        """
        INSERT INTO route_checkpoints (route_id, slug, name, position)
        VALUES (?, 'summit', 'Summit', 0)
        RETURNING id
        """,
        (route_id,),
    ).fetchone()["id"]
    for metric, amount in targets.items():
        database.execute(
            """
            INSERT INTO checkpoint_requirements (
                checkpoint_id, route_id, metric_code, threshold_amount
            )
            VALUES (?, ?, ?, ?)
            """,
            (checkpoint_id, route_id, metric, amount),
        )
    database.execute("UPDATE routes SET status = 'published' WHERE id = ?", (route_id,))
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
    database.commit()
    return route_id


def _expedition(user_id: int, slug: str, route_slug: str) -> dict:
    created = create_private_expedition(
        user_id,
        slug=slug,
        name="Foundation",
        route_slug=route_slug,
    )
    return created


def _feed_kinds(user_id: int) -> list[str]:
    return [
        row["kind"]
        for row in get_db().execute(
            """
            SELECT kind
            FROM expedition_updates
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()
    ]


def _journey_row(journey_id: int) -> dict:
    return dict(
        get_db().execute(
            "SELECT id, status, progress_version FROM route_journeys WHERE id = ?",
            (journey_id,),
        ).fetchone()
    )


def _totals(journey_id: int) -> dict[str, Decimal]:
    return {
        row["metric_code"]: row["credited_total"]
        for row in get_db().execute(
            """
            SELECT metric_code, credited_total
            FROM journey_metric_totals
            WHERE journey_id = ?
            """,
            (journey_id,),
        ).fetchall()
    }


def _credit_map(saved: dict) -> dict[str, Decimal]:
    credits = saved["credits"]
    if credits is None:
        activity = saved["activity"]
        credits = [] if activity["credit"] is None else activity["credit"]["metrics"]
    return {item["metric"]: item["credited_m"] for item in credits}


def get_saved(user_id: int, activity_id: int) -> dict:
    activity = get_activity(user_id, activity_id)
    credits = [] if activity["credit"] is None else activity["credit"]["metrics"]
    return {"activity": activity, "credits": credits}


def _assignment_count(activity_id: int) -> int:
    return get_db().execute(
        """
        SELECT COUNT(*) AS n
        FROM activity_journey_assignments
        WHERE activity_id = ?
        """,
        (activity_id,),
    ).fetchone()["n"]


def _assignment_journey(activity_id: int) -> int | None:
    row = get_db().execute(
        """
        SELECT journey_id
        FROM activity_journey_assignments
        WHERE activity_id = ?
        """,
        (activity_id,),
    ).fetchone()
    if row is None:
        return None
    return row["journey_id"]


def _credit_count(activity_id: int) -> int:
    return get_db().execute(
        """
        SELECT COUNT(*) AS n
        FROM journey_credits AS credit
        JOIN activity_journey_assignments AS assignment
            ON assignment.id = credit.assignment_id
        WHERE assignment.activity_id = ?
        """,
        (activity_id,),
    ).fetchone()["n"]


def _credit_count_metric(activity_id: int, metric: str) -> int:
    return get_db().execute(
        """
        SELECT COUNT(*) AS n
        FROM journey_credits AS credit
        JOIN activity_journey_assignments AS assignment
            ON assignment.id = credit.assignment_id
        WHERE assignment.activity_id = ? AND credit.metric_code = ?
        """,
        (activity_id, metric),
    ).fetchone()["n"]


def _credit_row(activity_id: int) -> dict:
    return dict(
        get_db().execute(
            """
            SELECT credit.credited_amount, credit.created_at, credit.assignment_id
            FROM journey_credits AS credit
            JOIN activity_journey_assignments AS assignment
                ON assignment.id = credit.assignment_id
            WHERE assignment.activity_id = ?
            """,
            (activity_id,),
        ).fetchone()
    )


def _credit_row_metric(activity_id: int, metric: str) -> dict:
    return dict(
        get_db().execute(
            """
            SELECT credit.credited_amount, credit.created_at
            FROM journey_credits AS credit
            JOIN activity_journey_assignments AS assignment
                ON assignment.id = credit.assignment_id
            WHERE assignment.activity_id = ? AND credit.metric_code = ?
            """,
            (activity_id, metric),
        ).fetchone()
    )


def _origin_row(activity_id: int, metric: str) -> dict | None:
    row = get_db().execute(
        """
        SELECT origin.assignment_id, origin.credited_at
        FROM journey_credit_origins AS origin
        JOIN activity_journey_assignments AS assignment
            ON assignment.id = origin.assignment_id
        WHERE assignment.activity_id = ? AND origin.metric_code = ?
        """,
        (activity_id, metric),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _set_cutoff(user_id: int, slug: str, when: datetime) -> None:
    get_db().execute(
        """
        UPDATE expedition_participations AS participation
        SET display_credits_after = ?
        FROM expeditions AS expedition
        WHERE expedition.id = participation.expedition_id
          AND expedition.slug = ?
          AND participation.user_id = ?
        """,
        (when, slug, user_id),
    )
    get_db().commit()
