"""Assignment order, privacy cutoffs, and progress version under a reversed lock.

The early transaction starts first and acquires the journey lock only after the
late transaction has committed. Stored credits must match one later walk.
"""

from __future__ import annotations

import threading
from decimal import Decimal

from adventurer.activities import create_activity, preview_activity, preview_activity_update, update_activity
from adventurer.allocation import MeasuredAssignment, allocate
from adventurer.db import get_db
from adventurer.expeditions import (
    accept_invitation,
    create_private_expedition,
    create_share_link,
    get_leaderboard,
    remove_participation_privacy,
)
from adventurer.journey_progress import read_journey_progress
from adventurer.journeys import start_journey
from tests.test_activity_foundations import (
    _assignment_count,
    _credit_map,
    _feet,
    _fields,
    _miles,
    _journey_row,
    _origin_row,
    _route,
    _totals,
    _user,
    get_saved,
)
from tests.test_activity_lifecycle import _route as _route_with_camps


def test_a_later_lock_sorts_after_the_assignment_that_committed_first(app):
    with app.app_context():
        user_id = _user("Order Create")
        route_id = _route(
            "order-create",
            {"elevation": _feet("500"), "distance": _miles("50")},
        )
        journey_id = start_journey(user_id, route_id)["id"]
        held = create_activity(user_id, _fields(elevation="100"))

    def late():
        return create_activity(user_id, _fields(elevation="400"))

    def early():
        return create_activity(user_id, _fields(elevation="400"))

    holder = _reverse_start(app, early, late)
    with app.app_context():
        late_id = holder["late"]["activity"]["id"]
        early_id = holder["early"]["activity"]["id"]
        late_at = _assignment_time(late_id)
        early_at = _assignment_time(early_id)
        assert holder["early_now"] < late_at < early_at
        assert _credit_map(get_saved(user_id, held["activity"]["id"]))["elevation"] == _feet("100")
        assert _credit_map(get_saved(user_id, late_id))["elevation"] == _feet("400")
        assert _credit_map(get_saved(user_id, early_id)) == {}
        assert _assignment_count(early_id) == 1
        _assert_walk(journey_id, [("elevation", _feet("500"))])


def test_a_first_assignment_sorts_after_the_create_that_locked_first(app):
    with app.app_context():
        user_id = _user("Order Edit")
        route_id = _route(
            "order-edit",
            {"elevation": _feet("500"), "distance": _miles("50")},
        )
        journey_id = start_journey(user_id, route_id)["id"]
        loose = create_activity(user_id, _fields(elevation="0"))
        held = create_activity(user_id, _fields(elevation="100"))
        assert _assignment_count(loose["activity"]["id"]) == 0

    loose_id = loose["activity"]["id"]

    def late():
        return create_activity(user_id, _fields(elevation="400"))

    def early():
        return update_activity(user_id, loose_id, {"elevation_ft": "400"})

    holder = _reverse_start(app, early, late)
    with app.app_context():
        late_id = holder["late"]["activity"]["id"]
        late_at = _assignment_time(late_id)
        early_at = _assignment_time(loose_id)
        assert holder["early_now"] < late_at < early_at
        assert _credit_map(get_saved(user_id, held["activity"]["id"]))["elevation"] == _feet("100")
        assert _credit_map(get_saved(user_id, late_id))["elevation"] == _feet("400")
        assert _credit_map(holder["early"]) == {}
        assert holder["early"]["activity"]["association"]["journey_id"] == journey_id
        _assert_walk(journey_id, [("elevation", _feet("500"))])


def test_a_rejoin_that_locks_second_hides_the_activity_committed_first(app, monkeypatch):
    _skip_due_close(monkeypatch)
    with app.app_context():
        owner_id, member_id, token = _two_expeditions("order-hide")
        existing = create_activity(member_id, _fields(elevation="100"))
        existing_origin = _origin_row(existing["activity"]["id"], "elevation")["credited_at"]
        remove_participation_privacy(member_id, "order-hide-exp", confirm=True)

    def late():
        return create_activity(member_id, _fields(elevation="80"))

    def early():
        return accept_invitation(member_id, token=token)

    holder = _reverse_start(app, early, late)
    with app.app_context():
        created = holder["late"]
        cutoff = holder["early"]["participation"]["display_credits_after"]
        origin = _origin_row(created["activity"]["id"], "elevation")["credited_at"]
        assert holder["early_now"] < _assignment_time(created["activity"]["id"])
        assert origin == _assignment_time(created["activity"]["id"]) < cutoff
        assert _origin_row(existing["activity"]["id"], "elevation")["credited_at"] == existing_origin
        assert _cutoff("order-hide-two", member_id) is None
        assert _standing(owner_id, "order-hide-exp", member_id) == 0
        assert _standing(owner_id, "order-hide-two", member_id) == _feet("180")
        revised = update_activity(member_id, created["activity"]["id"], {"elevation_ft": "90"})
        assert _origin_row(created["activity"]["id"], "elevation")["credited_at"] == origin
        assert _credit_map(revised)["elevation"] == _feet("90")
        assert _standing(owner_id, "order-hide-exp", member_id) == 0
        assert _standing(owner_id, "order-hide-two", member_id) == _feet("190")


def test_an_activity_that_locks_after_rejoin_stays_visible(app, monkeypatch):
    _skip_due_close(monkeypatch)
    with app.app_context():
        owner_id, member_id, token = _two_expeditions("order-show")
        existing = create_activity(member_id, _fields(elevation="100"))
        existing_origin = _origin_row(existing["activity"]["id"], "elevation")["credited_at"]
        remove_participation_privacy(member_id, "order-show-exp", confirm=True)

    def late():
        return accept_invitation(member_id, token=token)

    def early():
        return create_activity(member_id, _fields(elevation="80"))

    holder = _reverse_start(app, early, late)
    with app.app_context():
        created = holder["early"]
        cutoff = holder["late"]["participation"]["display_credits_after"]
        origin = _origin_row(created["activity"]["id"], "elevation")["credited_at"]
        assert holder["early_now"] < cutoff < origin
        assert origin == _assignment_time(created["activity"]["id"])
        assert existing_origin < cutoff
        assert _origin_row(existing["activity"]["id"], "elevation")["credited_at"] == existing_origin
        assert _cutoff("order-show-two", member_id) is None
        assert _standing(owner_id, "order-show-exp", member_id) == _feet("80")
        assert _standing(owner_id, "order-show-two", member_id) == _feet("180")
        revised = update_activity(member_id, created["activity"]["id"], {"elevation_ft": "90"})
        assert _origin_row(created["activity"]["id"], "elevation")["credited_at"] == origin
        assert _standing(owner_id, "order-show-exp", member_id) == _feet("90")
        assert _standing(owner_id, "order-show-two", member_id) == _feet("190")


def test_an_unassigned_edit_leaves_the_active_journey_and_its_summit_token(app):
    with app.app_context():
        user_id = _user("Order Version")
        route_id = _route_with_camps(
            "order-version",
            {"elevation": _feet("1000")},
            [
                ("camp", 0, {"elevation": _feet("400")}),
                ("summit", 1, {"elevation": _feet("1000")}),
            ],
        )
        journey_id = start_journey(user_id, route_id)["id"]
        loose = create_activity(user_id, _fields(distance="1"))
        credited = create_activity(user_id, _fields(elevation="500"))
        version = _journey_row(journey_id)["progress_version"]
        totals = _totals(journey_id)
        reached = _highest(journey_id)
        preview = preview_activity_update(user_id, loose["activity"]["id"], {"distance_mi": "4"})
        assert preview["journey"] is None
        assert preview["no_credit_reason"] == "no_positive_measurement"
        saved = update_activity(user_id, loose["activity"]["id"], {"distance_mi": "4"})
        assert saved["journey"] is None
        assert saved["activity"]["association"] is None
        assert saved["no_credit_reason"] == "no_positive_measurement"
        assert saved["activity"]["distance_m"] == _miles("4")
        assert _assignment_count(loose["activity"]["id"]) == 0
        assert _journey_row(journey_id)["progress_version"] == version
        assert _totals(journey_id) == totals
        assert _highest(journey_id) == reached

        same = update_activity(user_id, credited["activity"]["id"], {"elevation_ft": "500"})
        assert _credit_map(same)["elevation"] == _feet("500")
        assert _journey_row(journey_id)["progress_version"] == version
        noted = update_activity(user_id, credited["activity"]["id"], {"note": "clear"})
        assert noted["activity"]["note"] == "clear"
        assert _journey_row(journey_id)["progress_version"] == version

        summit = preview_activity(user_id, _fields(elevation="500"))
        token = summit["confirmation_token"]
        update_activity(user_id, loose["activity"]["id"], {"distance_mi": "6"})
        assert _journey_row(journey_id)["progress_version"] == version
        finished = create_activity(
            user_id,
            _fields(elevation="500"),
            confirmation_token=token,
        )
        assert finished["journey"]["status"] == "completed"

        fresh_user = _user("Order First")
        fresh_route = _route(
            "order-first",
            {"elevation": _feet("1000"), "distance": _miles("50")},
        )
        fresh_journey = start_journey(fresh_user, fresh_route)["id"]
        waiting = create_activity(fresh_user, _fields(distance="2"))
        before = _journey_row(fresh_journey)["progress_version"]
        attaching = preview_activity_update(
            fresh_user, waiting["activity"]["id"], {"elevation_ft": "100"}
        )
        assert attaching["journey"]["id"] == fresh_journey
        assert attaching["no_credit_reason"] is None
        attached = update_activity(
            fresh_user, waiting["activity"]["id"], {"elevation_ft": "100"}
        )
        assert attached["journey"]["id"] == fresh_journey
        assert attached["activity"]["association"]["journey_id"] == fresh_journey
        assert attached["no_credit_reason"] is None
        assert _credit_map(attached)["elevation"] == _feet("100")
        assert _journey_row(fresh_journey)["progress_version"] == before + 1

        capped = create_activity(fresh_user, _fields(elevation="1000"))
        held_version = _journey_row(fresh_journey)["progress_version"]
        held_credit = _credit_map(capped)["elevation"]
        held_total = _totals(fresh_journey)["elevation"]
        taller = update_activity(fresh_user, capped["activity"]["id"], {"elevation_ft": "1200"})
        assert taller["activity"]["elevation_gain_m"] == _feet("1200")
        assert _credit_map(taller)["elevation"] == held_credit
        assert _totals(fresh_journey)["elevation"] == held_total
        assert _journey_row(fresh_journey)["progress_version"] == held_version + 1


def _reverse_start(app, early_write, late_write) -> dict:
    started = threading.Event()
    late_done = threading.Event()
    holder: dict = {}

    def early() -> None:
        with app.app_context():
            try:
                get_db().execute("SELECT 1")
                holder["early_now"] = get_db().execute(
                    "SELECT now() AS started"
                ).fetchone()["started"]
                started.set()
                assert late_done.wait(timeout=20)
                holder["early"] = early_write()
            except Exception as exc:
                holder["early_error"] = exc
            finally:
                get_db().close()

    def late() -> None:
        with app.app_context():
            try:
                assert started.wait(timeout=20)
                holder["late"] = late_write()
            except Exception as exc:
                holder["late_error"] = exc
            finally:
                get_db().close()
                late_done.set()

    threads = [threading.Thread(target=early), threading.Thread(target=late)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert "early_error" not in holder, holder.get("early_error")
    assert "late_error" not in holder, holder.get("late_error")
    return holder


def _skip_due_close(monkeypatch) -> None:
    monkeypatch.setattr("adventurer.expeditions.complete_due_expeditions", lambda: [])


def _two_expeditions(prefix: str) -> tuple[int, int, str]:
    owner_id = _user(f"{prefix} owner")
    member_id = _user(f"{prefix} member")
    _route(prefix, {"elevation": _feet("1000")})
    create_private_expedition(
        owner_id, slug=f"{prefix}-exp", name="One", route_slug=prefix
    )
    create_private_expedition(
        owner_id, slug=f"{prefix}-two", name="Two", route_slug=prefix
    )
    link = create_share_link(owner_id, f"{prefix}-exp")
    other = create_share_link(owner_id, f"{prefix}-two")
    accept_invitation(member_id, token=link["token"])
    accept_invitation(member_id, token=other["token"])
    return owner_id, member_id, link["token"]


def _assignment_time(activity_id: int):
    with_context = get_db().execute(
        """
        SELECT created_at
        FROM activity_journey_assignments
        WHERE activity_id = ?
        """,
        (activity_id,),
    ).fetchone()
    return with_context["created_at"]


def _assert_walk(journey_id: int, requirements: list[tuple[str, Decimal]]) -> None:
    rows = get_db().execute(
        """
        SELECT assignment.activity_id, assignment.id AS assignment_id, assignment.created_at,
               activity.activity_type, activity.elevation_gain_m, activity.distance_m
        FROM activity_journey_assignments AS assignment
        JOIN activities AS activity ON activity.id = assignment.activity_id
        WHERE assignment.journey_id = ?
        """,
        (journey_id,),
    ).fetchall()
    slots = [
        MeasuredAssignment(
            row["activity_id"],
            row["assignment_id"],
            row["created_at"],
            False,
            row["activity_type"],
            row["elevation_gain_m"],
            row["distance_m"],
        )
        for row in rows
    ]
    expected = allocate(slots, requirements)
    stored: dict[int, dict[str, Decimal]] = {}
    for row in get_db().execute(
        """
        SELECT assignment.activity_id, credit.metric_code, credit.credited_amount
        FROM journey_credits AS credit
        JOIN activity_journey_assignments AS assignment
          ON assignment.id = credit.assignment_id
        WHERE assignment.journey_id = ?
        """,
        (journey_id,),
    ).fetchall():
        stored.setdefault(row["activity_id"], {})[row["metric_code"]] = row["credited_amount"]
    assert stored == expected


def _standing(owner_id: int, slug: str, member_id: int) -> Decimal:
    board = get_leaderboard(owner_id, slug)
    entry = next(row for row in board["entries"] if row["user_id"] == member_id)
    item = next(row for row in entry["metrics"] if row["metric"] == "elevation")
    if "total_m" in item:
        return item["total_m"]
    return item["credited_m"]


def _cutoff(slug: str, user_id: int):
    return get_db().execute(
        """
        SELECT participation.display_credits_after
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE expedition.slug = ? AND participation.user_id = ?
        """,
        (slug, user_id),
    ).fetchone()["display_credits_after"]


def _highest(journey_id: int) -> str | None:
    progress = read_journey_progress(get_db(), journey_id)
    if progress.highest_checkpoint is None:
        return None
    return progress.highest_checkpoint.slug

