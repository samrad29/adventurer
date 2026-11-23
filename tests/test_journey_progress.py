"""Journey progress against the isolated database and exact Decimal conversion.

Activity assignment, summit confirmation, and API payloads are not this phase.
The database tests commit their own routes and credits, then read them back.
"""

from __future__ import annotations

from decimal import Decimal, DecimalException

import pytest

from adventurer.db import get_db
from adventurer.journey_progress import (
    CheckpointMark,
    assess_checkpoints,
    journey_progress,
)
from adventurer.metrics import MetricError, assess_metric, parse_measurement
from adventurer.services import create_adventurer

FOOT = Decimal("0.3048")
MILE = Decimal("1609.344")


def test_conversion_accepts_the_input_step_and_rejects_finer_values():
    assert parse_measurement("elevation", "0.01") == Decimal("0.003048")
    assert parse_measurement("elevation", "10.25") == Decimal("3.124200")
    assert parse_measurement("distance", "0.001") == Decimal("1.609344")
    assert parse_measurement("distance", "1.25") == Decimal("2011.680000")
    assert parse_measurement("elevation", None) is None
    assert parse_measurement("elevation", "  ") is None
    assert parse_measurement("distance", "0") == Decimal("0")
    assert parse_measurement("elevation", "0.00") == Decimal("0")
    assert parse_measurement("distance", Decimal("0.001")) == Decimal("1.609344")
    assert parse_measurement("elevation", 1) == Decimal("0.304800")
    assert parse_measurement("elevation", "10.100") == Decimal("3.078480")
    assert parse_measurement("distance", "+1.250") == Decimal("2011.680000")
    assert parse_measurement("elevation", Decimal("0")) == Decimal("0")

    with pytest.raises(MetricError, match="negative"):
        parse_measurement("elevation", "-0.01")
    with pytest.raises(MetricError, match="negative"):
        parse_measurement("distance", "-0")
    with pytest.raises(MetricError, match="0.01 ft"):
        parse_measurement("elevation", "0.001")
    with pytest.raises(MetricError, match="0.001 mi"):
        parse_measurement("distance", "1.0001")
    with pytest.raises(MetricError, match="decimal"):
        parse_measurement("distance", 1.5)
    with pytest.raises(MetricError, match="decimal"):
        parse_measurement("elevation", True)
    with pytest.raises(MetricError, match="decimal"):
        parse_measurement("elevation", "nan")
    with pytest.raises(MetricError, match="decimal"):
        parse_measurement("distance", Decimal("Infinity"))
    with pytest.raises(MetricError, match="does not exist"):
        parse_measurement("steps", "1")


def test_commas_are_accepted_only_as_thousands_groups():
    assert parse_measurement("elevation", "1,000") == Decimal("304.800000")
    assert parse_measurement("elevation", "1,234.50") == parse_measurement("elevation", "1234.50")
    assert parse_measurement("distance", "1,000.125") == parse_measurement("distance", "1000.125")
    assert parse_measurement("elevation", "32,808,398,950.13") == Decimal("9999999999.999624")

    for malformed in ("1,2", "1,,000", "12,34", "1,000,", ",100", "1,000.2,5", "1,00"):
        with pytest.raises(MetricError, match="groups of three"):
            parse_measurement("elevation", malformed)
        with pytest.raises(MetricError, match="groups of three"):
            parse_measurement("distance", f" {malformed} ")


def test_stored_meters_must_fit_numeric_16_6():
    assert parse_measurement("elevation", "32808398950.13") == Decimal("9999999999.999624")
    assert parse_measurement("distance", "6,213,711.922") == Decimal("9999999999.399168")

    with pytest.raises(MetricError, match="range"):
        parse_measurement("elevation", "32808398950.14")
    with pytest.raises(MetricError, match="range"):
        parse_measurement("distance", "6213711.923")
    with pytest.raises(MetricError, match="range"):
        parse_measurement("elevation", "32,808,398,950.14")


def test_decimal_failures_raise_metric_error():
    for raw in ("1e200", Decimal("1e200"), "1e1000000"):
        with pytest.raises(MetricError) as caught:
            parse_measurement("elevation", raw)
        assert isinstance(caught.value.__cause__, DecimalException)
    with pytest.raises(MetricError, match="0.01 ft") as caught:
        parse_measurement("elevation", Decimal("10.101"))
    assert caught.value.__cause__ is None


def test_display_rounding_does_not_finish_a_short_metric_or_uncap_a_long_one():
    short = assess_metric("elevation", Decimal("0.304800"), Decimal("0.304700"))
    assert short.complete is False
    assert short.remaining_m == Decimal("0.000100")
    assert short.remaining_display == Decimal("0.01")
    assert short.credited_display == Decimal("0.99")
    assert short.percent < Decimal("100")

    finished = assess_metric("distance", Decimal("1609.344000"), Decimal("1609.344000"))
    assert finished.complete is True
    assert finished.remaining_m == Decimal("0")
    assert finished.remaining_display == Decimal("0")
    assert finished.percent == Decimal("100")

    over = assess_metric("elevation", Decimal("1.000000"), Decimal("1.500000"))
    assert over.complete is True
    assert over.credited_m == Decimal("1.000000")
    assert over.remaining_m == Decimal("0")
    assert over.percent == Decimal("100")

    tiny = assess_metric("elevation", Decimal("0.000001"), Decimal("0"))
    assert tiny.complete is False
    assert tiny.target_display == Decimal("0.01")
    assert tiny.credited_display == Decimal("0")
    assert tiny.remaining_display == Decimal("0.01")
    finished_tiny = assess_metric("elevation", Decimal("0.000001"), Decimal("0.000001"))
    assert finished_tiny.complete is True
    assert finished_tiny.remaining_display == Decimal("0")
    assert finished_tiny.percent == Decimal("100")


def test_a_checkpoint_stays_locked_until_every_threshold_is_met():
    early = CheckpointMark(1, "camp", "Camp", 0, False, (("elevation", Decimal("10")),))
    both = CheckpointMark(
        2,
        "summit",
        "Summit",
        1,
        False,
        (("distance", Decimal("20")), ("elevation", Decimal("10"))),
    )
    marks = assess_checkpoints([both, early], {"elevation": Decimal("10"), "distance": Decimal("19")})
    assert [mark.reached for mark in marks] == [True, False]
    assert marks[0].slug == "camp"


def test_elevation_only_progress_uses_the_route_requirement(app):
    with app.app_context():
        user_id = _user("Elev Walker")
        route_id = _route(
            "progress-elev",
            {"elevation": _feet("1000")},
            [
                ("base", 0, {"elevation": _feet("400")}),
                ("summit", 1, {"elevation": _feet("1000")}),
            ],
        )
        journey_id = _journey(user_id, route_id)
        _credit(user_id, journey_id, route_id, "elevation", _feet("400"))
        before = _fingerprint(journey_id)
        progress = journey_progress(user_id, journey_id)
        assert _fingerprint(journey_id) == before
        assert progress.status == "active"
        assert [metric.code for metric in progress.metrics] == ["elevation"]
        metric = progress.metrics[0]
        assert metric.target_m == _feet("1000")
        assert metric.credited_m == _feet("400")
        assert metric.remaining_m == _feet("600")
        assert metric.complete is False
        assert metric.percent == Decimal("40.00")
        assert progress.requirements_met is False
        assert progress.highest_checkpoint.slug == "base"
        assert progress.next_checkpoint.slug == "summit"
        assert progress.final_checkpoint_reached is False


def test_distance_only_progress_ignores_an_unrequired_elevation_measurement(app):
    with app.app_context():
        user_id = _user("Mile Walker")
        route_id = _route(
            "progress-dist",
            {"distance": _miles("2")},
            [
                ("halfway", 0, {"distance": _miles("1")}),
                ("finish", 1, {"distance": _miles("2")}),
            ],
        )
        journey_id = _journey(user_id, route_id)
        _activity(user_id, Decimal("0"), None)
        _credit(user_id, journey_id, route_id, "distance", _miles("1"))
        progress = journey_progress(user_id, journey_id)
        assert [metric.code for metric in progress.metrics] == ["distance"]
        metric = progress.metrics[0]
        assert metric.credited_m == _miles("1")
        assert metric.remaining_m == _miles("1")
        assert metric.complete is False
        assert progress.highest_checkpoint.slug == "halfway"
        assert progress.final_checkpoint_reached is False
        assert progress.requirements_met is False


def test_dual_metric_progress_keeps_each_remainder_separate(app):
    with app.app_context():
        user_id = _user("Dual Walker")
        route_id, journey_id = _dual(user_id, "progress-dual")
        _credit(user_id, journey_id, route_id, "elevation", _feet("100"))
        _credit(user_id, journey_id, route_id, "distance", _miles("0.5"))
        progress = journey_progress(user_id, journey_id)
        by_code = {metric.code: metric for metric in progress.metrics}
        assert by_code["elevation"].credited_m == _feet("100")
        assert by_code["elevation"].remaining_m == _feet("400")
        assert by_code["elevation"].complete is False
        assert by_code["distance"].credited_m == _miles("0.5")
        assert by_code["distance"].remaining_m == _miles("1.5")
        assert by_code["distance"].complete is False
        assert progress.highest_checkpoint is None
        assert progress.next_checkpoint.slug == "camp"
        assert progress.requirements_met is False
        assert progress.final_checkpoint_reached is False
        assert progress.status == "active"


def test_one_finished_metric_does_not_complete_the_journey_or_the_final_checkpoint(app):
    with app.app_context():
        elevation_user = _user("Elev Done")
        route_id, elevation_journey = _dual(elevation_user, "progress-elev-done")
        _credit(elevation_user, elevation_journey, route_id, "elevation", _feet("500"))
        _credit(elevation_user, elevation_journey, route_id, "distance", _miles("1"))
        elevation_first = journey_progress(elevation_user, elevation_journey)
        assert _by_code(elevation_first)["elevation"].complete is True
        assert _by_code(elevation_first)["distance"].complete is False
        assert _by_code(elevation_first)["distance"].remaining_m == _miles("1")
        assert elevation_first.highest_checkpoint.slug == "camp"
        assert elevation_first.final_checkpoint_reached is False
        assert elevation_first.requirements_met is False

        distance_user = _user("Dist Done")
        route_id, distance_journey = _dual(distance_user, "progress-dist-done")
        _credit(distance_user, distance_journey, route_id, "elevation", _feet("250"))
        _credit(distance_user, distance_journey, route_id, "distance", _miles("2"))
        distance_first = journey_progress(distance_user, distance_journey)
        assert _by_code(distance_first)["distance"].complete is True
        assert _by_code(distance_first)["elevation"].complete is False
        assert _by_code(distance_first)["elevation"].remaining_m == _feet("250")
        assert distance_first.highest_checkpoint.slug == "camp"
        assert distance_first.final_checkpoint_reached is False
        assert distance_first.requirements_met is False


def test_stored_completion_agrees_with_the_final_checkpoint_and_does_not_write_status(app):
    with app.app_context():
        user_id = _user("Route Done")
        route_id, journey_id = _dual(user_id, "progress-done")
        _credit(user_id, journey_id, route_id, "elevation", _feet("500"))
        _credit(user_id, journey_id, route_id, "distance", _miles("2"))
        before = _fingerprint(journey_id)
        progress = journey_progress(user_id, journey_id)
        assert _fingerprint(journey_id) == before
        assert progress.status == "active"
        assert progress.requirements_met is True
        assert progress.final_checkpoint_reached is True
        assert progress.highest_checkpoint.slug == "summit"
        assert progress.next_checkpoint is None
        assert all(metric.complete for metric in progress.metrics)
        assert all(metric.credited_m == metric.target_m for metric in progress.metrics)
        assert all(metric.percent == Decimal("100") for metric in progress.metrics)


def test_lifecycle_status_is_reported_and_left_unchanged(app):
    with app.app_context():
        route_id = _route(
            "progress-life",
            {"elevation": _feet("100")},
            [("summit", 0, {"elevation": _feet("100")})],
        )
        cases = (
            ("Life Active", "active", _feet("40"), False),
            ("Life Paused", "paused", _feet("40"), False),
            ("Life Abandoned", "abandoned", _feet("40"), False),
            ("Life Complete", "completed", _feet("100"), True),
        )
        for name, status, amount, done in cases:
            user_id = _user(name)
            journey_id = _journey(user_id, route_id, "active")
            _credit(user_id, journey_id, route_id, "elevation", amount)
            if status != "active":
                _status(journey_id, status)
            before = _fingerprint(journey_id)
            progress = journey_progress(user_id, journey_id)
            assert _fingerprint(journey_id) == before
            assert progress.status == status
            assert progress.requirements_met is done
            assert progress.final_checkpoint_reached is done
            assert progress.metrics[0].complete is done


def test_a_zero_measurement_is_stored_progress_of_zero_and_a_null_is_absent(app):
    with app.app_context():
        user_id = _user("Zero Walker")
        route_id = _route(
            "progress-zero",
            {"elevation": _feet("10")},
            [("summit", 0, {"elevation": _feet("10")})],
        )
        journey_id = _journey(user_id, route_id)
        _activity(user_id, Decimal("0"), None)
        progress = journey_progress(user_id, journey_id)
        assert progress.metrics[0].credited_m == Decimal("0")
        assert progress.metrics[0].complete is False
        assert progress.requirements_met is False
        assert progress.highest_checkpoint is None
        credited = get_db().execute(
            "SELECT count(*) AS n FROM journey_credits WHERE journey_id = ?",
            (journey_id,),
        ).fetchone()["n"]
        assert credited == 0


def test_near_target_storage_still_reports_a_visible_remainder(app):
    with app.app_context():
        user_id = _user("Near Summit")
        route_id = _route(
            "progress-near",
            {"elevation": Decimal("0.304800")},
            [
                ("base", 0, {"elevation": Decimal("0.152400")}),
                ("summit", 1, {"elevation": Decimal("0.304800")}),
            ],
        )
        journey_id = _journey(user_id, route_id)
        _credit(user_id, journey_id, route_id, "elevation", Decimal("0.304700"))
        progress = journey_progress(user_id, journey_id)
        metric = progress.metrics[0]
        assert metric.complete is False
        assert metric.remaining_m == Decimal("0.000100")
        assert metric.remaining_display == Decimal("0.01")
        assert metric.percent < Decimal("100")
        assert progress.requirements_met is False
        assert progress.final_checkpoint_reached is False
        assert progress.highest_checkpoint.slug == "base"


def _by_code(progress):
    return {metric.code: metric for metric in progress.metrics}


def _feet(amount: str) -> Decimal:
    return parse_measurement("elevation", amount)


def _miles(amount: str) -> Decimal:
    return parse_measurement("distance", amount)


def _user(name: str) -> int:
    return create_adventurer(name, "trailhead")["id"]


def _route(slug: str, targets: dict[str, Decimal], checkpoints: list[tuple]) -> int:
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
    for slug_name, position, thresholds in checkpoints:
        checkpoint_id = database.execute(
            """
            INSERT INTO route_checkpoints (route_id, slug, name, position)
            VALUES (?, ?, ?, ?)
            RETURNING id
            """,
            (route_id, slug_name, slug_name, position),
        ).fetchone()["id"]
        for metric, amount in thresholds.items():
            database.execute(
                """
                INSERT INTO checkpoint_requirements (
                    checkpoint_id, route_id, metric_code, threshold_amount
                )
                VALUES (?, ?, ?, ?)
                """,
                (checkpoint_id, route_id, metric, amount),
            )
    database.execute(
        "UPDATE routes SET status = 'published' WHERE id = ?",
        (route_id,),
    )
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


def _dual(user_id: int, slug: str) -> tuple[int, int]:
    route_id = _route(
        slug,
        {"elevation": _feet("500"), "distance": _miles("2")},
        [
            ("camp", 0, {"elevation": _feet("250"), "distance": _miles("1")}),
            ("summit", 1, {"elevation": _feet("500"), "distance": _miles("2")}),
        ],
    )
    return route_id, _journey(user_id, route_id)


def _journey(user_id: int, route_id: int, status: str = "active") -> int:
    database = get_db()
    journey_id = database.execute(
        """
        INSERT INTO route_journeys (
            user_id, route_id, status, attempt_number, is_current
        )
        VALUES (?, ?, ?, 1, true)
        RETURNING id
        """,
        (user_id, route_id, status),
    ).fetchone()["id"]
    database.commit()
    return journey_id


def _status(journey_id: int, status: str) -> None:
    database = get_db()
    database.execute(
        """
        UPDATE route_journeys
        SET status = ?,
            paused_at = CASE WHEN ? = 'paused' THEN now() ELSE paused_at END,
            abandoned_at = CASE WHEN ? = 'abandoned' THEN now() ELSE abandoned_at END,
            completed_at = CASE WHEN ? = 'completed' THEN now() ELSE completed_at END
        WHERE id = ?
        """,
        (status, status, status, status, journey_id),
    )
    database.commit()


def _activity(user_id: int, elevation: Decimal | None, distance: Decimal | None) -> int:
    database = get_db()
    activity_id = database.execute(
        """
        INSERT INTO activities (
            user_id, activity_date, elevation_gain_m, distance_m, note
        )
        VALUES (?, DATE '2026-04-01', ?, ?, 'progress')
        RETURNING id
        """,
        (user_id, elevation, distance),
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
    database.commit()
    return activity_id


def _credit(user_id: int, journey_id: int, route_id: int, metric: str, amount: Decimal) -> None:
    elevation = amount if metric == "elevation" else None
    distance = amount if metric == "distance" else None
    activity_id = _activity(user_id, elevation, distance)
    database = get_db()
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
        VALUES (?, ?, ?, ?, ?)
        """,
        (assignment_id, journey_id, route_id, metric, amount),
    )
    database.commit()


def _fingerprint(journey_id: int) -> tuple:
    database = get_db()
    journey = database.execute(
        """
        SELECT status, progress_version, completed_at, paused_at, abandoned_at
        FROM route_journeys
        WHERE id = ?
        """,
        (journey_id,),
    ).fetchone()
    credits = database.execute(
        """
        SELECT count(*) AS n, COALESCE(SUM(credited_amount), 0) AS total
        FROM journey_credits
        WHERE journey_id = ?
        """,
        (journey_id,),
    ).fetchone()
    totals = database.execute(
        """
        SELECT metric_code, credited_total
        FROM journey_metric_totals
        WHERE journey_id = ?
        ORDER BY metric_code
        """,
        (journey_id,),
    ).fetchall()
    return (
        journey["status"],
        journey["progress_version"],
        journey["completed_at"],
        journey["paused_at"],
        journey["abandoned_at"],
        credits["n"],
        credits["total"],
        tuple((row["metric_code"], row["credited_total"]) for row in totals),
    )
