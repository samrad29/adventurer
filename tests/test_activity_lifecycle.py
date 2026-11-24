"""Multi-metric activity creation, confirmation, edits, and deletion.

These tests run against the isolated PostgreSQL database. The credit-math
test does not open a connection. Expedition ranking and the HTTP API are not
part of this phase.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg
import pytest

from adventurer.activities import (
    ActivityError,
    _canonical_credits,
    _portion,
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
from adventurer.metrics import parse_measurement
from adventurer.services import create_adventurer

DAY = date(2026, 6, 1)


def test_credit_math_caps_each_metric_in_decimal_storage():
    assert _portion(None, Decimal("10"), Decimal("1")) == Decimal("0")
    assert _portion(Decimal("0"), Decimal("10"), Decimal("1")) == Decimal("0")
    assert _portion(Decimal("4"), Decimal("10"), Decimal("7")) == Decimal("3")
    assert _portion(Decimal("2"), Decimal("10"), Decimal("10")) == Decimal("0")
    text = _canonical_credits(
        {"elevation": Decimal("152.4"), "distance": Decimal("3218.688")}
    )
    assert text == "distance=3218.688000,elevation=152.400000"


def test_kilometers_are_rejected_and_miles_use_the_statute_conversion(app):
    with app.app_context():
        user_id = _user("Mile Logger")
        route_id = _route(
            "life-miles",
            {"distance": _miles("5")},
            [("summit", 0, {"distance": _miles("5")})],
        )
        start_journey(user_id, route_id)
        with pytest.raises(ActivityError, match="not supported"):
            preview_activity(user_id, {"distance_km": "1.609344", "happened_on": DAY})
        saved = create_activity(user_id, {"distance_mi": "1", "happened_on": DAY})
        assert saved["activity"]["distance_m"] == Decimal("1609.344000")
        assert saved["activity"]["elevation_gain_m"] is None
        assert _credit_map(saved) == {"distance": Decimal("1609.344000")}


def test_elevation_only_creation_stores_the_full_measurement(app):
    with app.app_context():
        user_id = _user("Elev Logger")
        route_id = _route(
            "life-elev",
            {"elevation": _feet("1000")},
            [
                ("camp", 0, {"elevation": _feet("400")}),
                ("summit", 1, {"elevation": _feet("1000")}),
            ],
        )
        journey = start_journey(user_id, route_id)
        before = _fingerprint(journey["id"])
        preview = preview_activity(user_id, _fields(elevation="250"))
        assert _fingerprint(journey["id"]) == before
        assert preview["will_complete"] is False
        assert preview["confirmation_token"] is None
        metric = _metric(preview, "elevation")
        assert metric["credit_m"] == _feet("250")
        assert metric["complete"] is False
        saved = create_activity(user_id, _fields(elevation="450", distance="3"))
        assert saved["activity"]["elevation_gain_m"] == _feet("450")
        assert saved["activity"]["distance_m"] == _miles("3")
        assert _credit_map(saved) == {"elevation": _feet("450")}
        assert _assignment_count(saved["activity"]["id"]) == 1
        assert saved["journey"]["status"] == "active"
        assert saved["journey"]["final_checkpoint_reached"] is False
        assert saved["journey"]["highest_checkpoint"] == "camp"
        assert _totals(journey["id"]) == {"elevation": _feet("450")}
        listed = list_activities(user_id)
        assert listed[0]["id"] == saved["activity"]["id"]
        assert listed[0]["credit"]["metrics"] == [
            {"metric": "elevation", "credited_m": _feet("450")}
        ]


def test_distance_only_creation_ignores_elevation_for_credit(app):
    with app.app_context():
        user_id = _user("Road Logger")
        route_id = _route(
            "life-road",
            {"distance": _miles("4")},
            [("summit", 0, {"distance": _miles("4")})],
        )
        journey = start_journey(user_id, route_id)
        saved = create_activity(user_id, _fields(distance="1.5", elevation="100"))
        assert saved["activity"]["elevation_gain_m"] == _feet("100")
        assert _credit_map(saved) == {"distance": _miles("1.5")}
        assert saved["journey"]["status"] == "active"
        assert _totals(journey["id"]) == {"distance": _miles("1.5")}


def test_dual_creation_caps_each_metric_and_uses_one_assignment(app):
    with app.app_context():
        user_id = _user("Dual Logger")
        _route_id, journey_id = _started_dual(user_id, "life-dual")
        create_activity(user_id, _fields(elevation="800", distance="0.25"))
        saved = create_activity(user_id, _fields(elevation="500", distance="0.5"))
        assert _assignment_count(saved["activity"]["id"]) == 1
        assert _credit_map(saved) == {
            "distance": _miles("0.5"),
            "elevation": _feet("200"),
        }
        assert saved["activity"]["elevation_gain_m"] == _feet("500")
        assert saved["activity"]["distance_m"] == _miles("0.5")
        assert saved["journey"]["status"] == "active"
        assert saved["journey"]["requirements_met"] is False
        totals = _totals(journey_id)
        assert totals["elevation"] == _feet("1000")
        assert totals["distance"] == _miles("0.75")
        by_code = {item["metric"]: item for item in saved["journey"]["metrics"]}
        assert by_code["elevation"]["complete"] is True
        assert by_code["distance"]["complete"] is False


def test_zero_and_null_measurements_do_not_create_an_assignment(app):
    with app.app_context():
        user_id = _user("Zero Logger")
        route_id = _route(
            "life-zero",
            {"elevation": _feet("100")},
            [("summit", 0, {"elevation": _feet("100")})],
        )
        journey = start_journey(user_id, route_id)
        saved = create_activity(
            user_id,
            {"elevation_ft": "0", "distance_mi": None, "happened_on": DAY},
        )
        assert saved["activity"]["elevation_gain_m"] == Decimal("0")
        assert saved["activity"]["distance_m"] is None
        assert saved["credits"] == []
        assert _assignment_count(saved["activity"]["id"]) == 0
        assert _journey_row(journey["id"])["progress_version"] == 1
        with pytest.raises(ActivityError, match="negative"):
            create_activity(user_id, _fields(elevation="-1"))


def test_a_new_activity_credits_only_the_active_journey(app):
    with app.app_context():
        user_id = _user("Switch Logger")
        paused_route = _route(
            "life-switch-paused",
            {"elevation": _feet("100")},
            [("summit", 0, {"elevation": _feet("100")})],
        )
        active_route = _route(
            "life-switch-active",
            {"distance": _miles("3")},
            [("summit", 0, {"distance": _miles("3")})],
        )
        paused = start_journey(user_id, paused_route)
        create_activity(user_id, _fields(elevation="10"))
        switch_journey(user_id, active_route)
        saved = create_activity(user_id, _fields(elevation="40", distance="1"))
        assert _journey_row(paused["id"])["status"] == "paused"
        assert _totals(paused["id"]) == {"elevation": _feet("10")}
        assert _credit_map(saved) == {"distance": _miles("1")}
        assert saved["activity"]["elevation_gain_m"] == _feet("40")
        assert saved["journey"]["id"] != paused["id"]


def test_a_completed_journey_receives_no_further_credit(app):
    with app.app_context():
        user_id = _user("After Summit")
        route_id = _route(
            "life-after",
            {"elevation": _feet("100")},
            [("summit", 0, {"elevation": _feet("100")})],
        )
        journey = start_journey(user_id, route_id)
        fields = _fields(elevation="100")
        token = preview_activity(user_id, fields)["confirmation_token"]
        create_activity(user_id, fields, confirmation_token=token)
        version = _journey_row(journey["id"])["progress_version"]
        extra = create_activity(user_id, _fields(elevation="25"))
        assert extra["journey"] is None
        assert extra["credits"] == []
        assert extra["activity"]["elevation_gain_m"] == _feet("25")
        assert _journey_row(journey["id"])["status"] == "completed"
        assert _journey_row(journey["id"])["progress_version"] == version


def test_no_active_journey_keeps_personal_history(app):
    with app.app_context():
        user_id = _user("Solo Logger")
        saved = create_activity(user_id, _fields(elevation="10", distance="1"))
        assert saved["journey"] is None
        assert saved["credits"] == []
        assert saved["activity"]["elevation_gain_m"] == _feet("10")
        assert _assignment_count(saved["activity"]["id"]) == 0


def test_finishing_one_metric_does_not_require_confirmation(app):
    with app.app_context():
        user_id = _user("Half Summit")
        _route_id, journey_id = _started_dual(user_id, "life-half")
        preview = preview_activity(user_id, _fields(elevation="1000", distance="1"))
        assert preview["will_complete"] is False
        assert preview["confirmation_token"] is None
        assert _metric(preview, "elevation")["complete"] is True
        assert _metric(preview, "distance")["complete"] is False
        saved = create_activity(user_id, _fields(elevation="1000", distance="1"))
        assert saved["journey"]["status"] == "active"
        assert saved["journey"]["final_checkpoint_reached"] is False
        assert _event_kinds(user_id) == []
        assert _journey_row(journey_id)["status"] == "active"


def test_finishing_every_metric_requires_a_matching_token(app, monkeypatch):
    with app.app_context():
        user_id = _user("Full Summit")
        route_id = _route(
            "life-full",
            {"elevation": _feet("500"), "distance": _miles("2")},
            [
                ("camp", 0, {"elevation": _feet("250"), "distance": _miles("1")}),
                ("summit", 1, {"elevation": _feet("500"), "distance": _miles("2")}),
            ],
        )
        journey_id = start_journey(user_id, route_id)["id"]
        expedition_id = _expedition("full-club", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey_id)
        fields = _fields(elevation="900", distance="3")
        preview = preview_activity(user_id, fields)
        assert preview["will_complete"] is True
        assert "distance=" in preview["confirmation_token"]
        assert "elevation=" in preview["confirmation_token"]
        assert preview["confirmation_token"].startswith("v2|")
        _assert_unchanged(user_id, journey_id, activities=0)

        with pytest.raises(ActivityError) as missing:
            create_activity(user_id, fields)
        assert missing.value.code == "confirmation_required"
        _assert_unchanged(user_id, journey_id, activities=0)

        altered = preview["confirmation_token"][:-1] + (
            "0" if preview["confirmation_token"][-1] != "0" else "1"
        )
        with pytest.raises(ActivityError) as bad:
            create_activity(user_id, fields, confirmation_token=altered)
        assert bad.value.code == "confirmation_required"
        _assert_unchanged(user_id, journey_id, activities=0)

        monkeypatch.setattr(
            "adventurer.activities._now",
            lambda: preview["confirmation_expires_at"] + 1,
        )
        with pytest.raises(ActivityError) as expired:
            create_activity(user_id, fields, confirmation_token=preview["confirmation_token"])
        assert expired.value.code == "confirmation_required"
        _assert_unchanged(user_id, journey_id, activities=0)
        monkeypatch.undo()

        create_activity(user_id, _fields(elevation="10"))
        with pytest.raises(ActivityError) as stale:
            create_activity(user_id, fields, confirmation_token=preview["confirmation_token"])
        assert stale.value.code == "confirmation_required"
        assert stale.value.details["preview"]["confirmation_token"] != preview["confirmation_token"]
        assert _count("activities") == 1
        assert _journey_row(journey_id)["status"] == "active"

        fresh = stale.value.details["preview"]["confirmation_token"]
        saved = create_activity(user_id, fields, confirmation_token=fresh)
        assert saved["journey"]["status"] == "completed"
        assert saved["journey"]["requirements_met"] is True
        assert saved["journey"]["final_checkpoint_reached"] is True
        assert saved["journey"]["highest_checkpoint"] == "summit"
        assert saved["activity"]["elevation_gain_m"] == _feet("900")
        assert _credit_map(saved)["elevation"] == _feet("490")
        assert _credit_map(saved)["distance"] == _miles("2")
        kinds = _event_kinds(user_id)
        assert kinds.count("activity_logged") == 2
        assert "checkpoint_reached" in kinds
        assert kinds.count("summit_reached") == 1
        logged = _event_amounts(user_id, "activity_logged")
        assert logged[-1]["elevation"] == _feet("490")
        assert logged[-1]["distance"] == _miles("2")
        assert _event_amounts(user_id, "summit_reached")[0]["elevation"] == _feet("500")
        assert _event_amounts(user_id, "summit_reached")[0]["distance"] == _miles("2")


def test_a_second_summit_attempt_does_not_complete_again(app):
    with app.app_context():
        user_id = _user("Once Summit")
        route_id = _route(
            "life-once",
            {"elevation": _feet("100")},
            [("summit", 0, {"elevation": _feet("100")})],
        )
        journey = start_journey(user_id, route_id)
        expedition_id = _expedition("once-club", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey["id"])
        fields = _fields(elevation="100")
        token = preview_activity(user_id, fields)["confirmation_token"]
        first = create_activity(user_id, fields, confirmation_token=token)
        with pytest.raises(ActivityError) as caught:
            create_activity(user_id, fields, confirmation_token=token)
        assert caught.value.code == "confirmation_required"
        assert _count("activities") == 1
        assert _journey_row(journey["id"])["status"] == "completed"
        assert _event_kinds(user_id).count("summit_reached") == 1
        assert first["journey"]["progress_version"] == 2


def test_an_edit_preview_increases_credit_from_the_replacement_baseline(app):
    with app.app_context():
        user_id = _user("Preview Up")
        route_id = _route(
            "life-preview-up",
            {"elevation": _feet("1000")},
            [("summit", 0, {"elevation": _feet("1000")})],
        )
        journey = start_journey(user_id, route_id)
        create_activity(user_id, _fields(elevation="200"))
        saved = create_activity(user_id, _fields(elevation="100"))
        before = _edit_snapshot(journey["id"], saved["activity"]["id"])
        preview = preview_activity_update(user_id, saved["activity"]["id"], {"elevation_ft": "250"})
        assert _edit_snapshot(journey["id"], saved["activity"]["id"]) == before
        metric = _metric(preview, "elevation")
        assert metric["existing_credit_m"] == _feet("100")
        assert metric["current_m"] == _feet("200")
        assert metric["remaining_m"] == _feet("800")
        assert metric["credit_m"] == _feet("250")
        assert metric["resulting_m"] == _feet("450")
        _assert_preview_baseline(preview)


def test_an_edit_preview_decreases_credit_from_the_replacement_baseline(app):
    with app.app_context():
        user_id = _user("Preview Down")
        route_id = _route(
            "life-preview-down",
            {"elevation": _feet("1000")},
            [("summit", 0, {"elevation": _feet("1000")})],
        )
        journey = start_journey(user_id, route_id)
        create_activity(user_id, _fields(elevation="200"))
        saved = create_activity(user_id, _fields(elevation="100"))
        before = _edit_snapshot(journey["id"], saved["activity"]["id"])
        preview = preview_activity_update(user_id, saved["activity"]["id"], {"elevation_ft": "40"})
        assert _edit_snapshot(journey["id"], saved["activity"]["id"]) == before
        metric = _metric(preview, "elevation")
        assert metric["existing_credit_m"] == _feet("100")
        assert metric["current_m"] == _feet("200")
        assert metric["remaining_m"] == _feet("800")
        assert metric["credit_m"] == _feet("40")
        assert metric["resulting_m"] == _feet("240")
        _assert_preview_baseline(preview)


def test_an_edit_preview_keeps_a_credit_that_is_already_capped(app):
    with app.app_context():
        user_id = _user("Preview Cap")
        route_id = _route(
            "life-preview-cap",
            {"elevation": _feet("1000"), "distance": _miles("2")},
            [
                (
                    "summit",
                    0,
                    {"elevation": _feet("1000"), "distance": _miles("2")},
                )
            ],
        )
        journey = start_journey(user_id, route_id)
        create_activity(user_id, _fields(elevation="700"))
        saved = create_activity(user_id, _fields(elevation="500", distance="0.5"))
        before = _edit_snapshot(journey["id"], saved["activity"]["id"])
        preview = preview_activity_update(user_id, saved["activity"]["id"], {"elevation_ft": "800"})
        assert _edit_snapshot(journey["id"], saved["activity"]["id"]) == before
        elevation = _metric(preview, "elevation")
        assert elevation["existing_credit_m"] == _feet("300")
        assert elevation["current_m"] == _feet("700")
        assert elevation["remaining_m"] == _feet("300")
        assert elevation["measurement_m"] == _feet("800")
        assert elevation["credit_m"] == _feet("300")
        assert elevation["resulting_m"] == _feet("1000")
        distance = _metric(preview, "distance")
        assert distance["credit_m"] == distance["existing_credit_m"] == _miles("0.5")
        assert preview["will_complete"] is False
        _assert_preview_baseline(preview)


def test_an_edit_preview_changes_one_metric_and_leaves_the_other(app):
    with app.app_context():
        user_id = _user("Preview Half")
        route_id = _route(
            "life-preview-half",
            {"elevation": _feet("1000"), "distance": _miles("2")},
            [
                (
                    "summit",
                    0,
                    {"elevation": _feet("1000"), "distance": _miles("2")},
                )
            ],
        )
        journey = start_journey(user_id, route_id)
        create_activity(user_id, _fields(elevation="50", distance="0.25"))
        saved = create_activity(user_id, _fields(elevation="100", distance="0.5"))
        before = _edit_snapshot(journey["id"], saved["activity"]["id"])
        preview = preview_activity_update(user_id, saved["activity"]["id"], {"distance_mi": "1"})
        assert _edit_snapshot(journey["id"], saved["activity"]["id"]) == before
        elevation = _metric(preview, "elevation")
        distance = _metric(preview, "distance")
        assert elevation["current_m"] == _feet("50")
        assert elevation["existing_credit_m"] == _feet("100")
        assert elevation["credit_m"] == _feet("100")
        assert elevation["resulting_m"] == _feet("150")
        assert distance["current_m"] == _miles("0.25")
        assert distance["existing_credit_m"] == _miles("0.5")
        assert distance["credit_m"] == _miles("1")
        assert distance["resulting_m"] == _miles("1.25")
        _assert_preview_baseline(preview)


def test_edits_recalculate_each_metric_and_the_progress_version(app):
    with app.app_context():
        user_id = _user("Edit Logger")
        route_id, journey_id = _started_dual(user_id, "life-edit")
        expedition_id = _expedition("edit-club", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey_id)
        saved = create_activity(user_id, _fields(elevation="100", distance="0.5"))
        activity_id = saved["activity"]["id"]
        version = _journey_row(journey_id)["progress_version"]
        events = len(_event_kinds(user_id))

        raised = update_activity(user_id, activity_id, {"elevation_ft": "200"})
        assert _credit_map(raised)["elevation"] == _feet("200")
        assert _credit_map(raised)["distance"] == _miles("0.5")
        assert _journey_row(journey_id)["progress_version"] == version + 1

        lowered = update_activity(user_id, activity_id, {"distance_mi": "0.25"})
        assert _credit_map(lowered)["distance"] == _miles("0.25")
        assert _journey_row(journey_id)["progress_version"] == version + 2

        both = update_activity(
            user_id,
            activity_id,
            {"elevation_ft": "80", "distance_mi": "0.4"},
        )
        assert _credit_map(both) == {
            "distance": _miles("0.4"),
            "elevation": _feet("80"),
        }
        noted = update_activity(user_id, activity_id, {"note": "clear morning"})
        assert noted["activity"]["note"] == "clear morning"
        assert _journey_row(journey_id)["progress_version"] == version + 3
        assert len(_event_kinds(user_id)) == events

        capped = update_activity(user_id, activity_id, {"elevation_ft": "1000"})
        assert _credit_map(capped)["elevation"] == _feet("1000")
        held = _journey_row(journey_id)["progress_version"]
        taller = update_activity(user_id, activity_id, {"elevation_ft": "1200"})
        assert taller["activity"]["elevation_gain_m"] == _feet("1200")
        assert _credit_map(taller)["elevation"] == _feet("1000")
        assert _journey_row(journey_id)["progress_version"] == held + 1

        cleared = update_activity(user_id, activity_id, {"elevation_ft": "0", "distance_mi": None})
        assert cleared["credits"] == []
        assert _assignment_count(activity_id) == 0
        assert _totals(journey_id) == {}
        assert _journey_row(journey_id)["progress_version"] == held + 2

        regained = update_activity(user_id, activity_id, {"distance_mi": "0.5"})
        assert _credit_map(regained) == {"distance": _miles("0.5")}
        assert _assignment_count(activity_id) == 1
        assert len(_event_kinds(user_id)) == events


def test_an_edit_that_finishes_the_journey_writes_only_the_summit(app):
    with app.app_context():
        user_id = _user("Edit Summit")
        route_id = _route(
            "life-edit-summit",
            {"elevation": _feet("100")},
            [
                ("camp", 0, {"elevation": _feet("40")}),
                ("summit", 1, {"elevation": _feet("100")}),
            ],
        )
        journey = start_journey(user_id, route_id)
        expedition_id = _expedition("edit-summit-club", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey["id"])
        saved = create_activity(user_id, _fields(elevation="40"))
        assert _event_kinds(user_id) == ["activity_logged", "checkpoint_reached"]
        fields = {"elevation_ft": "100"}
        preview = preview_activity_update(user_id, saved["activity"]["id"], fields)
        assert preview["will_complete"] is True
        with pytest.raises(ActivityError) as caught:
            update_activity(user_id, saved["activity"]["id"], fields)
        assert caught.value.code == "confirmation_required"
        assert _journey_row(journey["id"])["status"] == "active"
        assert _event_kinds(user_id) == ["activity_logged", "checkpoint_reached"]
        updated = update_activity(
            user_id,
            saved["activity"]["id"],
            fields,
            confirmation_token=preview["confirmation_token"],
        )
        assert updated["journey"]["status"] == "completed"
        assert updated["journey"]["final_checkpoint_reached"] is True
        assert _event_kinds(user_id) == [
            "activity_logged",
            "checkpoint_reached",
            "summit_reached",
        ]


def test_a_paused_journey_edit_cannot_complete_the_journey(app):
    with app.app_context():
        user_id = _user("Paused Logger")
        paused_route = _route(
            "life-paused",
            {"elevation": _feet("100")},
            [("summit", 0, {"elevation": _feet("100")})],
        )
        other = _route(
            "life-other",
            {"distance": _miles("2")},
            [("summit", 0, {"distance": _miles("2")})],
        )
        paused = start_journey(user_id, paused_route)
        saved = create_activity(user_id, _fields(elevation="40"))
        switch_journey(user_id, other)
        before = _fingerprint(paused["id"])
        with pytest.raises(ActivityError) as caught:
            update_activity(user_id, saved["activity"]["id"], {"elevation_ft": "100"})
        assert caught.value.code == "resume_required"
        assert _fingerprint(paused["id"]) == before


def test_an_abandoned_journey_edit_requires_resume_before_completion(app):
    with app.app_context():
        user_id = _user("Left Logger")
        route_id = _route(
            "life-left",
            {"distance": _miles("2")},
            [("summit", 0, {"distance": _miles("2")})],
        )
        journey = start_journey(user_id, route_id)
        saved = create_activity(user_id, _fields(distance="1"))
        abandon_journey(user_id, route_id)
        before = _fingerprint(journey["id"])
        preview = preview_activity_update(user_id, saved["activity"]["id"], {"distance_mi": "2"})
        assert preview["resume_required"] is True
        assert preview["confirmation_token"] is None
        with pytest.raises(ActivityError) as caught:
            update_activity(user_id, saved["activity"]["id"], {"distance_mi": "2"})
        assert caught.value.code == "resume_required"
        assert _fingerprint(journey["id"]) == before
        assert _journey_row(journey["id"])["status"] == "abandoned"


def test_completed_progress_stays_fixed_and_other_fields_remain_editable(app):
    with app.app_context():
        user_id = _user("Done Logger")
        route_id = _route(
            "life-done",
            {"elevation": _feet("100")},
            [("summit", 0, {"elevation": _feet("100")})],
        )
        journey = start_journey(user_id, route_id)
        fields = _fields(elevation="100", distance="2")
        token = preview_activity(user_id, fields)["confirmation_token"]
        saved = create_activity(user_id, fields, confirmation_token=token)
        activity_id = saved["activity"]["id"]
        version = _journey_row(journey["id"])["progress_version"]
        with pytest.raises(ActivityError) as caught:
            update_activity(user_id, activity_id, {"elevation_ft": "80"})
        assert caught.value.code == "invalid_state"
        with pytest.raises(ActivityError) as distance:
            update_activity(user_id, activity_id, {"distance_mi": "4"})
        assert distance.value.code == "invalid_state"
        updated = update_activity(
            user_id, activity_id, {"note": "kept", "activity_type": "hike"}
        )
        assert updated["activity"]["distance_m"] == _miles("2")
        assert updated["activity"]["note"] == "kept"
        assert updated["activity"]["activity_type"] == "hike"
        assert updated["activity"]["elevation_gain_m"] == _feet("100")
        assert _journey_row(journey["id"])["progress_version"] == version
        with pytest.raises(ActivityError) as deleted:
            delete_activity(user_id, activity_id)
        assert deleted.value.code == "invalid_state"
        assert _count("activities") == 1
        with pytest.raises(psycopg.Error, match="cannot be deleted"):
            get_db().execute("DELETE FROM activities WHERE id = ?", (activity_id,))
        get_db().rollback()
        get_db().execute(
            "UPDATE users SET deletion_state = 'deleting' WHERE id = ?",
            (user_id,),
        )
        get_db().execute("SELECT set_config('adventurer.account_deletion', 'on', true)")
        get_db().execute("DELETE FROM activities WHERE id = ?", (activity_id,))
        get_db().commit()
        assert _count("activities") == 0
        assert _count("journey_credits") == 0


def test_privacy_removal_does_not_delete_a_completed_credit(app):
    with app.app_context():
        user_id = _user("Private Logger")
        route_id = _route(
            "life-private",
            {"elevation": _feet("50")},
            [("summit", 0, {"elevation": _feet("50")})],
        )
        start_journey(user_id, route_id)
        fields = _fields(elevation="50")
        token = preview_activity(user_id, fields)["confirmation_token"]
        saved = create_activity(user_id, fields, confirmation_token=token)
        get_db().execute("SELECT set_config('adventurer.privacy_removal', 'on', true)")
        get_db().execute(
            "SELECT set_config('adventurer.privacy_removal_user_id', ?, true)",
            (str(user_id),),
        )
        with pytest.raises(psycopg.Error, match="cannot be deleted"):
            get_db().execute(
                "DELETE FROM activities WHERE id = ?",
                (saved["activity"]["id"],),
            )
        get_db().rollback()
        assert _count("activities") == 1


def test_deletion_removes_one_or_both_credits_and_bumps_the_version(app):
    with app.app_context():
        user_id = _user("Delete Logger")
        route_id, journey_id = _started_dual(user_id, "life-delete")
        plain = create_activity(user_id, {"elevation_ft": None, "distance_mi": "0", "happened_on": DAY})
        assert delete_activity(user_id, plain["activity"]["id"])["journey"] is None
        assert _count("activities") == 0

        one = create_activity(user_id, _fields(elevation="100"))
        version = _journey_row(journey_id)["progress_version"]
        delete_activity(user_id, one["activity"]["id"])
        assert _totals(journey_id) == {}
        assert _journey_row(journey_id)["progress_version"] == version + 1
        assert _event_kinds(user_id) == []

        both = create_activity(user_id, _fields(elevation="100", distance="0.5"))
        assert len(_credit_map(both)) == 2
        version = _journey_row(journey_id)["progress_version"]
        delete_activity(user_id, both["activity"]["id"])
        assert _totals(journey_id) == {}
        assert _assignment_count(both["activity"]["id"]) == 0
        assert _journey_row(journey_id)["progress_version"] == version + 1
        assert _journey_row(journey_id)["status"] == "active"


def test_new_checkpoints_are_not_announced_twice(app):
    with app.app_context():
        user_id = _user("Camp Logger")
        route_id = _route(
            "life-camps",
            {"elevation": _feet("100")},
            [
                ("camp", 0, {"elevation": _feet("40")}),
                ("summit", 1, {"elevation": _feet("100")}),
            ],
        )
        journey = start_journey(user_id, route_id)
        expedition_id = _expedition("camp-club", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey["id"])
        withdrawn = _expedition("camp-left", route_id, user_id)
        closed = _expedition("camp-closed", route_id, user_id, status="archived")
        _participation(user_id, withdrawn, route_id, journey["id"], status="withdrawn")
        _participation(user_id, closed, route_id, journey["id"])
        create_activity(user_id, _fields(elevation="50"))
        assert _event_kinds(user_id) == ["activity_logged", "checkpoint_reached"]
        assert _event_amounts(user_id, "checkpoint_reached")[0]["elevation"] == _feet("50")
        fields = _fields(elevation="50")
        token = preview_activity(user_id, fields)["confirmation_token"]
        create_activity(user_id, fields, confirmation_token=token)
        assert _event_kinds(user_id) == [
            "activity_logged",
            "checkpoint_reached",
            "activity_logged",
            "checkpoint_reached",
            "summit_reached",
        ]
        names = [
            row["checkpoint_name"]
            for row in _events(user_id)
            if row["kind"] == "checkpoint_reached"
        ]
        assert names == ["camp", "summit"]


def test_create_locks_expeditions_before_the_journey_and_the_activity(app, monkeypatch):
    with app.app_context():
        user_id = _user("Lock Logger")
        route_id = _route(
            "life-lock",
            {"elevation": _feet("100")},
            [("summit", 0, {"elevation": _feet("100")})],
        )
        journey = start_journey(user_id, route_id)
        expedition_id = _expedition("lock-club", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey["id"])
        seen: list[str] = []
        original = DB.execute

        def wrapped(self, sql, params=()):
            normalized = " ".join(sql.split()).lower()
            if "for update" in normalized:
                seen.append(normalized)
            return original(self, sql, params)

        monkeypatch.setattr(DB, "execute", wrapped)
        create_activity(user_id, _fields(elevation="10"))
        def position(fragment: str) -> int:
            for index, statement in enumerate(seen):
                if fragment in statement:
                    return index
            raise AssertionError(fragment)

        assert position("from users") < position("from expeditions")
        assert position("from expeditions") < position("from route_journeys")
        assert position("from route_journeys") < position("for update of participation")
        assert all("journey_metric_totals" not in statement for statement in seen)
        assert all("pg_advisory" not in statement for statement in seen)


def test_concurrent_creates_cannot_over_credit(app):
    with app.app_context():
        user_id = _user("Race Logger")
        route_id = _route(
            "life-race",
            {"elevation": _feet("1000")},
            [("summit", 0, {"elevation": _feet("1000")})],
        )
        journey = start_journey(user_id, route_id)
    outcome = {"ok": 0, "codes": [], "errors": []}
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def run() -> None:
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                create_activity(user_id, _fields(elevation="600"))
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
        assert _totals(journey["id"]) == {"elevation": _feet("600")}
        assert _journey_row(journey["id"])["status"] == "active"


def test_concurrent_edits_cannot_over_credit(app):
    with app.app_context():
        user_id = _user("Edit Race")
        route_id = _route(
            "life-edit-race",
            {"distance": _miles("10")},
            [("summit", 0, {"distance": _miles("10")})],
        )
        journey = start_journey(user_id, route_id)
        first = create_activity(user_id, _fields(distance="2"))
        second = create_activity(user_id, _fields(distance="2"))
    outcome = {"ok": 0, "codes": [], "errors": []}
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def run(activity_id: int) -> None:
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                update_activity(user_id, activity_id, {"distance_mi": "9"})
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

    threads = [
        threading.Thread(target=run, args=(first["activity"]["id"],)),
        threading.Thread(target=run, args=(second["activity"]["id"],)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    with app.app_context():
        assert outcome["errors"] == []
        assert outcome["ok"] == 0
        assert outcome["codes"] == ["confirmation_required", "confirmation_required"]
        assert _totals(journey["id"]) == {"distance": _miles("4")}
        assert _journey_row(journey["id"])["status"] == "active"


def test_concurrent_summit_attempts_emit_one_summit(app):
    with app.app_context():
        user_id = _user("Summit Race")
        route_id = _route(
            "life-summit-race",
            {"elevation": _feet("100")},
            [("summit", 0, {"elevation": _feet("100")})],
        )
        journey = start_journey(user_id, route_id)
        expedition_id = _expedition("summit-race-club", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey["id"])
        fields = _fields(elevation="100")
        token = preview_activity(user_id, fields)["confirmation_token"]
    outcome = {"ok": 0, "codes": [], "errors": []}
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def run() -> None:
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                create_activity(user_id, fields, confirmation_token=token)
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
        assert _journey_row(journey["id"])["status"] == "completed"
        assert _count("activities") == 1
        assert _event_kinds(user_id).count("summit_reached") == 1


def test_mixed_timezone_times_are_rejected(app):
    with app.app_context():
        user_id = _user("Time Logger")
        aware = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
        naive = datetime(2026, 6, 1, 13)
        with pytest.raises(ActivityError, match="timezone"):
            create_activity(
                user_id,
                _fields(elevation="1", started_at=aware, ended_at=naive),
            )


def test_starting_a_journey_does_not_credit_an_older_activity(app):
    with app.app_context():
        user_id = _user("Before Start")
        route_id = _route(
            "life-before",
            {"elevation": _feet("1000")},
            [("summit", 0, {"elevation": _feet("1000")})],
        )
        saved = create_activity(user_id, _fields(elevation="125"))
        journey = start_journey(user_id, route_id)
        assert saved["credits"] == []
        assert _assignment_count(saved["activity"]["id"]) == 0
        assert _totals(journey["id"]) == {}
        assert _journey_row(journey["id"])["progress_version"] == 1
        assert get_activity(user_id, saved["activity"]["id"])["credit"] is None


def test_deleting_a_paused_or_abandoned_credit_leaves_that_status(app):
    with app.app_context():
        user_id = _user("Pause Delete")
        paused_route = _route(
            "life-pause-delete",
            {"elevation": _feet("1000")},
            [("summit", 0, {"elevation": _feet("1000")})],
        )
        other_route = _route(
            "life-pause-other",
            {"elevation": _feet("1000")},
            [("summit", 0, {"elevation": _feet("1000")})],
        )
        paused = start_journey(user_id, paused_route)
        paused_activity = create_activity(user_id, _fields(elevation="300"))
        switch_journey(user_id, other_route)
        version = _journey_row(paused["id"])["progress_version"]
        removed = delete_activity(user_id, paused_activity["activity"]["id"])
        assert removed["journey"]["id"] == paused["id"]
        assert removed["journey"]["status"] == "paused"
        assert _totals(paused["id"]) == {}
        assert _journey_row(paused["id"])["progress_version"] == version + 1
        assert _journey_row(paused["id"])["status"] == "paused"

        switch_journey(user_id, paused_route)
        abandoned_activity = create_activity(user_id, _fields(elevation="120"))
        abandoned = abandon_journey(user_id, paused_route)
        version = _journey_row(abandoned["id"])["progress_version"]
        removed = delete_activity(user_id, abandoned_activity["activity"]["id"])
        assert removed["journey"]["id"] == abandoned["id"]
        assert removed["journey"]["status"] == "abandoned"
        assert _totals(abandoned["id"]) == {}
        assert _journey_row(abandoned["id"])["progress_version"] == version + 1
        assert _journey_row(abandoned["id"])["status"] == "abandoned"


def test_a_paused_participation_receives_no_feed_row(app):
    with app.app_context():
        user_id = _user("Feed Split")
        paused_route = _route(
            "life-feed-paused",
            {"elevation": _feet("2000")},
            [("summit", 0, {"elevation": _feet("2000")})],
        )
        active_route = _route(
            "life-feed-active",
            {"elevation": _feet("2000")},
            [
                ("gate", 0, {"elevation": _feet("100")}),
                ("summit", 1, {"elevation": _feet("2000")}),
            ],
        )
        paused = start_journey(user_id, paused_route)
        paused_expedition = _expedition("life-feed-paused-club", paused_route, user_id)
        _participation(user_id, paused_expedition, paused_route, paused["id"])
        switch_journey(user_id, active_route)
        active = get_db().execute(
            """
            SELECT id FROM route_journeys
            WHERE user_id = ? AND status = 'active'
            """,
            (user_id,),
        ).fetchone()["id"]
        active_expedition = _expedition("life-feed-active-club", active_route, user_id)
        _participation(user_id, active_expedition, active_route, active)
        saved = create_activity(user_id, _fields(elevation="150"))
        rows = get_db().execute(
            """
            SELECT expedition_id, kind, checkpoint_name
            FROM expedition_updates
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()
        assert [row["expedition_id"] for row in rows] == [active_expedition, active_expedition]
        assert rows[0]["kind"] == "activity_logged"
        assert rows[1]["kind"] == "checkpoint_reached"
        assert rows[1]["checkpoint_name"] == "gate"
        assert _event_amounts(user_id, "activity_logged") == [{"elevation": _feet("150")}]
        assert saved["journey"]["id"] == active
        assert _journey_row(paused["id"])["status"] == "paused"
        assert _totals(paused["id"]) == {}


def test_a_feed_insert_failure_rolls_back_the_activity(app, monkeypatch):
    with app.app_context():
        user_id = _user("Feed Fail")
        route_id = _route(
            "life-feed-fail",
            {"elevation": _feet("1000")},
            [("summit", 0, {"elevation": _feet("1000")})],
        )
        journey = start_journey(user_id, route_id)
        expedition_id = _expedition("life-feed-fail-club", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey["id"])
        database = get_db()
        original = database.execute

        def fail_feed(sql, params=()):
            if "INSERT INTO expedition_updates" in sql:
                raise RuntimeError("feed failed")
            return original(sql, params)

        monkeypatch.setattr(database, "execute", fail_feed)
        with pytest.raises(RuntimeError, match="feed failed"):
            create_activity(user_id, _fields(elevation="100"))
        monkeypatch.undo()
        assert _count("activities") == 0
        assert _count("journey_credits") == 0
        assert _count("expedition_updates") == 0
        assert _journey_row(journey["id"])["progress_version"] == 1
        saved = create_activity(user_id, _fields(elevation="100"))
        assert _credit_map(saved) == {"elevation": _feet("100")}
        assert _event_kinds(user_id) == ["activity_logged"]


def test_personal_history_is_owner_scoped(app):
    with app.app_context():
        owner = _user("Owner Logger")
        other = _user("Other Logger")
        route_id = _route(
            "life-owner",
            {"elevation": _feet("1000")},
            [("summit", 0, {"elevation": _feet("1000")})],
        )
        journey = start_journey(owner, route_id)
        older = create_activity(
            owner,
            _fields(elevation="100", happened_on=date(2024, 6, 1), note="older"),
        )
        newer = create_activity(
            owner,
            _fields(elevation="50", happened_on=date(2024, 6, 3), note="newer"),
        )
        same_day = create_activity(
            owner,
            _fields(elevation="25", happened_on=date(2024, 6, 3), note="later"),
        )
        create_activity(other, _fields(elevation="70", note="private"))
        history = list_activities(owner)
        assert [row["note"] for row in history] == ["later", "newer", "older"]
        assert history[0]["elevation_gain_m"] == _feet("25")
        assert history[2]["credit"]["metrics"] == [
            {"metric": "elevation", "credited_m": _feet("100")}
        ]
        assert history[2]["credit"]["journey_id"] == journey["id"]
        assert history[2]["credit"]["journey_status"] == "active"
        assert [row["note"] for row in list_activities(other)] == ["private"]
        assert all(row["id"] != older["activity"]["id"] for row in list_activities(other))
        for call in (
            lambda: get_activity(other, newer["activity"]["id"]),
            lambda: preview_activity_update(other, newer["activity"]["id"], {"note": "no"}),
            lambda: update_activity(other, newer["activity"]["id"], {"note": "no"}),
            lambda: delete_activity(other, newer["activity"]["id"]),
        ):
            with pytest.raises(ActivityError) as caught:
                call()
            assert caught.value.code == "forbidden"
            assert caught.value.status == 403
        assert get_activity(owner, newer["activity"]["id"])["note"] == "newer"
        assert same_day["activity"]["id"] in {row["id"] for row in history}
        other_preview = preview_activity(other, _fields(elevation="10"))
        assert other_preview["journey"] is None
        assert other_preview["saved_without_credit"] is True


def test_a_deleting_account_cannot_write_activities(app):
    with app.app_context():
        user_id = _user("Deleting Logger")
        route_id = _route(
            "life-deleting",
            {"elevation": _feet("1000")},
            [("summit", 0, {"elevation": _feet("1000")})],
        )
        journey = start_journey(user_id, route_id)
        saved = create_activity(user_id, _fields(elevation="100"))
        get_db().execute(
            "UPDATE users SET deletion_state = 'deleting' WHERE id = ?",
            (user_id,),
        )
        get_db().commit()
        for call in (
            lambda: preview_activity(user_id, _fields(elevation="10")),
            lambda: create_activity(user_id, _fields(elevation="10")),
            lambda: update_activity(user_id, saved["activity"]["id"], {"note": "no"}),
            lambda: delete_activity(user_id, saved["activity"]["id"]),
        ):
            with pytest.raises(ActivityError) as caught:
                call()
            assert caught.value.code == "forbidden"
            assert caught.value.status == 403
        assert _count("activities") == 1
        assert _journey_row(journey["id"])["progress_version"] == 2
        with pytest.raises(ActivityError) as caught:
            create_activity(999_999, _fields(elevation="10"))
        assert caught.value.code == "not_found"
        assert caught.value.status == 404


def test_an_activity_racing_a_switch_credits_the_journey_that_wins(app, monkeypatch):
    with app.app_context():
        user_id = _user("Race Switch")
        original_route = _route(
            "life-race-switch",
            {"elevation": _feet("2000")},
            [("summit", 0, {"elevation": _feet("2000")})],
        )
        next_route = _route(
            "life-race-next",
            {"elevation": _feet("2000")},
            [("summit", 0, {"elevation": _feet("2000")})],
        )
        original = start_journey(user_id, original_route)
        switched = _race_activity_against(
            app,
            monkeypatch,
            lambda: switch_journey(user_id, next_route),
            lambda: create_activity(user_id, _fields(elevation="100")),
        )
        assert "error" not in switched
        assert switched["result"]["journey"]["id"] == switched["other"]["id"]
        assert _credit_map(switched["result"]) == {"elevation": _feet("100")}
        assert _totals(original["id"]) == {}
        assert _journey_row(original["id"])["status"] == "paused"
        assert _totals(switched["other"]["id"]) == {"elevation": _feet("100")}


def test_an_activity_racing_a_restart_credits_the_new_attempt(app, monkeypatch):
    with app.app_context():
        user_id = _user("Race Restart")
        route_id = _route(
            "life-race-restart",
            {"elevation": _feet("1000")},
            [("summit", 0, {"elevation": _feet("1000")})],
        )
        original = start_journey(user_id, route_id)
        raced = _race_activity_against(
            app,
            monkeypatch,
            lambda: restart_journey(user_id, route_id),
            lambda: create_activity(user_id, _fields(elevation="80")),
        )
        assert "error" not in raced
        new_id = raced["other"]["journey"]["id"]
        assert raced["result"]["journey"]["id"] == new_id
        assert new_id != original["id"]
        assert _credit_map(raced["result"]) == {"elevation": _feet("80")}
        assert _totals(original["id"]) == {}
        assert _journey_row(original["id"])["status"] == "abandoned"
        assert _totals(new_id) == {"elevation": _feet("80")}


def _race_activity_against(app, monkeypatch, other_write, activity_write) -> dict:
    """Let the other write commit before the activity takes its user lock."""
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
    holder: dict = {}

    def run() -> None:
        with app.app_context():
            try:
                holder["result"] = activity_write()
            except Exception as exc:
                holder["error"] = exc
            finally:
                get_db().close()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert reached.wait(timeout=10)
        holder["other"] = other_write()
    finally:
        allow.set()
        thread.join(timeout=20)
        monkeypatch.undo()
    assert not thread.is_alive()
    return holder


def _fields(**extra) -> dict:
    payload = {"happened_on": DAY}
    if "elevation" in extra:
        payload["elevation_ft"] = extra.pop("elevation")
    if "distance" in extra:
        payload["distance_mi"] = extra.pop("distance")
    payload.update(extra)
    return payload


def _metric(preview: dict, code: str) -> dict:
    return next(item for item in preview["metrics"] if item["metric"] == code)


def _assert_preview_baseline(preview: dict) -> None:
    """Every reported amount for a metric comes from one replacement baseline."""
    stored = {item["metric"]: item["credited_m"] for item in preview["journey"]["metrics"]}
    for metric in preview["metrics"]:
        current = metric["current_m"]
        remaining = metric["remaining_m"]
        credit = metric["credit_m"]
        resulting = metric["resulting_m"]
        target = metric["target_m"]
        measurement = metric["measurement_m"]
        assert current + metric["existing_credit_m"] == stored[metric["metric"]]
        assert remaining == (target - current if target > current else Decimal("0"))
        assert credit == _portion(measurement, target, current)
        assert resulting == current + credit
        assert resulting <= target


def _edit_snapshot(journey_id: int, activity_id: int) -> tuple:
    activity = get_db().execute(
        """
        SELECT elevation_gain_m, distance_m, note
        FROM activities
        WHERE id = ?
        """,
        (activity_id,),
    ).fetchone()
    credits = tuple(
        (row["metric_code"], row["credited_amount"])
        for row in get_db().execute(
            """
            SELECT credit.metric_code, credit.credited_amount
            FROM journey_credits AS credit
            JOIN activity_journey_assignments AS assignment
                ON assignment.id = credit.assignment_id
            WHERE assignment.activity_id = ?
            ORDER BY credit.metric_code
            """,
            (activity_id,),
        ).fetchall()
    )
    return (_fingerprint(journey_id), dict(activity), credits)


def _credit_map(saved: dict) -> dict[str, Decimal]:
    return {item["metric"]: item["credited_m"] for item in saved["credits"]}


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
    for name, position, thresholds in checkpoints:
        checkpoint_id = database.execute(
            """
            INSERT INTO route_checkpoints (route_id, slug, name, position)
            VALUES (?, ?, ?, ?)
            RETURNING id
            """,
            (route_id, name, name, position),
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


def _started_dual(user_id: int, slug: str) -> tuple[int, int]:
    route_id = _route(
        slug,
        {"elevation": _feet("1000"), "distance": _miles("2")},
        [
            ("camp", 0, {"elevation": _feet("250"), "distance": _miles("1")}),
            ("summit", 1, {"elevation": _feet("1000"), "distance": _miles("2")}),
        ],
    )
    return route_id, start_journey(user_id, route_id)["id"]


def _expedition(slug: str, route_id: int, owner_id: int, status: str = "active") -> int:
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


def _participation(
    user_id: int,
    expedition_id: int,
    route_id: int,
    journey_id: int,
    status: str = "active",
) -> None:
    database = get_db()
    database.execute(
        """
        INSERT INTO expedition_participations (
            user_id, expedition_id, route_id, journey_id, status
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, expedition_id, route_id, journey_id, status),
    )
    database.commit()


def _journey_row(journey_id: int) -> dict:
    return dict(
        get_db().execute(
            """
            SELECT id, status, progress_version
            FROM route_journeys
            WHERE id = ?
            """,
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
            ORDER BY metric_code
            """,
            (journey_id,),
        ).fetchall()
    }


def _assignment_count(activity_id: int) -> int:
    return get_db().execute(
        """
        SELECT COUNT(*) AS n
        FROM activity_journey_assignments
        WHERE activity_id = ?
        """,
        (activity_id,),
    ).fetchone()["n"]


def _count(table: str) -> int:
    if table not in {"activities", "journey_credits", "activity_journey_assignments", "expedition_updates"}:
        raise AssertionError(table)
    return get_db().execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _events(user_id: int) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT feed.id, feed.kind, feed.checkpoint_name, feed.activity_id,
               metric.metric_code, metric.credited_amount
        FROM expedition_updates AS feed
        LEFT JOIN expedition_update_metrics AS metric ON metric.update_id = feed.id
        WHERE feed.user_id = ?
        ORDER BY feed.id, metric.metric_code
        """,
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _event_kinds(user_id: int) -> list[str]:
    seen: list[str] = []
    last = None
    for row in _events(user_id):
        if row["id"] != last:
            seen.append(row["kind"])
            last = row["id"]
    return seen


def _event_amounts(user_id: int, kind: str) -> list[dict[str, Decimal]]:
    grouped: list[dict[str, Decimal]] = []
    current_id = None
    current: dict[str, Decimal] = {}
    for row in _events(user_id):
        if row["kind"] != kind:
            continue
        if row["id"] != current_id:
            if current:
                grouped.append(current)
            current_id = row["id"]
            current = {}
        if row["metric_code"] is not None:
            current[row["metric_code"]] = row["credited_amount"]
    if current:
        grouped.append(current)
    return grouped


def _fingerprint(journey_id: int) -> tuple:
    row = _journey_row(journey_id)
    credits = get_db().execute(
        """
        SELECT COALESCE(SUM(credited_amount), 0) AS total, COUNT(*) AS n
        FROM journey_credits
        WHERE journey_id = ?
        """,
        (journey_id,),
    ).fetchone()
    return (row["status"], row["progress_version"], credits["n"], credits["total"], _count("activities"), _count("expedition_updates"))


def _assert_unchanged(user_id: int, journey_id: int, *, activities: int) -> None:
    assert _count("activities") == activities
    assert _count("activity_journey_assignments") == 0 if activities == 0 else _count("activity_journey_assignments") >= 0
    if activities == 0:
        assert _count("journey_credits") == 0
        assert _count("expedition_updates") == 0
        assert _journey_row(journey_id)["progress_version"] == 1
        assert _journey_row(journey_id)["status"] == "active"
    assert _event_kinds(user_id) == [] or activities
