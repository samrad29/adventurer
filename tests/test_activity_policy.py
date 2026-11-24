"""Release 2 expedition activity rules, confirmation, and succession.

Runs against the isolated PostgreSQL database named by TEST_DATABASE_URL.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from adventurer.activities import (
    ActivityError,
    create_activity,
    preview_activity,
    preview_activity_update,
    update_activity,
)
from adventurer.activity_policy import PolicyError
from adventurer.db import get_db
from adventurer.expeditions import (
    ExpeditionError,
    archive_private_expedition,
    create_organization_expedition,
    create_private_expedition,
    create_share_link,
    get_leaderboard,
    join_expedition,
    leave_expedition,
    remove_participation_privacy,
    accept_invitation,
)
from adventurer.journeys import (
    active_journey,
    choose_activity_policy,
    restart_journey,
    start_journey,
)
from adventurer.measures import feet_to_meters
from adventurer.metrics import parse_measurement
from adventurer.services import create_adventurer

from test_activity_foundations import _route as _dual_route  # type: ignore
from test_expeditions import (  # type: ignore
    _org,
    _route as _elevation_route,
    _season,
)

DAY = "2026-11-24"


def _user(name: str) -> int:
    return create_adventurer(name, "trailhead")["id"]


def _feet(amount: str) -> Decimal:
    return feet_to_meters(Decimal(amount))


def _policy(journey_id: int) -> tuple[str, tuple[str, ...], int | None]:
    row = get_db().execute(
        """
        SELECT activity_policy, governing_expedition_id, progress_version
        FROM route_journeys
        WHERE id = ?
        """,
        (journey_id,),
    ).fetchone()
    types = tuple(
        item["activity_type"]
        for item in get_db().execute(
            """
            SELECT activity_type
            FROM journey_activity_types
            WHERE journey_id = ?
            ORDER BY activity_type
            """,
            (journey_id,),
        ).fetchall()
    )
    return row["activity_policy"], types, row["governing_expedition_id"]


def _version(journey_id: int) -> int:
    return get_db().execute(
        "SELECT progress_version FROM route_journeys WHERE id = ?",
        (journey_id,),
    ).fetchone()["progress_version"]


def _credit(journey_id: int) -> Decimal:
    row = get_db().execute(
        """
        SELECT credited_total
        FROM journey_metric_totals
        WHERE journey_id = ? AND metric_code = 'elevation'
        """,
        (journey_id,),
    ).fetchone()
    if row is None:
        return Decimal("0")
    return row["credited_total"]


def _journey_id(user_id: int) -> int:
    return get_db().execute(
        """
        SELECT id FROM route_journeys
        WHERE user_id = ? AND is_current
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()["id"]


def _log(user_id: int, feet: str, activity_type: str = "hike") -> dict:
    fields = {
        "elevation_ft": feet,
        "happened_on": DAY,
        "activity_type": activity_type,
    }
    preview = preview_activity(user_id, fields)
    token = preview["confirmation_token"] if preview.get("will_complete") else None
    return create_activity(user_id, fields, confirmation_token=token)


def test_a_restricted_list_credits_only_its_types(app):
    with app.app_context():
        user_id = _user("Narrow")
        _elevation_route("policy-foot", "1000")
        created = create_private_expedition(
            user_id,
            slug="on-foot",
            name="On Foot",
            route_slug="policy-foot",
            activity_policy={"preset": "on_foot"},
        )
        assert created["expedition"]["activity_policy"] == {
            "mode": "restricted",
            "activity_types": ["hike", "run", "stair_climb", "walk"],
        }
        journey_id = created["journey"]["id"]
        mode, types, governor = _policy(journey_id)
        assert mode == "restricted"
        assert types == ("hike", "run", "stair_climb", "walk")
        assert governor == created["expedition"]["id"]
        _log(user_id, "100", "hike")
        cycle = _log(user_id, "400", "cycle")
        assert cycle["no_credit_reason"] == "activity_type_not_eligible"
        assert _credit(journey_id) == _feet("100")
        assignment = get_db().execute(
            """
            SELECT id FROM activity_journey_assignments
            WHERE activity_id = ?
            """,
            (cycle["activity"]["id"],),
        ).fetchone()
        assert assignment is not None


def test_joining_a_stricter_expedition_confirms_before_it_rewrites_credit(app):
    with app.app_context():
        user_id = _user("Confirm")
        _elevation_route("policy-confirm", "1000")
        first = create_private_expedition(
            user_id, slug="wide", name="Wide", route_slug="policy-confirm"
        )
        journey_id = first["journey"]["id"]
        _log(user_id, "200", "hike")
        _log(user_id, "300", "cycle")
        before = _version(journey_id)
        with pytest.raises(ExpeditionError) as caught:
            create_private_expedition(
                user_id,
                slug="feet-only",
                name="Feet Only",
                route_slug="policy-confirm",
                activity_policy={"preset": "on_foot"},
            )
        assert caught.value.code == "confirmation_required"
        assert caught.value.status == 409
        assert _credit(journey_id) == _feet("500")
        assert get_db().execute(
            "SELECT 1 FROM expeditions WHERE slug = 'feet-only'"
        ).fetchone() is None
        token = caught.value.details["confirmation_token"]
        preview = caught.value.details["policy_preview"]
        assert preview["will_complete"] is False
        assert any(
            item["credited_m"] == "0" or Decimal(item["credited_m"]) == 0
            for item in preview["allocations"]
        )
        confirmed = create_private_expedition(
            user_id,
            slug="feet-only",
            name="Feet Only",
            route_slug="policy-confirm",
            activity_policy={"preset": "on_foot"},
            confirmation_token=token,
        )
        assert confirmed["journey"]["id"] == journey_id
        assert _policy(journey_id)[0] == "restricted"
        assert _policy(journey_id)[2] == confirmed["expedition"]["id"]
        assert _credit(journey_id) == _feet("200")
        assert _version(journey_id) == before + 1


def test_a_broader_expedition_can_share_without_widening(app):
    with app.app_context():
        user_id = _user("Share")
        _elevation_route("policy-share", "1000")
        narrow = create_private_expedition(
            user_id,
            slug="feet",
            name="Feet",
            route_slug="policy-share",
            activity_policy={"activity_types": ["hike"], "mode": "restricted"},
        )
        journey_id = narrow["journey"]["id"]
        shared = join_expedition(user_id, "everest") if False else create_private_expedition(
            user_id, slug="open-share", name="Open", route_slug="policy-share"
        )
        assert shared["journey"]["id"] == journey_id
        assert _policy(journey_id)[1] == ("hike",)
        assert _policy(journey_id)[2] == narrow["expedition"]["id"]


def test_incomparable_lists_conflict_and_leave_the_journey(app):
    with app.app_context():
        user_id = _user("Clash")
        _elevation_route("policy-clash", "1000")
        create_private_expedition(
            user_id,
            slug="hike-only",
            name="Hike",
            route_slug="policy-clash",
            activity_policy={"mode": "restricted", "activity_types": ["hike"]},
        )
        with pytest.raises(ExpeditionError) as caught:
            create_private_expedition(
                user_id,
                slug="cycle-only",
                name="Cycle",
                route_slug="policy-clash",
                activity_policy={"mode": "restricted", "activity_types": ["cycle"]},
            )
        assert caught.value.code == "activity_policy_conflict"
        assert get_db().execute(
            "SELECT 1 FROM expeditions WHERE slug = 'cycle-only'"
        ).fetchone() is None


def test_succession_keep_and_expansion(app):
    with app.app_context():
        user_id = _user("Keep")
        _elevation_route("policy-keep", "1000")
        first = create_private_expedition(
            user_id,
            slug="first-feet",
            name="First",
            route_slug="policy-keep",
            activity_policy={"mode": "restricted", "activity_types": ["hike", "walk"]},
        )
        second = create_private_expedition(
            user_id,
            slug="second-feet",
            name="Second",
            route_slug="policy-keep",
            activity_policy={"mode": "restricted", "activity_types": ["hike", "walk"]},
        )
        journey_id = first["journey"]["id"]
        assert _policy(journey_id)[2] == min(first["expedition"]["id"], second["expedition"]["id"])
        leave_expedition(user_id, "first-feet")
        assert _policy(journey_id)[2] == second["expedition"]["id"]
        version = _version(journey_id)
        leave_expedition(user_id, "second-feet")
        assert _policy(journey_id)[2] is None
        assert _policy(journey_id)[1] == ("hike", "walk")
        pending = get_db().execute(
            "SELECT reason FROM journey_policy_choices WHERE journey_id = ?",
            (journey_id,),
        ).fetchone()
        assert pending["reason"] == "left"
        choose_activity_policy(user_id, journey_id, "keep", None)
        assert _policy(journey_id)[0] == "restricted"
        assert _version(journey_id) == version
        assert get_db().execute(
            "SELECT 1 FROM journey_policy_choices WHERE journey_id = ?",
            (journey_id,),
        ).fetchone() is None
        with pytest.raises(Exception) as blocked:
            choose_activity_policy(user_id, journey_id, "expand", None)
        assert getattr(blocked.value, "code", None) == "confirmation_required"
        token = blocked.value.details["confirmation_token"]
        choose_activity_policy(user_id, journey_id, "expand", token)
        assert _policy(journey_id)[0] == "unrestricted"
        assert _version(journey_id) == version + 1


def test_a_stale_token_with_the_same_total_writes_nothing(app):
    with app.app_context():
        user_id = _user("Stale")
        _dual_route(
            "policy-stale",
            {
                "elevation": _feet("1000"),
                "distance": parse_measurement("distance", "50"),
            },
        )
        create_private_expedition(
            user_id, slug="stale-open", name="Open", route_slug="policy-stale"
        )
        journey_id = _journey_id(user_id)
        first = _log(user_id, "600", "hike")
        _log(user_id, "400", "walk")
        with pytest.raises(ExpeditionError) as caught:
            create_private_expedition(
                user_id,
                slug="stale-hike",
                name="Hike",
                route_slug="policy-stale",
                activity_policy={"mode": "restricted", "activity_types": ["hike", "walk"]},
            )
        update_activity(user_id, first["activity"]["id"], {"elevation_ft": "700"})
        with pytest.raises(ExpeditionError) as stale:
            create_private_expedition(
                user_id,
                slug="stale-hike",
                name="Hike",
                route_slug="policy-stale",
                activity_policy={"mode": "restricted", "activity_types": ["hike", "walk"]},
                confirmation_token=caught.value.details["confirmation_token"],
            )
        assert stale.value.code == "stale_preview"
        assert get_db().execute(
            "SELECT 1 FROM expeditions WHERE slug = 'stale-hike'"
        ).fetchone() is None
        assert _credit(journey_id) == _feet("1000")
        assert _policy(journey_id)[0] == "unrestricted"


def test_expansion_can_complete_and_a_closed_snapshot_stays(app):
    with app.app_context():
        user_id = _user("Finish")
        _elevation_route("policy-finish", "100")
        created = create_private_expedition(
            user_id,
            slug="finish-feet",
            name="Feet",
            route_slug="policy-finish",
            activity_policy={"mode": "restricted", "activity_types": ["hike"]},
        )
        journey_id = created["journey"]["id"]
        _log(user_id, "40", "hike")
        cycle = _log(user_id, "60", "cycle")
        assert cycle["no_credit_reason"] == "activity_type_not_eligible"
        archive_private_expedition(user_id, "finish-feet")
        frozen = get_db().execute(
            """
            SELECT metric.total_amount
            FROM expedition_result_metrics AS metric
            JOIN expedition_result_entries AS entry ON entry.id = metric.result_entry_id
            WHERE entry.user_id = ? AND metric.metric_code = 'elevation'
            """,
            (user_id,),
        ).fetchone()["total_amount"]
        with pytest.raises(Exception) as caught:
            choose_activity_policy(user_id, journey_id, "expand", None)
        assert caught.value.code == "confirmation_required"
        assert caught.value.details["policy_preview"]["will_complete"] is True
        finished = choose_activity_policy(
            user_id,
            journey_id,
            "expand",
            caught.value.details["confirmation_token"],
        )
        assert finished["status"] == "completed"
        assert _credit(journey_id) == _feet("100")
        again = get_db().execute(
            """
            SELECT metric.total_amount
            FROM expedition_result_metrics AS metric
            JOIN expedition_result_entries AS entry ON entry.id = metric.result_entry_id
            WHERE entry.user_id = ? AND metric.metric_code = 'elevation'
            """,
            (user_id,),
        ).fetchone()["total_amount"]
        assert again == frozen
        summit = get_db().execute(
            """
            SELECT kind FROM expedition_updates
            WHERE user_id = ? AND kind = 'summit_reached'
            """,
            (user_id,),
        ).fetchall()
        assert summit == []


def test_restart_copies_a_retained_list_and_a_completed_journey_stays_fixed(app):
    with app.app_context():
        user_id = _user("Restart")
        route_id = _elevation_route("policy-restart", "100")
        created = create_private_expedition(
            user_id,
            slug="restart-feet",
            name="Feet",
            route_slug="policy-restart",
            activity_policy={"mode": "restricted", "activity_types": ["hike"]},
        )
        journey_id = created["journey"]["id"]
        leave_expedition(user_id, "restart-feet")
        choose_activity_policy(user_id, journey_id, "keep", None)
        _log(user_id, "100", "hike")
        assert get_db().execute(
            "SELECT status FROM route_journeys WHERE id = ?",
            (journey_id,),
        ).fetchone()["status"] == "completed"
        with pytest.raises(Exception) as blocked:
            choose_activity_policy(user_id, journey_id, "expand", None)
        assert blocked.value.code == "invalid_state"
        restarted = restart_journey(user_id, route_id)
        new_id = restarted["journey"]["id"]
        assert _policy(new_id)[1] == ("hike",)
        assert get_db().execute(
            "SELECT 1 FROM journey_policy_choices WHERE journey_id = ?",
            (new_id,),
        ).fetchone() is None
        with pytest.raises(Exception) as expand:
            choose_activity_policy(user_id, new_id, "expand", None)
        assert expand.value.code == "confirmation_required"


def test_global_everest_stays_unrestricted_and_an_empty_list_fails(app):
    with app.app_context():
        user_id = _user("Everest")
        _elevation_route("policy-empty", "1000")
        with pytest.raises(PolicyError):
            create_private_expedition(
                user_id,
                slug="empty-rules",
                name="Empty",
                route_slug="policy-empty",
                activity_policy={"mode": "restricted", "activity_types": []},
            )
        database = get_db()
        with pytest.raises(Exception):
            database.execute(
                """
                UPDATE expeditions
                SET activity_policy = 'restricted', activity_policy_sealed = false
                WHERE slug = 'everest'
                """
            )
        database.rollback()
        row = database.execute(
            "SELECT activity_policy FROM expeditions WHERE slug = 'everest'"
        ).fetchone()
        assert row["activity_policy"] == "unrestricted"


def test_organization_create_stores_a_policy_without_joining(app):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        _org(app, admin_id, "policy-club", "Owen")
        _elevation_route("policy-org", "1000")
        created = create_organization_expedition(
            owner_id,
            "policy-club",
            slug="club-feet",
            name="Club Feet",
            route_slug="policy-org",
            enrollment="open",
            activity_policy={"preset": "on_foot"},
        )
        assert created["activity_policy"]["mode"] == "restricted"
        assert get_db().execute(
            "SELECT COUNT(*) AS n FROM route_journeys WHERE user_id = ?",
            (owner_id,),
        ).fetchone()["n"] == 0
        joined = join_expedition(owner_id, "club-feet")
        mode, types, governor = _policy(joined["journey"]["id"])
        assert mode == "restricted"
        assert "hike" in types
        assert governor == created["id"]


def test_a_pre_cutoff_activity_stays_off_the_board_after_expansion(app):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _elevation_route("policy-cutoff", "1000")
        create_private_expedition(
            owner_id, slug="owen-open", name="Owen", route_slug="policy-cutoff"
        )
        link = create_share_link(owner_id, "owen-open")
        restricted = create_private_expedition(
            sam_id,
            slug="sam-feet",
            name="Feet",
            route_slug="policy-cutoff",
            activity_policy={"mode": "restricted", "activity_types": ["hike"]},
        )
        journey_id = restricted["journey"]["id"]
        cycle = _log(sam_id, "100", "cycle")
        assert cycle["no_credit_reason"] == "activity_type_not_eligible"
        accept_invitation(sam_id, token=link["token"])
        remove_participation_privacy(sam_id, "owen-open", confirm=True)
        accept_invitation(sam_id, token=link["token"])
        leave_expedition(sam_id, "sam-feet")
        choose_activity_policy(sam_id, journey_id, "keep", None)
        with pytest.raises(Exception) as caught:
            choose_activity_policy(sam_id, journey_id, "expand", None)
        choose_activity_policy(
            sam_id, journey_id, "expand", caught.value.details["confirmation_token"]
        )
        assert _credit(journey_id) == _feet("100")
        board = get_leaderboard(owner_id, "owen-open")
        sam = next(row for row in board["entries"] if row["user_id"] == sam_id)
        shown = next(item["credited_m"] for item in sam["metrics"] if item["metric"] == "elevation")
        assert shown == Decimal("0")


def test_confirm_new_attempt_refuses_an_incompatible_expedition(app):
    with app.app_context():
        user_id = _user("Adopt")
        _elevation_route("policy-adopt", "100")
        create_private_expedition(
            user_id,
            slug="adopt-feet",
            name="Feet",
            route_slug="policy-adopt",
            activity_policy={"mode": "restricted", "activity_types": ["hike"]},
        )
        _log(user_id, "100", "hike")
        with pytest.raises(ExpeditionError) as caught:
            create_private_expedition(
                user_id,
                slug="adopt-cycle",
                name="Cycle",
                route_slug="policy-adopt",
                activity_policy={"mode": "restricted", "activity_types": ["cycle"]},
                confirm_new_attempt=True,
            )
        assert caught.value.code == "activity_policy_conflict"
        assert get_db().execute(
            "SELECT 1 FROM expeditions WHERE slug = 'adopt-cycle'"
        ).fetchone() is None
        assert get_db().execute(
            """
            SELECT status, attempt_number
            FROM route_journeys
            WHERE user_id = ? AND is_current
            """,
            (user_id,),
        ).fetchone()["attempt_number"] == 1


def test_a_sealed_expedition_rejects_a_later_type(app):
    with app.app_context():
        user_id = _user("Seal")
        _elevation_route("policy-seal", "1000")
        created = create_private_expedition(
            user_id,
            slug="sealed",
            name="Sealed",
            route_slug="policy-seal",
            activity_policy={"preset": "cycling"},
        )
        database = get_db()
        with pytest.raises(Exception):
            database.execute(
                """
                INSERT INTO expedition_activity_types (expedition_id, activity_type)
                VALUES (?, 'hike')
                """,
                (created["expedition"]["id"],),
            )
            database.commit()
        database.rollback()


def _status(journey_id: int) -> str:
    return get_db().execute(
        "SELECT status FROM route_journeys WHERE id = ?",
        (journey_id,),
    ).fetchone()["status"]


def _choice(journey_id: int):
    return get_db().execute(
        "SELECT reason FROM journey_policy_choices WHERE journey_id = ?",
        (journey_id,),
    ).fetchone()


def _ledger(journey_id: int) -> list[tuple]:
    rows = get_db().execute(
        """
        SELECT activity.activity_type, credit.metric_code, credit.credited_amount,
               origin.credited_at
        FROM activity_journey_assignments AS assignment
        JOIN activities AS activity ON activity.id = assignment.activity_id
        LEFT JOIN journey_credits AS credit
          ON credit.assignment_id = assignment.id
        LEFT JOIN journey_credit_origins AS origin
          ON origin.assignment_id = assignment.id
         AND origin.metric_code = credit.metric_code
        WHERE assignment.journey_id = ?
        ORDER BY activity.id, credit.metric_code
        """,
        (journey_id,),
    ).fetchall()
    return [
        (row["activity_type"], row["metric_code"], row["credited_amount"], row["credited_at"])
        for row in rows
    ]


def _views(journey_id: int) -> int:
    return get_db().execute(
        "SELECT COUNT(*) AS n FROM journey_story_views WHERE journey_id = ?",
        (journey_id,),
    ).fetchone()["n"]


def test_restart_repoints_a_governed_journey_without_stranding_the_old_governor(app):
    with app.app_context():
        user_id = _user("Govern")
        route_id = _elevation_route("policy-govern", "1000")
        other_id = _elevation_route("policy-other", "1000")
        low = create_private_expedition(
            user_id,
            slug="govern-low",
            name="Low",
            route_slug="policy-govern",
            activity_policy={"mode": "restricted", "activity_types": ["hike", "walk"]},
        )
        high = create_private_expedition(
            user_id,
            slug="govern-high",
            name="High",
            route_slug="policy-govern",
            activity_policy={"mode": "restricted", "activity_types": ["hike", "walk"]},
        )
        wide = create_private_expedition(
            user_id,
            slug="govern-wide",
            name="Wide",
            route_slug="policy-govern",
        )
        journey_id = low["journey"]["id"]
        lowest = min(low["expedition"]["id"], high["expedition"]["id"])
        assert _policy(journey_id)[2] == lowest
        assert _policy(journey_id)[2] != wide["expedition"]["id"]
        _log(user_id, "40", "hike")
        kept = _ledger(journey_id)
        checkpoint_id = get_db().execute(
            "SELECT id FROM route_checkpoints WHERE route_id = ?",
            (route_id,),
        ).fetchone()["id"]
        get_db().execute(
            """
            INSERT INTO journey_story_views (
                journey_id, user_id, route_id, checkpoint_id
            )
            VALUES (?, ?, ?, ?)
            """,
            (journey_id, user_id, route_id, checkpoint_id),
        )
        get_db().commit()
        restarted = restart_journey(user_id, route_id)
        new_id = restarted["journey"]["id"]
        assert _status(journey_id) == "abandoned"
        assert _policy(journey_id)[2] is None
        assert _choice(journey_id) is None
        assert _policy(journey_id)[1] == ("hike", "walk")
        assert _ledger(journey_id) == kept
        assert _views(journey_id) == 1
        assert _ledger(new_id) == []
        assert _views(new_id) == 0
        assert _policy(new_id)[1] == ("hike", "walk")
        assert _policy(new_id)[2] == lowest
        assert _choice(new_id) is None
        assert restarted["journey"]["governing_expedition_slug"] == "govern-low"
        assert restarted["journey"]["activity_policy"]["mode"] == "restricted"
        assert restarted["journey"]["status"] == "active"
        start_journey(user_id, other_id)
        assert _status(new_id) == "paused"
        paused = restart_journey(user_id, route_id)
        assert _status(new_id) == "abandoned"
        assert _policy(new_id)[2] is None
        assert _choice(new_id) is None
        third = paused["journey"]["id"]
        assert _status(third) == "active"
        assert _policy(third)[2] == lowest
        assert active_journey(user_id)["route_id"] == route_id
        governor = _policy(third)[2]
        _log(user_id, "1000", "hike")
        finished_id = third
        restart_journey(user_id, route_id)
        assert _status(finished_id) == "completed"
        assert _policy(finished_id)[2] == governor


def test_expansion_of_a_paused_journey_leaves_both_routes_as_they_are(app):
    with app.app_context():
        user_id = _user("Pause")
        paused_route = _elevation_route("policy-paused", "1000")
        active_route = _elevation_route("policy-active", "1000")
        created = create_private_expedition(
            user_id,
            slug="paused-feet",
            name="Feet",
            route_slug="policy-paused",
            activity_policy={"mode": "restricted", "activity_types": ["hike"]},
        )
        journey_id = created["journey"]["id"]
        leave_expedition(user_id, "paused-feet")
        choose_activity_policy(user_id, journey_id, "keep", None)
        start_journey(user_id, active_route)
        assert _status(journey_id) == "paused"
        assert active_journey(user_id)["route_id"] == active_route
        with pytest.raises(Exception) as caught:
            choose_activity_policy(user_id, journey_id, "expand", None)
        preview = caught.value.details["policy_preview"]
        assert preview["lifecycle"] is None
        assert preview["will_resume"] is False
        assert preview["will_complete"] is False
        expanded = choose_activity_policy(
            user_id, journey_id, "expand", caught.value.details["confirmation_token"]
        )
        assert expanded["status"] == "paused"
        assert _policy(journey_id)[0] == "unrestricted"
        assert active_journey(user_id)["route_id"] == active_route
        assert _status(journey_id) == "paused"
        assert paused_route == get_db().execute(
            "SELECT route_id FROM route_journeys WHERE id = ?",
            (journey_id,),
        ).fetchone()["route_id"]


def test_a_completed_journey_rejects_an_eligibility_change(app):
    with app.app_context():
        user_id = _user("Frozen")
        route_id = _elevation_route("policy-frozen", "100")
        created = create_private_expedition(
            user_id,
            slug="frozen-feet",
            name="Feet",
            route_slug="policy-frozen",
            activity_policy={"mode": "restricted", "activity_types": ["hike", "walk"]},
        )
        journey_id = created["journey"]["id"]
        cycle = _log(user_id, "20", "cycle")
        hike = _log(user_id, "100", "hike")
        assert _status(journey_id) == "completed"
        checkpoint_id = get_db().execute(
            "SELECT id FROM route_checkpoints WHERE route_id = ?",
            (route_id,),
        ).fetchone()["id"]
        get_db().execute(
            """
            INSERT INTO journey_story_views (
                journey_id, user_id, route_id, checkpoint_id
            )
            VALUES (?, ?, ?, ?)
            """,
            (journey_id, user_id, route_id, checkpoint_id),
        )
        get_db().commit()
        before = (_ledger(journey_id), _version(journey_id), _views(journey_id), _status(journey_id))
        hike_id = hike["activity"]["id"]
        cycle_id = cycle["activity"]["id"]
        for activity_id, activity_type in ((hike_id, "cycle"), (cycle_id, "hike")):
            with pytest.raises(ActivityError) as preview:
                preview_activity_update(
                    user_id, activity_id, {"activity_type": activity_type}
                )
            assert preview.value.code == "invalid_state"
            with pytest.raises(ActivityError) as saved:
                update_activity(user_id, activity_id, {"activity_type": activity_type})
            assert saved.value.code == "invalid_state"
        assert (_ledger(journey_id), _version(journey_id), _views(journey_id), _status(journey_id)) == before
        update_activity(user_id, hike_id, {"activity_type": "walk", "note": "ridge"})
        update_activity(user_id, cycle_id, {"activity_type": "indoor_cycle"})
        after = _ledger(journey_id)
        assert [item[1:] for item in after] == [item[1:] for item in before[0]]
        assert [item[0] for item in after] == ["indoor_cycle", "walk"]
        assert (_version(journey_id), _views(journey_id), _status(journey_id)) == before[1:]
        kept = (_ledger(journey_id), _version(journey_id), _views(journey_id), _status(journey_id))
        database = get_db()
        for activity_id, activity_type in ((hike_id, "cycle"), (cycle_id, "hike")):
            with pytest.raises(Exception) as raw:
                database.execute(
                    "UPDATE activities SET activity_type = ? WHERE id = ?",
                    (activity_type, activity_id),
                )
            assert "activity eligibility on a completed journey cannot change" in str(raw.value)
            database.rollback()
        assert (_ledger(journey_id), _version(journey_id), _views(journey_id), _status(journey_id)) == kept


def test_a_narrowing_token_does_not_become_a_share(app):
    with app.app_context():
        user_id = _user("Share")
        _elevation_route("policy-share", "1000")
        create_private_expedition(
            user_id, slug="share-open", name="Open", route_slug="policy-share"
        )
        with pytest.raises(ExpeditionError) as first:
            create_private_expedition(
                user_id,
                slug="share-later",
                name="Later",
                route_slug="policy-share",
                activity_policy={"mode": "restricted", "activity_types": ["hike"]},
            )
        with pytest.raises(ExpeditionError) as second:
            create_private_expedition(
                user_id,
                slug="share-now",
                name="Now",
                route_slug="policy-share",
                activity_policy={"mode": "restricted", "activity_types": ["hike"]},
            )
        create_private_expedition(
            user_id,
            slug="share-now",
            name="Now",
            route_slug="policy-share",
            activity_policy={"mode": "restricted", "activity_types": ["hike"]},
            confirmation_token=second.value.details["confirmation_token"],
        )
        with pytest.raises(ExpeditionError) as stale:
            create_private_expedition(
                user_id,
                slug="share-later",
                name="Later",
                route_slug="policy-share",
                activity_policy={"mode": "restricted", "activity_types": ["hike"]},
                confirmation_token=first.value.details["confirmation_token"],
            )
        assert stale.value.code == "stale_preview"
        assert get_db().execute(
            "SELECT 1 FROM expeditions WHERE slug = 'share-later'"
        ).fetchone() is None
        assert _policy(_journey_id(user_id))[1] == ("hike",)


def test_a_policy_confirm_waits_out_an_activity_save_and_rejects_the_old_token(app):
    with app.app_context():
        user_id = _user("Race")
        _dual_route(
            "policy-race",
            {
                "elevation": _feet("1000"),
                "distance": parse_measurement("distance", "50"),
            },
        )
        create_private_expedition(
            user_id, slug="race-open", name="Open", route_slug="policy-race"
        )
        journey_id = _journey_id(user_id)
        first = _log(user_id, "600", "hike")
        _log(user_id, "400", "walk")
        with pytest.raises(ExpeditionError) as caught:
            create_private_expedition(
                user_id,
                slug="race-hike",
                name="Hike",
                route_slug="policy-race",
                activity_policy={"mode": "restricted", "activity_types": ["hike", "walk"]},
            )
        token = caught.value.details["confirmation_token"]
        version = _version(journey_id)

    started = threading.Event()
    late_done = threading.Event()
    holder: dict = {}

    def early() -> None:
        with app.app_context():
            try:
                get_db().execute("SELECT 1")
                started.set()
                assert late_done.wait(timeout=20)
                create_private_expedition(
                    user_id,
                    slug="race-hike",
                    name="Hike",
                    route_slug="policy-race",
                    activity_policy={"mode": "restricted", "activity_types": ["hike", "walk"]},
                    confirmation_token=token,
                )
                holder["wrote"] = True
            except Exception as exc:
                holder["early_error"] = exc
            finally:
                get_db().close()

    def late() -> None:
        with app.app_context():
            try:
                assert started.wait(timeout=20)
                holder["late"] = update_activity(
                    user_id, first["activity"]["id"], {"elevation_ft": "700"}
                )
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
    assert "late_error" not in holder, holder.get("late_error")
    assert "wrote" not in holder
    assert holder["early_error"].code == "stale_preview"
    with app.app_context():
        assert get_db().execute(
            "SELECT 1 FROM expeditions WHERE slug = 'race-hike'"
        ).fetchone() is None
        assert _policy(journey_id)[0] == "unrestricted"
        assert _credit(journey_id) == _feet("1000")
        assert _version(journey_id) == version + 1
        amounts = sorted(
            row["credited_amount"]
            for row in get_db().execute(
                """
                SELECT credited_amount
                FROM journey_credits
                WHERE journey_id = ? AND metric_code = 'elevation'
                """,
                (journey_id,),
            ).fetchall()
        )
        assert amounts == [_feet("300"), _feet("700")]


def test_http_policy_responses_reload_the_journey_and_keep_arrivals(client, app):
    with app.app_context():
        route_id = _elevation_route("policy-http", "100")
        checkpoint_id = get_db().execute(
            "SELECT id FROM route_checkpoints WHERE route_id = ?",
            (route_id,),
        ).fetchone()["id"]
        get_db().execute(
            """
            INSERT INTO checkpoint_stories (
                checkpoint_id, route_id, arrival_title, arrival_body, status
            )
            VALUES (?, ?, 'The ridge', 'The ridge opens.', 'published')
            """,
            (checkpoint_id, route_id),
        )
        get_db().commit()
    response = client.post(
        "/api/v1/auth/register",
        json={"name": "Http", "password": "trailhead"},
    )
    user_id = response.get_json()["you"]["id"]
    token = response.get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/expeditions",
        json={
            "slug": "http-feet",
            "name": "Feet",
            "route_slug": "policy-http",
            "activity_policy": {"mode": "restricted", "activity_types": ["hike"]},
        },
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()
    body = created.get_json()
    journey = body["journey"]
    assert journey["activity_policy"]["mode"] == "restricted"
    assert journey["governing_expedition_slug"] == "http-feet"
    assert body["arrivals"] == []
    journey_id = journey["id"]
    for feet, activity_type in (("60", "cycle"), ("40", "hike")):
        saved = client.post(
            "/api/v1/activities",
            json={"elevation_ft": feet, "happened_on": DAY, "activity_type": activity_type},
            headers=headers,
        )
        assert saved.status_code == 201, saved.get_json()
        assert "activity_policy" in saved.get_json()["journey"]
    left = client.post("/api/v1/expeditions/http-feet/leave", headers=headers)
    assert left.status_code == 200, left.get_json()
    kept = client.post(
        f"/api/v1/journeys/{journey_id}/activity-policy",
        json={"choice": "keep"},
        headers=headers,
    )
    assert kept.status_code == 200, kept.get_json()
    opened = client.post(
        "/api/v1/expeditions",
        json={"slug": "http-open", "name": "Open", "route_slug": "policy-http"},
        headers=headers,
    )
    assert opened.status_code == 201, opened.get_json()
    with app.app_context():
        before = {
            row["kind"]: row["n"]
            for row in get_db().execute(
                """
                SELECT kind, COUNT(*) AS n
                FROM expedition_updates
                WHERE user_id = ?
                GROUP BY kind
                """,
                (user_id,),
            ).fetchall()
        }
    preview = client.post(
        f"/api/v1/journeys/{journey_id}/activity-policy/preview",
        json={"operation": "expand"},
        headers=headers,
    )
    assert preview.status_code == 200, preview.get_json()
    assert preview.get_json()["policy_preview"]["will_complete"] is True
    assert preview.get_json()["policy_preview"]["will_resume"] is False
    missing = client.post(
        f"/api/v1/journeys/{journey_id}/activity-policy",
        json={"choice": "expand"},
        headers=headers,
    )
    assert missing.status_code == 409
    assert missing.get_json()["code"] == "confirmation_required"
    confirmed = client.post(
        f"/api/v1/journeys/{journey_id}/activity-policy",
        json={
            "choice": "expand",
            "confirmation_token": preview.get_json()["confirmation_token"],
        },
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    finished = confirmed.get_json()
    assert finished["journey"]["status"] == "completed"
    assert finished["journey"]["activity_policy"]["mode"] == "unrestricted"
    assert finished["journey"]["governing_expedition_slug"] is None
    assert finished["arrivals"][0]["arrival_title"] == "The ridge"
    with app.app_context():
        kinds = {
            row["kind"]: row["n"]
            for row in get_db().execute(
                """
                SELECT kind, COUNT(*) AS n
                FROM expedition_updates
                WHERE user_id = ?
                GROUP BY kind
                """,
                (user_id,),
            ).fetchall()
        }
    assert kinds.get("activity_logged", 0) == before.get("activity_logged", 0)
    assert kinds.get("checkpoint_reached", 0) == before.get("checkpoint_reached", 0)
    assert kinds.get("summit_reached", 0) == before.get("summit_reached", 0) + 1
    home = client.get("/api/v1/home", headers=headers)
    assert home.status_code == 200
    assert home.get_json()["focus"]["activity_policy"]["mode"] == "unrestricted"
    restarted = client.post(
        "/api/v1/journeys/restart",
        json={"route_slug": "policy-http"},
        headers=headers,
    )
    assert restarted.status_code == 200, restarted.get_json()
    assert restarted.get_json()["journey"]["attempt_number"] == 2
    assert restarted.get_json()["journey"]["activity_policy"]["mode"] == "unrestricted"
    assert restarted.get_json()["journey"]["eligibility_expansion"]["available"] is False
