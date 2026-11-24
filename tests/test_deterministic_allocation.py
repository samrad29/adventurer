"""Release 1B allocation, origin integrity, preflight, and concurrency.

Runs against the isolated PostgreSQL database named by TEST_DATABASE_URL.
Restricted policies and route recommendations are not part of this release.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
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
from adventurer.allocation import (
    AllocationCutoverError,
    MeasuredAssignment,
    allocate,
    preflight_report,
    require_allocation_cutover,
)
from adventurer.db import get_db
from adventurer.expeditions import (
    accept_invitation,
    archive_private_expedition,
    create_private_expedition,
    create_share_link,
    get_leaderboard,
    remove_participation_privacy,
)
from adventurer.journey_progress import read_journey_progress
from adventurer.journeys import abandon_journey, restart_journey, start_journey, switch_journey
from tests.conftest import ROOT, TEST_URL
from tests.test_activity_foundations import (
    _assignment_count,
    _assignment_journey,
    _credit_map,
    _feet,
    _fields,
    _journey_row,
    _miles,
    _origin_row,
    _route,
    _totals,
    _user,
    get_saved,
)
from tests.test_activity_lifecycle import (
    _event_kinds,
    _expedition,
    _participation,
    _route as _route_with_camps,
)

DAY_TARGETS = {"elevation": _feet("1000"), "distance": _miles("50")}


def _open(user_id: int, slug: str) -> tuple[int, int]:
    route_id = _route(slug, DAY_TARGETS)
    journey_id = start_journey(user_id, route_id)["id"]
    return route_id, journey_id


def _standing(owner_id: int, slug: str, member_id: int, metric: str) -> Decimal:
    board = get_leaderboard(owner_id, slug)
    entry = next(row for row in board["entries"] if row["user_id"] == member_id)
    item = next(row for row in entry["metrics"] if row["metric"] == metric)
    if "total_m" in item:
        return item["total_m"]
    return item["credited_m"]


def _snapshot_amount(slug: str, metric: str) -> Decimal | None:
    row = get_db().execute(
        """
        SELECT metric.total_amount
        FROM expedition_result_metrics AS metric
        JOIN expedition_result_entries AS entry
          ON entry.id = metric.result_entry_id
        JOIN expeditions AS expedition ON expedition.id = entry.expedition_id
        WHERE expedition.slug = ? AND metric.metric_code = ?
        """,
        (slug, metric),
    ).fetchone()
    if row is None:
        return None
    return row["total_amount"]


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["TEST_DATABASE_URL"] = TEST_URL
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_walk_orders_by_assignment_then_skips_empty_measurements():
    early = MeasuredAssignment(1, 1, 1, False, "hike", Decimal("10"), None)
    later = MeasuredAssignment(2, 2, 2, False, "cycle", None, Decimal("4"))
    empty = MeasuredAssignment(3, 3, 3, False, "run", Decimal("0"), Decimal("0"))
    shares = allocate(
        [later, empty, early],
        [("elevation", Decimal("6")), ("distance", Decimal("9"))],
    )
    assert shares[1] == {"elevation": Decimal("6")}
    assert shares[2] == {"distance": Decimal("4")}
    assert 3 not in shares


def test_deleting_an_earlier_activity_refills_the_later_one(app):
    with app.app_context():
        user_id = _user("Delete Earlier")
        _route_id, journey_id = _open(user_id, "alloc-delete-a")
        first = create_activity(user_id, _fields(elevation="1000"))
        second = create_activity(user_id, _fields(elevation="500"))
        assert _credit_map(second) == {}
        assert second["no_credit_reason"] == "metric_already_complete"
        detail = get_activity(user_id, second["activity"]["id"])
        assert detail["association"]["journey_id"] == journey_id
        assert detail["credit"] is None
        delete_activity(user_id, first["activity"]["id"])
        assert _credit_map(get_saved(user_id, second["activity"]["id"])) == {
            "elevation": _feet("500")
        }
        assert _totals(journey_id)["elevation"] == _feet("500")
        assert _journey_row(journey_id)["status"] == "active"


def test_reducing_an_earlier_activity_frees_or_shares_the_cap(app):
    with app.app_context():
        user_id = _user("Reduce Earlier")
        _route_id, journey_id = _open(user_id, "alloc-reduce-a")
        first = create_activity(user_id, _fields(elevation="1000"))
        second = create_activity(user_id, _fields(elevation="500"))
        update_activity(user_id, first["activity"]["id"], {"elevation_ft": "200"})
        assert _credit_map(get_saved(user_id, first["activity"]["id"]))["elevation"] == _feet("200")
        assert _credit_map(get_saved(user_id, second["activity"]["id"]))["elevation"] == _feet("500")
        assert _totals(journey_id)["elevation"] == _feet("700")

        user_two = _user("Share Earlier")
        _route_two, journey_two = _open(user_two, "alloc-share-a")
        earlier = create_activity(user_two, _fields(elevation="1000"))
        later = create_activity(user_two, _fields(elevation="800"))
        update_activity(user_two, earlier["activity"]["id"], {"elevation_ft": "500"})
        assert _credit_map(get_saved(user_two, earlier["activity"]["id"]))["elevation"] == _feet("500")
        assert _credit_map(get_saved(user_two, later["activity"]["id"]))["elevation"] == _feet("500")
        assert _totals(journey_two)["elevation"] == _feet("1000")
        assert _journey_row(journey_two)["status"] == "active"


def test_deleting_a_later_activity_leaves_the_capped_total(app):
    with app.app_context():
        user_id = _user("Delete Later")
        _route_id, journey_id = _open(user_id, "alloc-delete-b")
        first = create_activity(user_id, _fields(elevation="1000"))
        second = create_activity(user_id, _fields(elevation="500"))
        version = _journey_row(journey_id)["progress_version"]
        delete_activity(user_id, second["activity"]["id"])
        assert _credit_map(get_saved(user_id, first["activity"]["id"]))["elevation"] == _feet("1000")
        assert _totals(journey_id)["elevation"] == _feet("1000")
        assert _journey_row(journey_id)["progress_version"] == version + 1
        assert _journey_row(journey_id)["status"] == "active"


def test_growing_an_earlier_activity_takes_capacity_from_later_ones(app):
    with app.app_context():
        user_id = _user("Grow Earlier")
        _route_id, journey_id = _open(user_id, "alloc-grow")
        first = create_activity(user_id, _fields(elevation="600"))
        second = create_activity(user_id, _fields(elevation="400"))
        version = _journey_row(journey_id)["progress_version"]
        fields = _fields(distance="50")
        preview = preview_activity(user_id, fields)
        assert preview["will_complete"] is True
        update_activity(user_id, first["activity"]["id"], {"elevation_ft": "800"})
        assert _credit_map(get_saved(user_id, first["activity"]["id"]))["elevation"] == _feet("800")
        assert _credit_map(get_saved(user_id, second["activity"]["id"]))["elevation"] == _feet("200")
        assert _totals(journey_id)["elevation"] == _feet("1000")
        assert _journey_row(journey_id)["progress_version"] == version + 1
        with pytest.raises(ActivityError) as caught:
            create_activity(user_id, fields, confirmation_token=preview["confirmation_token"])
        assert caught.value.code == "confirmation_required"
        assert _journey_row(journey_id)["status"] == "active"


def test_metadata_and_identical_measurements_do_not_recalculate(app):
    with app.app_context():
        user_id = _user("Quiet Edit")
        route_id, journey_id = _open(user_id, "alloc-quiet")
        expedition_id = _expedition("alloc-quiet-exp", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey_id)
        saved = create_activity(user_id, _fields(elevation="100", distance="1"))
        version = _journey_row(journey_id)["progress_version"]
        credits = _credit_map(saved)
        totals = dict(_totals(journey_id))
        feed = _event_kinds(user_id)
        noted = update_activity(
            user_id,
            saved["activity"]["id"],
            {"note": "clear", "happened_on": "2026-06-02", "duration_s": 90},
        )
        assert noted["activity"]["note"] == "clear"
        assert _credit_map(get_saved(user_id, saved["activity"]["id"])) == credits
        assert _totals(journey_id) == totals
        assert _journey_row(journey_id)["progress_version"] == version
        assert _event_kinds(user_id) == feed
        same = update_activity(user_id, saved["activity"]["id"], {"elevation_ft": "100"})
        assert same["activity"]["elevation_gain_m"] == _feet("100")
        assert _journey_row(journey_id)["progress_version"] == version
        typed = update_activity(
            user_id, saved["activity"]["id"], {"activity_type": "cycle"}
        )
        assert typed["activity"]["activity_type"] == "cycle"
        assert _journey_row(journey_id)["progress_version"] == version
        assert _event_kinds(user_id) == feed


def test_clearing_measurements_keeps_the_assignment_and_its_origin(app):
    with app.app_context():
        user_id = _user("Clear Logger")
        _route_id, journey_id = _open(user_id, "alloc-clear")
        saved = create_activity(user_id, _fields(elevation="100", distance="1"))
        origin = _origin_row(saved["activity"]["id"], "elevation")["credited_at"]
        update_activity(
            user_id,
            saved["activity"]["id"],
            {"elevation_ft": "0", "distance_mi": None},
        )
        assert _assignment_count(saved["activity"]["id"]) == 1
        assert get_activity(user_id, saved["activity"]["id"])["credit"] is None
        assert get_activity(user_id, saved["activity"]["id"])["association"] is not None
        assert _origin_row(saved["activity"]["id"], "elevation")["credited_at"] == origin
        assert _totals(journey_id) == {}
        zero_rows = get_db().execute(
            "SELECT COUNT(*) AS n FROM journey_credits WHERE credited_amount <= 0"
        ).fetchone()["n"]
        assert zero_rows == 0


def test_an_elevation_edit_does_not_change_distance_credit(app):
    with app.app_context():
        user_id = _user("Split Metrics")
        _route_id, journey_id = _open(user_id, "alloc-split")
        saved = create_activity(user_id, _fields(elevation="100", distance="1"))
        update_activity(user_id, saved["activity"]["id"], {"elevation_ft": "250"})
        credits = _credit_map(get_saved(user_id, saved["activity"]["id"]))
        assert credits["elevation"] == _feet("250")
        assert credits["distance"] == _miles("1")
        assert _totals(journey_id)["distance"] == _miles("1")


def test_preview_and_save_use_the_same_walk(app):
    with app.app_context():
        user_id = _user("Same Walk")
        _route_id, journey_id = _open(user_id, "alloc-preview")
        first = create_activity(user_id, _fields(elevation="400"))
        second = create_activity(user_id, _fields(elevation="400"))
        preview = preview_activity_update(
            user_id, first["activity"]["id"], {"elevation_ft": "800"}
        )
        elevation = next(item for item in preview["metrics"] if item["metric"] == "elevation")
        assert elevation["credit_m"] == _feet("800")
        assert elevation["resulting_m"] == _feet("1000")
        assert preview["no_credit_reason"] is None
        update_activity(user_id, first["activity"]["id"], {"elevation_ft": "800"})
        assert _credit_map(get_saved(user_id, first["activity"]["id"]))["elevation"] == _feet("800")
        assert _credit_map(get_saved(user_id, second["activity"]["id"]))["elevation"] == _feet("200")
        assert _totals(journey_id)["elevation"] == elevation["resulting_m"]


def test_restart_keeps_the_restored_credit_on_the_original_attempt(app):
    with app.app_context():
        user_id = _user("Restart Logger")
        route_id, journey_id = _open(user_id, "alloc-restart")
        saved = create_activity(user_id, _fields(elevation="100"))
        restarted = restart_journey(user_id, route_id)
        update_activity(user_id, saved["activity"]["id"], {"elevation_ft": "180"})
        assert _assignment_journey(saved["activity"]["id"]) == journey_id
        assert _totals(journey_id)["elevation"] == _feet("180")
        assert _totals(restarted["journey"]["id"]) == {}
        other = _route("alloc-switch-other", {"distance": _miles("3")})
        other_journey = start_journey(user_id, other)["id"]
        assert _journey_row(restarted["journey"]["id"])["status"] == "paused"
        update_activity(user_id, saved["activity"]["id"], {"elevation_ft": "220"})
        assert _assignment_journey(saved["activity"]["id"]) == journey_id
        assert _totals(journey_id)["elevation"] == _feet("220")
        assert _totals(other_journey) == {}


def test_a_completed_journey_rejects_measurement_edits_and_deletion(app):
    with app.app_context():
        user_id = _user("Frozen Logger")
        route_id = _route(
            "alloc-frozen",
            {"elevation": _feet("100"), "distance": _miles("1")},
        )
        journey_id = start_journey(user_id, route_id)["id"]
        fields = _fields(elevation="100", distance="1")
        token = preview_activity(user_id, fields)["confirmation_token"]
        saved = create_activity(user_id, fields, confirmation_token=token)
        assert saved["journey"]["status"] == "completed"
        with pytest.raises(ActivityError) as edited:
            update_activity(user_id, saved["activity"]["id"], {"elevation_ft": "80"})
        assert edited.value.code == "invalid_state"
        with pytest.raises(ActivityError) as removed:
            delete_activity(user_id, saved["activity"]["id"])
        assert removed.value.code == "invalid_state"
        assert _credit_map(get_saved(user_id, saved["activity"]["id"]))["elevation"] == _feet("100")
        assert _journey_row(journey_id)["status"] == "completed"


def test_paused_and_abandoned_recalculation_cannot_complete(app):
    with app.app_context():
        user_id = _user("Held Logger")
        route_id = _route(
            "alloc-held",
            {"elevation": _feet("100"), "distance": _miles("2")},
        )
        journey_id = start_journey(user_id, route_id)["id"]
        saved = create_activity(user_id, _fields(elevation="100", distance="1"))
        version = _journey_row(journey_id)["progress_version"]
        other = _route("alloc-held-other", {"distance": _miles("3")})
        switch_journey(user_id, other)
        assert _journey_row(journey_id)["status"] == "paused"
        with pytest.raises(ActivityError) as paused:
            update_activity(user_id, saved["activity"]["id"], {"distance_mi": "2"})
        assert paused.value.code == "resume_required"
        assert _journey_row(journey_id)["status"] == "paused"
        assert _journey_row(journey_id)["progress_version"] == version
        assert _credit_map(get_saved(user_id, saved["activity"]["id"]))["distance"] == _miles("1")
        switch_journey(user_id, route_id)
        abandon_journey(user_id, route_id)
        with pytest.raises(ActivityError) as abandoned:
            update_activity(user_id, saved["activity"]["id"], {"distance_mi": "2"})
        assert abandoned.value.code == "resume_required"
        assert _journey_row(journey_id)["status"] == "abandoned"
        assert _totals(journey_id)["distance"] == _miles("1")


def test_recalculated_totals_lock_a_checkpoint_without_a_new_feed_row(app):
    with app.app_context():
        user_id = _user("Camp Lock")
        route_id = _route_with_camps(
            "alloc-camp",
            {"elevation": _feet("10000"), "distance": _miles("20")},
            [
                ("camp", 0, {"elevation": _feet("400")}),
                (
                    "summit",
                    1,
                    {"elevation": _feet("10000"), "distance": _miles("20")},
                ),
            ],
        )
        journey_id = start_journey(user_id, route_id)["id"]
        expedition_id = _expedition("alloc-camp-exp", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey_id)
        saved = create_activity(user_id, _fields(elevation="500"))
        progress = read_journey_progress(get_db(), journey_id)
        camp = next(mark for mark in progress.checkpoints if mark.slug == "camp")
        assert camp.reached is True
        get_db().execute(
            """
            INSERT INTO journey_story_views (
                journey_id, user_id, route_id, checkpoint_id
            )
            VALUES (?, ?, ?, ?)
            """,
            (journey_id, user_id, route_id, camp.id),
        )
        get_db().commit()
        feed = _event_kinds(user_id)
        update_activity(user_id, saved["activity"]["id"], {"elevation_ft": "100"})
        progress = read_journey_progress(get_db(), journey_id)
        camp = next(mark for mark in progress.checkpoints if mark.slug == "camp")
        assert camp.reached is False
        views = get_db().execute(
            "SELECT COUNT(*) AS n FROM journey_story_views WHERE journey_id = ?",
            (journey_id,),
        ).fetchone()["n"]
        assert views == 1
        assert _event_kinds(user_id) == feed


def test_a_new_credited_activity_keeps_its_feed_and_a_correction_does_not(app):
    with app.app_context():
        user_id = _user("Feed Logger")
        route_id = _route_with_camps(
            "alloc-feed",
            {"elevation": _feet("1000"), "distance": _miles("5")},
            [
                ("camp", 0, {"elevation": _feet("100")}),
                (
                    "summit",
                    1,
                    {"elevation": _feet("1000"), "distance": _miles("5")},
                ),
            ],
        )
        journey_id = start_journey(user_id, route_id)["id"]
        expedition_id = _expedition("alloc-feed-exp", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey_id)
        create_activity(user_id, _fields(elevation="150"))
        assert _event_kinds(user_id) == ["activity_logged", "checkpoint_reached"]
        saved = create_activity(user_id, _fields(elevation="20"))
        assert _event_kinds(user_id).count("activity_logged") == 2
        update_activity(user_id, saved["activity"]["id"], {"elevation_ft": "40", "note": "later"})
        assert _event_kinds(user_id).count("activity_logged") == 2
        assert _event_kinds(user_id).count("checkpoint_reached") == 1
        update_activity(user_id, saved["activity"]["id"], {"note": "only a note"})
        delete_activity(user_id, saved["activity"]["id"])
        assert "summit_reached" not in _event_kinds(user_id)
        assert _event_kinds(user_id).count("activity_logged") == 2
        fields = _fields(elevation="1000", distance="5")
        token = preview_activity(user_id, fields)["confirmation_token"]
        create_activity(user_id, fields, confirmation_token=token)
        kinds = _event_kinds(user_id)
        assert kinds[-3:] == ["activity_logged", "checkpoint_reached", "summit_reached"]


def test_a_confirmed_correction_appends_only_the_summit(app):
    with app.app_context():
        user_id = _user("Summit Edit")
        route_id = _route(
            "alloc-summit-edit",
            {"elevation": _feet("100"), "distance": _miles("1")},
        )
        journey_id = start_journey(user_id, route_id)["id"]
        expedition_id = _expedition("alloc-summit-exp", route_id, user_id)
        _participation(user_id, expedition_id, route_id, journey_id)
        saved = create_activity(user_id, _fields(elevation="40", distance="1"))
        before = _event_kinds(user_id)
        fields = {"elevation_ft": "100"}
        preview = preview_activity_update(user_id, saved["activity"]["id"], fields)
        updated = update_activity(
            user_id,
            saved["activity"]["id"],
            fields,
            confirmation_token=preview["confirmation_token"],
        )
        assert updated["journey"]["status"] == "completed"
        assert _event_kinds(user_id) == [*before, "summit_reached"]


def test_origin_cannot_change_while_its_assignment_survives(app):
    with app.app_context():
        user_id = _user("Origin Guard")
        _route_id, _journey_id = _open(user_id, "alloc-origin")
        saved = create_activity(user_id, _fields(elevation="80"))
        origin = _origin_row(saved["activity"]["id"], "elevation")
        assignment_id = origin["assignment_id"]
        with pytest.raises(Exception, match="cannot be changed"):
            get_db().execute(
                """
                UPDATE journey_credit_origins
                SET credited_at = credited_at + interval '1 second'
                WHERE assignment_id = ?
                """,
                (assignment_id,),
            )
        get_db().rollback()
        get_db().execute(
            "DELETE FROM journey_credits WHERE assignment_id = ?",
            (assignment_id,),
        )
        get_db().commit()
        assert _origin_row(saved["activity"]["id"], "elevation") is not None
        with pytest.raises(Exception, match="cannot be deleted"):
            get_db().execute(
                "DELETE FROM journey_credit_origins WHERE assignment_id = ?",
                (assignment_id,),
            )
        get_db().rollback()
        assert _assignment_count(saved["activity"]["id"]) == 1
        assert _origin_row(saved["activity"]["id"], "elevation")["credited_at"] == origin[
            "credited_at"
        ]
        delete_activity(user_id, saved["activity"]["id"])
        remaining = get_db().execute(
            """
            SELECT COUNT(*) AS n
            FROM journey_credit_origins
            WHERE assignment_id = ?
            """,
            (assignment_id,),
        ).fetchone()["n"]
        assert remaining == 0


def test_preflight_reports_inconsistent_unfinished_journeys_and_writes_nothing(app):
    with app.app_context():
        user_id = _user("Preflight")
        _route_id, journey_id = _open(user_id, "alloc-preflight")
        create_activity(user_id, _fields(elevation="100"))
        assert require_allocation_cutover(get_db()) == []
        credit = get_db().execute(
            """
            SELECT credit.id
            FROM journey_credits AS credit
            WHERE credit.journey_id = ?
            """,
            (journey_id,),
        ).fetchone()
        get_db().execute(
            "UPDATE journey_credits SET credited_amount = ? WHERE id = ?",
            (_feet("40"), credit["id"]),
        )
        report = preflight_report(get_db())
        assert report
        assert report[0]["journey_id"] == journey_id
        assert report[0]["stored_total"] == _feet("40")
        assert report[0]["expected_total"] == _feet("100")
        with pytest.raises(AllocationCutoverError):
            require_allocation_cutover(get_db())
        assert _journey_row(journey_id)["status"] == "active"
        assert _totals(journey_id)["elevation"] == _feet("40")
        get_db().rollback()
        assert _totals(journey_id)["elevation"] == _feet("100")
        assert require_allocation_cutover(get_db()) == []


def test_privacy_rejoin_keeps_a_restored_credit_behind_the_cutoff(app):
    with app.app_context():
        owner_id = _user("Privacy Owner")
        member_id = _user("Privacy Member")
        _route("alloc-privacy", DAY_TARGETS)
        create_private_expedition(
            owner_id,
            slug="alloc-privacy-exp",
            name="Privacy",
            route_slug="alloc-privacy",
        )
        link = create_share_link(owner_id, "alloc-privacy-exp")
        accept_invitation(member_id, token=link["token"])
        early = create_activity(member_id, _fields(elevation="100", distance="1"))
        origin = _origin_row(early["activity"]["id"], "elevation")["credited_at"]
        remove_participation_privacy(member_id, "alloc-privacy-exp", confirm=True)
        rejoined = accept_invitation(member_id, token=link["token"])
        cutoff = rejoined["participation"]["display_credits_after"]
        assert origin < cutoff
        update_activity(member_id, early["activity"]["id"], {"elevation_ft": "0"})
        update_activity(member_id, early["activity"]["id"], {"elevation_ft": "80"})
        assert _origin_row(early["activity"]["id"], "elevation")["credited_at"] == origin
        journey_id = _assignment_journey(early["activity"]["id"])
        assert _totals(journey_id)["elevation"] == _feet("80")
        assert _standing(owner_id, "alloc-privacy-exp", member_id, "elevation") == 0


def test_a_pre_cutoff_capped_activity_stays_off_the_board_when_it_is_first_credited(app):
    with app.app_context():
        owner_id = _user("Hidden Owner")
        member_id = _user("Hidden Member")
        _route("alloc-hidden", DAY_TARGETS)
        create_private_expedition(
            owner_id,
            slug="alloc-hidden-exp",
            name="Hidden",
            route_slug="alloc-hidden",
        )
        second = create_private_expedition(
            owner_id,
            slug="alloc-hidden-two",
            name="Hidden Two",
            route_slug="alloc-hidden",
        )
        link = create_share_link(owner_id, "alloc-hidden-exp")
        other_link = create_share_link(owner_id, "alloc-hidden-two")
        accept_invitation(member_id, token=link["token"])
        accept_invitation(member_id, token=other_link["token"])
        first = create_activity(member_id, _fields(elevation="1000"))
        capped = create_activity(member_id, _fields(elevation="500"))
        assert _credit_map(capped) == {}
        assigned_at = get_db().execute(
            """
            SELECT created_at
            FROM activity_journey_assignments
            WHERE activity_id = ?
            """,
            (capped["activity"]["id"],),
        ).fetchone()["created_at"]
        remove_participation_privacy(member_id, "alloc-hidden-exp", confirm=True)
        rejoined = accept_invitation(member_id, token=link["token"])
        cutoff = rejoined["participation"]["display_credits_after"]
        visible = create_activity(member_id, _fields(elevation="400"))
        delete_activity(member_id, first["activity"]["id"])
        journey_id = _assignment_journey(capped["activity"]["id"])
        assert _credit_map(get_saved(member_id, capped["activity"]["id"]))["elevation"] == _feet(
            "500"
        )
        hidden_origin = _origin_row(capped["activity"]["id"], "elevation")["credited_at"]
        assert hidden_origin == assigned_at
        assert hidden_origin < cutoff
        visible_origin = _origin_row(visible["activity"]["id"], "elevation")["credited_at"]
        assert visible_origin > cutoff
        assert _totals(journey_id)["elevation"] == _feet("900")
        assert _standing(owner_id, "alloc-hidden-exp", member_id, "elevation") == _feet("400")
        assert _standing(owner_id, "alloc-hidden-two", member_id, "elevation") == _feet("900")
        assert second["journey"]["id"]
        repeated = _origin_row(capped["activity"]["id"], "elevation")["credited_at"]
        update_activity(member_id, capped["activity"]["id"], {"elevation_ft": "450"})
        update_activity(member_id, capped["activity"]["id"], {"elevation_ft": "500"})
        assert _origin_row(capped["activity"]["id"], "elevation")["credited_at"] == repeated


def test_a_closed_snapshot_keeps_the_filtered_total(app):
    with app.app_context():
        owner_id = _user("Snap Owner")
        member_id = _user("Snap Member")
        _route("alloc-snap", DAY_TARGETS)
        create_private_expedition(
            owner_id,
            slug="alloc-snap-exp",
            name="Snap",
            route_slug="alloc-snap",
        )
        link = create_share_link(owner_id, "alloc-snap-exp")
        accept_invitation(member_id, token=link["token"])
        create_activity(member_id, _fields(elevation="100"))
        remove_participation_privacy(member_id, "alloc-snap-exp", confirm=True)
        accept_invitation(member_id, token=link["token"])
        later = create_activity(member_id, _fields(elevation="200"))
        assert _standing(owner_id, "alloc-snap-exp", member_id, "elevation") == _feet("200")
        archive_private_expedition(owner_id, "alloc-snap-exp")
        frozen = _snapshot_amount("alloc-snap-exp", "elevation")
        assert frozen == _feet("200")
        update_activity(member_id, later["activity"]["id"], {"elevation_ft": "350"})
        assert _totals(_assignment_journey(later["activity"]["id"]))["elevation"] == _feet("450")
        assert _snapshot_amount("alloc-snap-exp", "elevation") == frozen
        assert _standing(owner_id, "alloc-snap-exp", member_id, "elevation") == frozen


def test_populated_upgrade_preserves_origins_credits_and_snapshots(app):
    with app.app_context():
        user_id = _user("Upgrade Logger")
        _route("alloc-upgrade", {"elevation": _feet("1000")})
        created = create_private_expedition(
            user_id,
            slug="alloc-upgrade-exp",
            name="Upgrade",
            route_slug="alloc-upgrade",
        )
        saved = create_activity(user_id, _fields(elevation="100"))
        archive_private_expedition(user_id, "alloc-upgrade-exp")
        origin = _origin_row(saved["activity"]["id"], "elevation")["credited_at"]
        credit = _credit_map(saved)["elevation"]
        snapshot = _snapshot_amount("alloc-upgrade-exp", "elevation")
        journey_id = created["journey"]["id"]
        get_db().commit()
    try:
        down = _alembic("downgrade", "0011_activity_foundations")
        assert down.returncode == 0, down.stderr
        up = _alembic("upgrade", "head")
        assert up.returncode == 0, up.stderr
    finally:
        _alembic("upgrade", "head")
    with app.app_context():
        assert _origin_row(saved["activity"]["id"], "elevation")["credited_at"] == origin
        assert _credit_map(get_saved(user_id, saved["activity"]["id"]))["elevation"] == credit
        assert _snapshot_amount("alloc-upgrade-exp", "elevation") == snapshot
        assert _journey_row(journey_id)["status"] == "active"


def test_overlapping_creation_and_edits_match_one_walk(app):
    with app.app_context():
        user_id = _user("Overlap Logger")
        _route_id, journey_id = _open(user_id, "alloc-overlap")
        first = create_activity(user_id, _fields(elevation="1000"))
        second = create_activity(user_id, _fields(elevation="500"))
        third_id: dict[str, int] = {}
        errors: list[BaseException] = []
        gate = threading.Barrier(2)

        def remove() -> None:
            try:
                with app.app_context():
                    gate.wait(timeout=8)
                    delete_activity(user_id, first["activity"]["id"])
            except BaseException as exc:
                errors.append(exc)

        def add() -> None:
            try:
                with app.app_context():
                    gate.wait(timeout=8)
                    saved = create_activity(user_id, _fields(elevation="300"))
                    third_id["id"] = saved["activity"]["id"]
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=remove), threading.Thread(target=add)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert errors == []
        assert third_id["id"]
        assert _assignment_count(first["activity"]["id"]) == 0
        credits = {
            second["activity"]["id"]: _credit_map(
                get_saved(user_id, second["activity"]["id"])
            ).get("elevation", Decimal("0")),
            third_id["id"]: _credit_map(get_saved(user_id, third_id["id"])).get(
                "elevation", Decimal("0")
            ),
        }
        assert credits[second["activity"]["id"]] == _feet("500")
        assert credits[third_id["id"]] == _feet("300")
        assert _totals(journey_id)["elevation"] == _feet("800")

        left = create_activity(user_id, _fields(elevation="200"))
        right = create_activity(user_id, _fields(elevation="200"))
        edit_errors: list[BaseException] = []
        edit_gate = threading.Barrier(2)

        def edit(activity_id: int, feet: str) -> None:
            try:
                with app.app_context():
                    edit_gate.wait(timeout=8)
                    update_activity(user_id, activity_id, {"elevation_ft": feet})
            except BaseException as exc:
                edit_errors.append(exc)

        editors = [
            threading.Thread(target=edit, args=(left["activity"]["id"], "800")),
            threading.Thread(target=edit, args=(right["activity"]["id"], "100")),
        ]
        for thread in editors:
            thread.start()
        for thread in editors:
            thread.join(timeout=20)
        assert edit_errors == []
        assert get_saved(user_id, left["activity"]["id"])["activity"]["elevation_gain_m"] == _feet(
            "800"
        )
        assert get_saved(user_id, right["activity"]["id"])["activity"]["elevation_gain_m"] == _feet(
            "100"
        )
        assert _credit_map(get_saved(user_id, left["activity"]["id"])).get(
            "elevation", Decimal("0")
        ) == _feet("200")
        assert "elevation" not in _credit_map(get_saved(user_id, right["activity"]["id"]))
        assert _totals(journey_id)["elevation"] == _feet("1000")


def test_allocation_waits_for_the_journey_lock_and_a_prior_summit_token_fails(app):
    with app.app_context():
        user_id = _user("Lock Logger")
        route_id, journey_id = _open(user_id, "alloc-lock")
        first = create_activity(user_id, _fields(elevation="400"))
        create_activity(user_id, _fields(elevation="400"))
        fields = _fields(distance="50")
        preview = preview_activity(user_id, fields)
        token = preview["confirmation_token"]
        holding = threading.Event()
        release = threading.Event()
        outcome: dict[str, str] = {}

        def hold() -> None:
            import psycopg

            connection = psycopg.connect(TEST_URL, autocommit=False)
            connection.execute("SET search_path TO adventurer")
            connection.execute(
                "SELECT id FROM users WHERE id = %s FOR UPDATE",
                (user_id,),
            )
            connection.execute(
                "SELECT id FROM route_journeys WHERE id = %s FOR UPDATE",
                (journey_id,),
            )
            holding.set()
            release.wait(timeout=8)
            connection.rollback()
            connection.close()

        def save() -> None:
            holding.wait(timeout=8)
            with app.app_context():
                try:
                    update_activity(user_id, first["activity"]["id"], {"elevation_ft": "800"})
                    outcome["edit"] = "committed"
                except ActivityError as exc:
                    outcome["edit"] = exc.code

        holder = threading.Thread(target=hold)
        writer = threading.Thread(target=save)
        holder.start()
        assert holding.wait(timeout=8)
        writer.start()
        blocked = False
        deadline = threading.Event()
        import time

        started = time.time()
        while time.time() - started < 1.5:
            if not writer.is_alive():
                break
            time.sleep(0.05)
        else:
            blocked = writer.is_alive()
        release.set()
        writer.join(timeout=20)
        holder.join(timeout=8)
        assert blocked
        assert outcome["edit"] == "committed"
        with pytest.raises(ActivityError) as caught:
            create_activity(user_id, fields, confirmation_token=token)
        assert caught.value.code == "confirmation_required"
        assert _journey_row(journey_id)["status"] == "active"
        restart = threading.Thread(
            target=lambda: _restart(app, user_id, route_id, outcome)
        )
        edit = threading.Thread(
            target=lambda: _edit(app, user_id, first["activity"]["id"], outcome)
        )
        restart.start()
        edit.start()
        restart.join(timeout=20)
        edit.join(timeout=20)
        assert outcome.get("restart") == "committed"
        assert outcome.get("second_edit") == "committed"
        assert _assignment_journey(first["activity"]["id"]) == journey_id


def _restart(app, user_id: int, route_id: int, outcome: dict) -> None:
    try:
        with app.app_context():
            restart_journey(user_id, route_id)
            outcome["restart"] = "committed"
    except Exception as exc:
        outcome["restart"] = str(exc)


def _edit(app, user_id: int, activity_id: int, outcome: dict) -> None:
    try:
        with app.app_context():
            update_activity(user_id, activity_id, {"elevation_ft": "350"})
            outcome["second_edit"] = "committed"
    except Exception as exc:
        outcome["second_edit"] = str(exc)


def test_recalculation_and_closure_do_not_mix_a_snapshot(app):
    with app.app_context():
        owner_id = _user("Close Owner")
        _route("alloc-close", DAY_TARGETS)
        create_private_expedition(
            owner_id,
            slug="alloc-close-exp",
            name="Close",
            route_slug="alloc-close",
        )
        journey_id = get_db().execute(
            """
            SELECT journey_id
            FROM expedition_participations
            WHERE user_id = ? AND status = 'active'
            """,
            (owner_id,),
        ).fetchone()["journey_id"]
        first = create_activity(owner_id, _fields(elevation="1000"))
        second = create_activity(owner_id, _fields(elevation="500"))
        errors: list[BaseException] = []
        gate = threading.Barrier(2)

        def close() -> None:
            try:
                with app.app_context():
                    gate.wait(timeout=8)
                    archive_private_expedition(owner_id, "alloc-close-exp")
            except BaseException as exc:
                errors.append(exc)

        def remove() -> None:
            try:
                with app.app_context():
                    gate.wait(timeout=8)
                    delete_activity(owner_id, first["activity"]["id"])
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=close), threading.Thread(target=remove)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert errors == []
        frozen = _snapshot_amount("alloc-close-exp", "elevation")
        assert frozen in {_feet("1000"), _feet("500")}
        assert _totals(journey_id)["elevation"] == _feet("500")
        update_activity(owner_id, second["activity"]["id"], {"elevation_ft": "250"})
        assert _snapshot_amount("alloc-close-exp", "elevation") == frozen
        assert _standing(owner_id, "alloc-close-exp", owner_id, "elevation") == frozen


def test_every_supported_type_still_uses_the_one_walk(app):
    with app.app_context():
        user_id = _user("Type Logger")
        _route_id, journey_id = _open(user_id, "alloc-types")
        for activity_type in ("hike", "cycle", "indoor_cycle", "other", "unspecified"):
            saved = create_activity(
                user_id,
                _fields(distance="1", activity_type=activity_type),
            )
            assert _credit_map(saved)["distance"] == _miles("1")
        assert _journey_row(journey_id)["status"] == "active"
        assert get_db().execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'adventurer'
              AND table_name = 'expeditions'
              AND column_name = 'activity_policy'
            """
        ).fetchone() is not None
        policy = get_db().execute(
            "SELECT activity_policy FROM route_journeys WHERE id = ?",
            (journey_id,),
        ).fetchone()
        assert policy["activity_policy"] == "unrestricted"
