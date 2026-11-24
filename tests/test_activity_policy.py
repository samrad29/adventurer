"""Release 2 expedition activity rules, confirmation, and succession.

Runs against the isolated PostgreSQL database named by TEST_DATABASE_URL.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from adventurer.activities import create_activity, preview_activity, update_activity
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
from adventurer.journeys import choose_activity_policy, restart_journey, start_journey
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
