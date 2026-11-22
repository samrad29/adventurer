"""Expedition services against the isolated test database."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import psycopg

from adventurer.activities import create_activity, preview_activity
from adventurer.db import DB, get_db
from adventurer.expeditions import (
    ExpeditionError,
    accept_invitation,
    add_organization_member,
    archive_private_expedition,
    complete_organization_expedition,
    create_organization,
    create_organization_expedition,
    create_private_expedition,
    create_share_link,
    create_username_invitation,
    decline_invitation,
    extend_expedition,
    get_expedition,
    get_leaderboard,
    get_roster,
    get_updates,
    join_expedition,
    leave_expedition,
    list_expedition_invitations,
    list_organization_members,
    remove_participation_privacy,
    suspend_organization,
    transfer_ownership,
)
from adventurer.journeys import restart_journey, start_journey, switch_journey
from adventurer.measures import feet_to_meters
from adventurer.services import create_adventurer

DAY = "2026-01-02"


def _user(name: str) -> int:
    return create_adventurer(name, "trailhead")["id"]


def _route(slug: str, feet: str = "1000") -> int:
    database = get_db()
    row = database.execute(
        """
        INSERT INTO routes (slug, name, region, summary, target_elevation_m, status)
        VALUES (?, ?, 'Test', 'A test route', ?, 'published')
        RETURNING id
        """,
        (slug, slug, feet_to_meters(Decimal(feet))),
    ).fetchone()
    database.commit()
    return row["id"]


def _checkpoint(route_id: int, feet: str, name: str = "Camp") -> None:
    database = get_db()
    database.execute(
        """
        INSERT INTO route_checkpoints (route_id, slug, name, position, elevation_m)
        VALUES (?, ?, ?, 1, ?)
        """,
        (route_id, name.casefold(), name, feet_to_meters(Decimal(feet))),
    )
    database.commit()


def _fields(feet: str) -> dict:
    return {"elevation_ft": Decimal(feet), "happened_on": DAY}


def _log(user_id: int, feet: str) -> dict:
    fields = _fields(feet)
    preview = preview_activity(user_id, fields)
    return create_activity(
        user_id,
        fields,
        confirmation_token=preview["confirmation_token"],
    )


def _journeys(user_id: int) -> list[dict]:
    return list(
        get_db().execute(
            """
            SELECT id, route_id, status, attempt_number, is_current
            FROM route_journeys
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()
    )


def _journey_total(journey_id: int) -> Decimal:
    row = get_db().execute(
        "SELECT COALESCE(SUM(credited_elevation_m), 0) AS total_m FROM journey_credits WHERE journey_id = ?",
        (journey_id,),
    ).fetchone()
    return row["total_m"]


def _count(table: str, where: str = "TRUE", params: tuple = ()) -> int:
    if table not in {
        "activities",
        "journey_credits",
        "route_journeys",
        "expedition_participations",
        "expedition_updates",
        "expedition_result_entries",
        "expeditions",
        "organization_memberships",
        "expedition_invitations",
        "expedition_privacy_removals",
    }:
        raise AssertionError(table)
    row = get_db().execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE {where}",
        params,
    ).fetchone()
    return row["count"]


def _participation(user_id: int, slug: str):
    return get_db().execute(
        """
        SELECT participation.id, participation.status, participation.journey_id,
               participation.final_standings_eligible, participation.retained_read_access,
               participation.display_credits_after
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ? AND expedition.slug = ?
        """,
        (user_id, slug),
    ).fetchone()


def _org(app, admin_id: int, slug: str, owner_name: str) -> None:
    app.config["PLATFORM_ADMIN_IDS"] = {admin_id}
    create_organization(admin_id, slug=slug, name=slug, owner_username=owner_name)


def _season(owner_id: int, org_slug: str, slug: str, route_slug: str, *, enrollment: str = "open", ends_at=None):
    if ends_at is None:
        ends_at = datetime.now(timezone.utc) + timedelta(days=7)
    return create_organization_expedition(
        owner_id,
        org_slug,
        slug=slug,
        name=slug,
        route_slug=route_slug,
        enrollment=enrollment,
        ends_at=ends_at,
        starts_at=ends_at - timedelta(days=1),
    )


def _pause_at_user_lock(monkeypatch):
    main = get_db()
    reached = threading.Event()
    allow = threading.Event()
    original = DB.execute
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
        return original(self, sql, params)

    monkeypatch.setattr(DB, "execute", wrapped)
    return reached, allow


def _pause_after_organization_lock(monkeypatch):
    main = get_db()
    reached = threading.Event()
    allow = threading.Event()
    original = DB.execute
    seen = {"paused": False}

    def wrapped(self, sql, params=()):
        normalized = " ".join(sql.split()).lower()
        if (
            not seen["paused"]
            and self is not main
            and "for update" in normalized
            and "from organizations" in normalized
            and "where slug" in normalized
        ):
            seen["paused"] = True
            outcome = original(self, sql, params)
            reached.set()
            assert allow.wait(timeout=15)
            return outcome
        return original(self, sql, params)

    monkeypatch.setattr(DB, "execute", wrapped)
    return reached, allow


def _wait_for_organization_lock_wait() -> None:
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://postgres:postgres@127.0.0.1:54329/adventurer_migration_test",
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with psycopg.connect(url, connect_timeout=3) as conn:
            waiting = conn.execute(
                """
                SELECT 1
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND state = 'active'
                  AND wait_event_type = 'Lock'
                  AND query ILIKE '%from organizations where slug%'
                  AND query ILIKE '%for update%'
                LIMIT 1
                """
            ).fetchone()
        if waiting is not None:
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the organization lock")


def test_private_create_activates_the_owner_journey_or_rolls_back(app):
    with app.app_context():
        user_id = _user("Sam")
        _route("kili")
        created = create_private_expedition(
            user_id,
            slug="sam-kili",
            name="Sam Kili",
            route_slug="kili",
        )
        assert created["journey"]["status"] == "active"
        assert created["journey"]["attempt_number"] == 1
        assert created["participation"]["status"] == "active"
        assert created["participation"]["journey_id"] == created["journey"]["id"]
        assert _count("expeditions", "slug = ?", ("sam-kili",)) == 1
        _log(user_id, "1000")
        with pytest.raises(ExpeditionError) as caught:
            create_private_expedition(
                user_id,
                slug="sam-again",
                name="Sam Again",
                route_slug="kili",
            )
        assert caught.value.code == "restart_required"
        assert caught.value.status == 409
        assert caught.value.details["route_slug"] == "kili"
        assert _count("expeditions", "slug = ?", ("sam-again",)) == 0
        assert _journeys(user_id)[-1]["status"] == "completed"
        confirmed = create_private_expedition(
            user_id,
            slug="sam-again",
            name="Sam Again",
            route_slug="kili",
            confirm_new_attempt=True,
        )
        assert confirmed["journey"]["attempt_number"] == 2
        assert confirmed["participation"]["journey_id"] == confirmed["journey"]["id"]


def test_joining_a_new_route_pauses_the_previous_journey(app):
    with app.app_context():
        user_id = _user("Sam")
        _route("kili")
        join_expedition(user_id, "everest")
        _log(user_id, "100")
        create_private_expedition(user_id, slug="kili-trip", name="Kili Trip", route_slug="kili")
        journeys = _journeys(user_id)
        everest = next(row for row in journeys if row["attempt_number"] == 1 and row["status"] == "paused")
        kili = next(row for row in journeys if row["status"] == "active")
        assert kili["attempt_number"] == 1
        assert _journey_total(everest["id"]) == feet_to_meters(Decimal("100"))


def test_joining_a_paused_route_keeps_its_progress(app):
    with app.app_context():
        user_id = _user("Sam")
        kili_id = _route("kili")
        join_expedition(user_id, "everest")
        _log(user_id, "100")
        switch_journey(user_id, kili_id)
        resumed = join_expedition(user_id, "everest")
        assert resumed["journey"]["status"] == "active"
        assert _journey_total(resumed["journey"]["id"]) == feet_to_meters(Decimal("100"))
        paused = next(row for row in _journeys(user_id) if row["status"] == "paused")
        assert paused["route_id"] != resumed["journey"]["route_id"]


def test_a_finished_route_requires_restart_confirmation(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("kili", "500")
        start_journey(user_id, route_id)
        _log(user_id, "500")
        with pytest.raises(ExpeditionError) as caught:
            join_expedition(user_id, "everest") if False else create_private_expedition(
                user_id, slug="after", name="After", route_slug="kili"
            )
        assert caught.value.code == "restart_required"
        assert _count("expedition_participations", "user_id = ?", (user_id,)) == 0
        created = create_private_expedition(
            user_id,
            slug="after",
            name="After",
            route_slug="kili",
            confirm_new_attempt=True,
        )
        assert created["journey"]["attempt_number"] == 2
        assert created["journey"]["status"] == "active"


def test_a_failed_join_rolls_back_the_journey_transition(app):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _org(app, admin_id, "ridge-club", "Owen")
        _route("alpha")
        _route("beta")
        _season(owner_id, "ridge-club", "alpha-season", "alpha")
        _season(owner_id, "ridge-club", "beta-season", "beta")
        join_expedition(sam_id, "alpha-season")
        with pytest.raises(ExpeditionError) as caught:
            join_expedition(sam_id, "beta-season")
        assert caught.value.code == "slot_occupied"
        journeys = _journeys(sam_id)
        assert len(journeys) == 1
        assert journeys[0]["status"] == "active"
        assert _participation(sam_id, "beta-season") is None
        assert _participation(sam_id, "alpha-season")["status"] == "active"


def test_open_enrollment_does_not_create_a_membership(app):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _org(app, admin_id, "ridge-club", "Owen")
        _route("alpha")
        _season(owner_id, "ridge-club", "open-season", "alpha", enrollment="open")
        join_expedition(sam_id, "open-season")
        assert _count(
            "organization_memberships",
            "user_id = ?",
            (sam_id,),
        ) == 0
        assert get_expedition(sam_id, "open-season")["enrollment"] == "open"
        with pytest.raises(ExpeditionError) as caught:
            get_roster(_user("Rita"), "open-season")
        assert caught.value.code == "forbidden"


def test_invitation_only_enrollment_cannot_be_joined_directly(app):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _org(app, admin_id, "ridge-club", "Owen")
        _route("alpha")
        _season(owner_id, "ridge-club", "closed-season", "alpha", enrollment="invitation")
        missing = None
        try:
            get_expedition(sam_id, "missing-season")
        except ExpeditionError as exc:
            missing = exc
        with pytest.raises(ExpeditionError) as caught:
            join_expedition(sam_id, "closed-season")
        assert caught.value.code == missing.code
        assert caught.value.status == missing.status
        assert str(caught.value) == str(missing)
        with pytest.raises(ExpeditionError) as owner_join:
            join_expedition(owner_id, "closed-season")
        assert owner_join.value.code == "forbidden"
        invite = create_username_invitation(owner_id, "closed-season", "Sam")
        accepted = accept_invitation(sam_id, invitation_id=invite["id"])
        assert accepted["participation"]["status"] == "active"
        assert _count("organization_memberships", "user_id = ?", (sam_id,)) == 0


def test_the_organization_slot_ignores_private_and_global_expeditions(app):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _org(app, admin_id, "ridge-club", "Owen")
        _org(app, admin_id, "other-club", "Owen")
        _route("alpha")
        _route("beta")
        _route("kili")
        _season(owner_id, "ridge-club", "alpha-season", "alpha")
        _season(owner_id, "other-club", "beta-season", "beta")
        join_expedition(sam_id, "alpha-season")
        join_expedition(sam_id, "everest")
        create_private_expedition(sam_id, slug="sam-kili", name="Sam Kili", route_slug="kili")
        with pytest.raises(ExpeditionError) as caught:
            join_expedition(sam_id, "beta-season")
        assert caught.value.code == "slot_occupied"
        assert _participation(sam_id, "everest")["status"] == "active"
        assert _participation(sam_id, "sam-kili")["status"] == "active"


def test_accept_and_journey_activation_commit_together(app):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        route_id = _route("kili", "500")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        start_journey(sam_id, route_id)
        _log(sam_id, "500")
        invite = create_username_invitation(owner_id, "owen-kili", "Sam")
        with pytest.raises(ExpeditionError) as caught:
            accept_invitation(sam_id, invitation_id=invite["id"])
        assert caught.value.code == "restart_required"
        assert get_db().execute(
            "SELECT status FROM expedition_invitations WHERE id = ?",
            (invite["id"],),
        ).fetchone()["status"] == "pending"
        assert _journeys(sam_id)[0]["status"] == "completed"
        accepted = accept_invitation(
            sam_id, invitation_id=invite["id"], confirm_new_attempt=True
        )
        assert accepted["journey"]["attempt_number"] == 2
        assert get_db().execute(
            "SELECT status FROM expedition_invitations WHERE id = ?",
            (invite["id"],),
        ).fetchone()["status"] == "accepted"


def test_a_share_link_can_be_accepted_by_more_than_one_person(app):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        rita_id = _user("Rita")
        _route("kili")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        link = create_share_link(owner_id, "owen-kili")
        assert link["token"]
        again = create_share_link(owner_id, "owen-kili")
        assert again["token"] is None
        assert again["id"] == link["id"]
        accept_invitation(sam_id, token=link["token"])
        accept_invitation(rita_id, token=link["token"])
        stored = get_db().execute(
            "SELECT status FROM expedition_invitations WHERE id = ?",
            (link["id"],),
        ).fetchone()
        assert stored["status"] == "pending"
        assert _participation(sam_id, "owen-kili")["status"] == "active"
        assert _participation(rita_id, "owen-kili")["status"] == "active"
        with pytest.raises(ExpeditionError) as caught:
            decline_invitation(sam_id, link["id"])
        assert caught.value.code == "invalid_state"


def test_an_expired_username_invitation_is_replaced(app):
    with app.app_context():
        owner_id = _user("Owen")
        _user("Sam")
        _route("kili")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        first = create_username_invitation(owner_id, "owen-kili", "Sam")
        database = get_db()
        database.execute(
            "UPDATE expedition_invitations SET expires_at = now() - interval '1 minute' WHERE id = ?",
            (first["id"],),
        )
        database.commit()
        second = create_username_invitation(owner_id, "owen-kili", "Sam")
        assert second["id"] != first["id"]
        assert second["token"]
        statuses = {
            row["id"]: row["status"]
            for row in get_db().execute(
                "SELECT id, status FROM expedition_invitations WHERE expedition_id = (SELECT id FROM expeditions WHERE slug = 'owen-kili')"
            ).fetchall()
        }
        assert statuses[first["id"]] == "expired"
        assert statuses[second["id"]] == "pending"


def test_leave_sets_the_two_flags_independently(app):
    with app.app_context():
        user_id = _user("Sam")
        route_id = _route("kili", "500")
        _checkpoint(route_id, "100")
        create_private_expedition(user_id, slug="sam-kili", name="Sam Kili", route_slug="kili")
        early = leave_expedition(user_id, "sam-kili")
        assert early["final_standings_eligible"] is False
        assert early["retained_read_access"] is False
        join_expedition(user_id, "sam-kili")
        _log(user_id, "100")
        database = get_db()
        database.execute(
            """
            UPDATE route_journeys
            SET status = 'completed', completed_at = now()
            WHERE user_id = ? AND status = 'active'
            """,
            (user_id,),
        )
        database.commit()
        untouched = _participation(user_id, "sam-kili")
        assert untouched["final_standings_eligible"] is False
        assert untouched["retained_read_access"] is False
        restart_journey(user_id, route_id)
        recomputed = leave_expedition(user_id, "sam-kili")
        assert recomputed["final_standings_eligible"] is False
        assert recomputed["retained_read_access"] is True
        join_expedition(user_id, "sam-kili")
        _log(user_id, "500")
        finished = leave_expedition(user_id, "sam-kili")
        assert finished["final_standings_eligible"] is True
        assert finished["retained_read_access"] is True


def test_restart_does_not_repoint_withdrawn_or_closed_expeditions(app):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        route_id = _route("kili", "500")
        create_private_expedition(owner_id, slug="live-kili", name="Live Kili", route_slug="kili")
        create_private_expedition(owner_id, slug="old-kili", name="Old Kili", route_slug="kili")
        invite = create_share_link(owner_id, "live-kili")
        accept_invitation(sam_id, token=invite["token"])
        other = create_share_link(owner_id, "old-kili")
        accept_invitation(sam_id, token=other["token"])
        _log(sam_id, "500")
        leave_expedition(sam_id, "live-kili")
        withdrawn_journey = _participation(sam_id, "live-kili")["journey_id"]
        archive_private_expedition(owner_id, "old-kili")
        closed_journey = _participation(sam_id, "old-kili")["journey_id"]
        closed_result = get_db().execute(
            """
            SELECT journey_id, total_m
            FROM expedition_result_entries
            WHERE user_id = ?
              AND expedition_id = (SELECT id FROM expeditions WHERE slug = 'old-kili')
            """,
            (sam_id,),
        ).fetchone()
        restart_journey(sam_id, route_id)
        assert _participation(sam_id, "live-kili")["journey_id"] == withdrawn_journey
        assert _participation(sam_id, "old-kili")["journey_id"] == closed_journey
        assert get_db().execute(
            """
            SELECT journey_id, total_m
            FROM expedition_result_entries
            WHERE user_id = ?
              AND expedition_id = (SELECT id FROM expeditions WHERE slug = 'old-kili')
            """,
            (sam_id,),
        ).fetchone() == closed_result


def test_close_keeps_zeros_and_departed_finishers(app):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        finisher_id = _user("Fay")
        leaver_id = _user("Lee")
        zero_id = _user("Zed")
        _org(app, admin_id, "ridge-club", "Owen")
        route_id = _route("kili", "500")
        _checkpoint(route_id, "100", "Hut")
        _season(owner_id, "ridge-club", "kili-season", "kili")
        for user_id in (finisher_id, leaver_id, zero_id):
            join_expedition(user_id, "kili-season")
        switch_journey(finisher_id, route_id)
        _log(finisher_id, "500")
        leave_expedition(finisher_id, "kili-season")
        switch_journey(leaver_id, route_id)
        _log(leaver_id, "100")
        leave_expedition(leaver_id, "kili-season")
        closed = complete_organization_expedition(owner_id, "kili-season")
        by_user = {row["user_id"]: row for row in closed["entries"]}
        assert zero_id in by_user
        assert by_user[zero_id]["total_m"] == 0
        assert by_user[zero_id]["checkpoint_name"] == "Start"
        assert finisher_id in by_user
        assert by_user[finisher_id]["checkpoint_name"] == "Hut"
        assert leaver_id not in by_user
        assert [by_user[finisher_id]["rank"], by_user[zero_id]["rank"]] == [1, 2]
        board = get_leaderboard(owner_id, "kili-season")
        assert board["mode"] == "final"
        assert board["entries"] == closed["entries"]
        restart_journey(finisher_id, route_id)
        assert get_leaderboard(owner_id, "kili-season")["entries"] == closed["entries"]


def test_live_and_final_boards_use_different_sources(app):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        route_id = _route("kili", "1000")
        _checkpoint(route_id, "100", "Hut")
        create_private_expedition(owner_id, slug="sam-kili", name="Sam Kili", route_slug="kili")
        link = create_share_link(owner_id, "sam-kili")
        accept_invitation(sam_id, token=link["token"])
        _log(sam_id, "100")
        live = get_leaderboard(sam_id, "sam-kili")
        assert live["mode"] == "live"
        mine = next(row for row in live["entries"] if row["user_id"] == sam_id)
        assert mine["total_m"] == feet_to_meters(Decimal("100"))
        assert mine["checkpoint_name"] == "Hut"
        assert mine["journey_id"] == _participation(sam_id, "sam-kili")["journey_id"]
        owner = next(row for row in live["entries"] if row["user_id"] == owner_id)
        assert owner["total_m"] == 0
        remove_participation_privacy(sam_id, "sam-kili", confirm=True)
        rejoined = accept_invitation(sam_id, token=link["token"])
        assert rejoined["participation"]["display_credits_after"] is not None
        hidden = get_leaderboard(owner_id, "sam-kili")
        rejoined_row = next(row for row in hidden["entries"] if row["user_id"] == sam_id)
        assert rejoined_row["total_m"] == 0
        assert _journey_total(rejoined["journey"]["id"]) == feet_to_meters(Decimal("100"))
        _log(sam_id, "100")
        shown = next(
            row for row in get_leaderboard(owner_id, "sam-kili")["entries"] if row["user_id"] == sam_id
        )
        assert shown["total_m"] == feet_to_meters(Decimal("100"))
        assert _journey_total(rejoined["journey"]["id"]) == feet_to_meters(Decimal("200"))


def test_privacy_removal_leaves_a_rank_gap_and_personal_progress(app):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        first_id = _user("Ann")
        second_id = _user("Bea")
        third_id = _user("Cy")
        _org(app, admin_id, "ridge-club", "Owen")
        route_id = _route("kili", "1000")
        _season(owner_id, "ridge-club", "kili-season", "kili")
        for user_id, feet in ((first_id, "300"), (second_id, "200"), (third_id, "100")):
            join_expedition(user_id, "kili-season")
            switch_journey(user_id, route_id)
            _log(user_id, feet)
        complete_organization_expedition(owner_id, "kili-season")
        before = _count("activities", "user_id = ?", (second_id,))
        credits = _count("journey_credits", "user_id = ?", (second_id,))
        journeys = _count("route_journeys", "user_id = ?", (second_id,))
        remove_participation_privacy(second_id, "kili-season", confirm=True)
        ranks = {
            row["user_id"]: row["rank"]
            for row in get_leaderboard(owner_id, "kili-season")["entries"]
        }
        assert ranks == {first_id: 1, third_id: 3}
        assert _count("activities", "user_id = ?", (second_id,)) == before
        assert _count("journey_credits", "user_id = ?", (second_id,)) == credits
        assert _count("route_journeys", "user_id = ?", (second_id,)) == journeys
        assert _participation(second_id, "kili-season") is None
        assert _count(
            "expedition_updates",
            "user_id = ? AND expedition_id = (SELECT id FROM expeditions WHERE slug = 'kili-season')",
            (second_id,),
        ) == 0


def test_authorization_hides_private_expeditions(app):
    with app.app_context():
        owner_id = _user("Owen")
        member_id = _user("Sam")
        stranger_id = _user("Rita")
        _route("kili")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        link = create_share_link(owner_id, "owen-kili")
        accept_invitation(member_id, token=link["token"])
        leave_expedition(member_id, "owen-kili")
        missing = None
        try:
            get_expedition(stranger_id, "missing-expedition")
        except ExpeditionError as exc:
            missing = exc
        for viewer in (stranger_id, member_id):
            with pytest.raises(ExpeditionError) as caught:
                get_expedition(viewer, "owen-kili")
            assert caught.value.code == missing.code
            assert caught.value.status == missing.status
            assert str(caught.value) == str(missing)
        assert get_roster(owner_id, "owen-kili")
        _log(owner_id, "1000")
        leave_expedition(owner_id, "owen-kili")
        assert get_expedition(owner_id, "owen-kili")["owner"] is True
        invite = create_username_invitation(owner_id, "owen-kili", "Sam")
        accept_invitation(member_id, invitation_id=invite["id"])
        _log(member_id, "1000")
        leave_expedition(member_id, "owen-kili")
        assert get_roster(member_id, "owen-kili")


def test_lazy_completion_releases_the_organization_slot(app):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _org(app, admin_id, "ridge-club", "Owen")
        _org(app, admin_id, "other-club", "Owen")
        _route("alpha")
        _route("beta")
        _season(owner_id, "ridge-club", "alpha-season", "alpha")
        _season(owner_id, "other-club", "beta-season", "beta")
        join_expedition(sam_id, "alpha-season")
        database = get_db()
        database.execute(
            """
            UPDATE expeditions
            SET ends_at = now() - interval '1 minute',
                starts_at = now() - interval '2 minutes'
            WHERE slug = 'alpha-season'
            """
        )
        database.commit()
        join_expedition(sam_id, "beta-season")
        assert get_expedition(owner_id, "alpha-season")["status"] == "completed"
        assert _participation(sam_id, "beta-season")["status"] == "active"
        assert _participation(sam_id, "alpha-season")["status"] == "active"


def test_transfer_release_and_suspend(app):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        member_id = _user("Sam")
        finisher_id = _user("Fay")
        _org(app, admin_id, "ridge-club", "Owen")
        route_id = _route("kili", "500")
        _route("other")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        link = create_share_link(owner_id, "owen-kili")
        accept_invitation(member_id, token=link["token"])
        with pytest.raises(ExpeditionError) as blocked:
            remove_participation_privacy(owner_id, "owen-kili", confirm=True)
        assert blocked.value.code == "ownership_transfer_required"
        assert _participation(owner_id, "owen-kili")["status"] == "active"
        transferred = transfer_ownership(owner_id, "owen-kili", member_id)
        assert transferred["owner"] is False
        remove_participation_privacy(owner_id, "owen-kili", confirm=True)
        assert _participation(owner_id, "owen-kili") is None
        assert get_expedition(member_id, "owen-kili")["owner"] is True

        create_private_expedition(finisher_id, slug="fay-kili", name="Fay Kili", route_slug="kili")
        finish_link = create_share_link(finisher_id, "fay-kili")
        accept_invitation(member_id, token=finish_link["token"], confirm_new_attempt=True)
        switch_journey(member_id, route_id)
        _log(member_id, "500")
        leave_expedition(member_id, "fay-kili")
        remove_participation_privacy(finisher_id, "fay-kili", confirm=True)
        archived = get_expedition(member_id, "fay-kili")
        assert archived["status"] == "archived"
        assert archived["owner"] is False
        assert [row["user_id"] for row in get_leaderboard(member_id, "fay-kili")["entries"]] == [
            member_id
        ]

        _season(owner_id, "ridge-club", "club-season", "other")
        join_expedition(member_id, "club-season")
        add_organization_member(owner_id, "ridge-club", "Sam")
        later = datetime.now(timezone.utc) + timedelta(days=30)
        extended = extend_expedition(owner_id, "club-season", later)
        assert extended["ends_at"] == later
        suspend_organization(admin_id, "ridge-club")
        assert get_expedition(owner_id, "club-season")["status"] == "completed"
        assert get_db().execute(
            "SELECT status FROM organizations WHERE slug = 'ridge-club'"
        ).fetchone()["status"] == "suspended"
        with pytest.raises(ExpeditionError) as caught:
            list_organization_members(member_id, "ridge-club")
        assert caught.value.code == "forbidden"


def test_an_activity_that_commits_first_is_in_the_snapshot(app, monkeypatch):
    with app.app_context():
        owner_id = _user("Owen")
        _route("kili", "1000")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        reached, allow = _pause_at_user_lock(monkeypatch)
        holder = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["result"] = archive_private_expedition(owner_id, "owen-kili")
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            _log(owner_id, "100")
        finally:
            allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert "error" not in holder
        assert holder["result"]["entries"][0]["total_m"] == feet_to_meters(Decimal("100"))
        assert get_updates(owner_id, "owen-kili")


def test_a_close_that_commits_first_keeps_the_snapshot(app, monkeypatch):
    with app.app_context():
        owner_id = _user("Owen")
        _route("kili", "1000")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        reached, allow = _pause_at_user_lock(monkeypatch)
        holder = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["result"] = _log(owner_id, "100")
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            closed = archive_private_expedition(owner_id, "owen-kili")
        finally:
            allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert "error" not in holder
        assert closed["entries"][0]["total_m"] == 0
        assert get_updates(owner_id, "owen-kili") == []
        assert _journey_total(holder["result"]["journey"]["id"]) == feet_to_meters(Decimal("100"))
        assert get_leaderboard(owner_id, "owen-kili")["entries"][0]["total_m"] == 0


def test_leave_and_close_follow_whoever_commits(app, monkeypatch):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _route("kili")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        link = create_share_link(owner_id, "owen-kili")
        accept_invitation(sam_id, token=link["token"])
        reached, allow = _pause_at_user_lock(monkeypatch)
        holder = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["result"] = archive_private_expedition(owner_id, "owen-kili")
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            leave_expedition(sam_id, "owen-kili")
        finally:
            allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert "error" not in holder
        assert sam_id not in {row["user_id"] for row in holder["result"]["entries"]}


def test_close_before_leave_keeps_the_active_snapshot(app, monkeypatch):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _route("kili")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        link = create_share_link(owner_id, "owen-kili")
        accept_invitation(sam_id, token=link["token"])
        reached, allow = _pause_at_user_lock(monkeypatch)
        holder = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["left"] = leave_expedition(sam_id, "owen-kili")
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            closed = archive_private_expedition(owner_id, "owen-kili")
        finally:
            allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert "error" not in holder
        assert sam_id in {row["user_id"] for row in closed["entries"]}
        assert sam_id in {
            row["user_id"] for row in get_leaderboard(owner_id, "owen-kili")["entries"]
        }


def test_restart_before_close_snapshots_the_new_journey(app, monkeypatch):
    with app.app_context():
        owner_id = _user("Owen")
        route_id = _route("kili", "1000")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        _log(owner_id, "100")
        reached, allow = _pause_at_user_lock(monkeypatch)
        holder = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["result"] = archive_private_expedition(owner_id, "owen-kili")
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            restart_journey(owner_id, route_id)
        finally:
            allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert "error" not in holder
        entry = holder["result"]["entries"][0]
        assert entry["total_m"] == 0
        assert entry["journey_id"] == _participation(owner_id, "owen-kili")["journey_id"]


def test_close_before_restart_keeps_the_old_journey(app, monkeypatch):
    with app.app_context():
        owner_id = _user("Owen")
        route_id = _route("kili", "1000")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        logged = _log(owner_id, "100")
        reached, allow = _pause_at_user_lock(monkeypatch)
        holder = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["result"] = restart_journey(owner_id, route_id)
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            closed = archive_private_expedition(owner_id, "owen-kili")
        finally:
            allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert "error" not in holder
        entry = closed["entries"][0]
        assert entry["journey_id"] == logged["journey"]["id"]
        assert entry["total_m"] == feet_to_meters(Decimal("100"))
        assert _participation(owner_id, "owen-kili")["journey_id"] == logged["journey"]["id"]


def test_privacy_removal_before_close_drops_the_person(app, monkeypatch):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _route("kili")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        link = create_share_link(owner_id, "owen-kili")
        accept_invitation(sam_id, token=link["token"])
        reached, allow = _pause_at_user_lock(monkeypatch)
        holder = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["result"] = archive_private_expedition(owner_id, "owen-kili")
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            remove_participation_privacy(sam_id, "owen-kili", confirm=True)
        finally:
            allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert "error" not in holder
        assert sam_id not in {row["user_id"] for row in holder["result"]["entries"]}


def test_close_before_privacy_removal_deletes_only_that_result(app, monkeypatch):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        route_id = _route("kili", "1000")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        link = create_share_link(owner_id, "owen-kili")
        accept_invitation(sam_id, token=link["token"])
        switch_journey(owner_id, route_id)
        _log(owner_id, "200")
        switch_journey(sam_id, route_id)
        _log(sam_id, "100")
        reached, allow = _pause_at_user_lock(monkeypatch)
        holder = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["result"] = remove_participation_privacy(sam_id, "owen-kili", confirm=True)
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            closed = archive_private_expedition(owner_id, "owen-kili")
        finally:
            allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert "error" not in holder
        owner_rank = next(row["rank"] for row in closed["entries"] if row["user_id"] == owner_id)
        remaining = get_leaderboard(owner_id, "owen-kili")["entries"]
        assert [row["user_id"] for row in remaining] == [owner_id]
        assert remaining[0]["rank"] == owner_rank


def test_transfer_before_privacy_removal_releases_the_former_owner(app, monkeypatch):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _route("kili")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        link = create_share_link(owner_id, "owen-kili")
        accept_invitation(sam_id, token=link["token"])
        reached, allow = _pause_at_user_lock(monkeypatch)
        holder = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["result"] = remove_participation_privacy(owner_id, "owen-kili", confirm=True)
                except Exception as exc:
                    holder["error"] = repr(exc)
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            transfer_ownership(owner_id, "owen-kili", sam_id)
        finally:
            allow.set()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert holder.get("error") is None
        assert _participation(owner_id, "owen-kili") is None
        assert get_expedition(sam_id, "owen-kili")["owner"] is True
        assert _participation(sam_id, "owen-kili")["status"] == "active"


def test_concurrent_organization_joins_take_one_slot(app):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _org(app, admin_id, "ridge-club", "Owen")
        _org(app, admin_id, "other-club", "Owen")
        _route("alpha")
        _route("beta")
        _season(owner_id, "ridge-club", "alpha-season", "alpha")
        _season(owner_id, "other-club", "beta-season", "beta")
        barrier = threading.Barrier(2)
        outcome = {"ok": [], "codes": [], "errors": []}
        lock = threading.Lock()

        def run(slug: str) -> None:
            with app.app_context():
                try:
                    barrier.wait(timeout=10)
                    join_expedition(sam_id, slug)
                    with lock:
                        outcome["ok"].append(slug)
                except ExpeditionError as exc:
                    with lock:
                        outcome["codes"].append(exc.code)
                except Exception as exc:
                    with lock:
                        outcome["errors"].append(repr(exc))
                finally:
                    get_db().close()

        threads = [
            threading.Thread(target=run, args=("alpha-season",)),
            threading.Thread(target=run, args=("beta-season",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive()
        assert outcome["errors"] == []
        assert outcome["codes"] == ["slot_occupied"]
        assert len(outcome["ok"]) == 1
        assert _count(
            "expedition_participations",
            "user_id = ? AND status = 'active'",
            (sam_id,),
        ) == 1
        assert len([row for row in _journeys(sam_id) if row["status"] == "active"]) == 1


def _board_total(viewer_id: int, slug: str, user_id: int) -> Decimal:
    board = get_leaderboard(viewer_id, slug)
    return next(row["total_m"] for row in board["entries"] if row["user_id"] == user_id)


def test_ordinary_leave_and_rejoin_does_not_reset_the_board(app):
    with app.app_context():
        user_id = _user("Sam")
        _route("kili", "1000")
        create_private_expedition(user_id, slug="sam-kili", name="Sam Kili", route_slug="kili")
        _log(user_id, "100")
        leave_expedition(user_id, "sam-kili")
        assert _count("expedition_privacy_removals", "user_id = ?", (user_id,)) == 0
        rejoined = join_expedition(user_id, "sam-kili")
        assert rejoined["participation"]["display_credits_after"] is None
        assert _board_total(user_id, "sam-kili", user_id) == feet_to_meters(Decimal("100"))
        assert _count("expedition_invitations", "TRUE") == 0


def test_privacy_removal_rejoin_can_repeat_without_touching_invitations(app):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        rita_id = _user("Rita")
        _route("kili", "1000")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        link = create_share_link(owner_id, "owen-kili")
        named = create_username_invitation(owner_id, "owen-kili", "Rita")
        accept_invitation(sam_id, token=link["token"])
        invites_before = _count("expedition_invitations", "expedition_id = (SELECT id FROM expeditions WHERE slug = 'owen-kili')")
        personal = Decimal("0")
        for _cycle in range(2):
            _log(sam_id, "100")
            personal += feet_to_meters(Decimal("100"))
            assert _journey_total(_participation(sam_id, "owen-kili")["journey_id"]) == personal
            remove_participation_privacy(sam_id, "owen-kili", confirm=True)
            assert _participation(sam_id, "owen-kili") is None
            assert _count(
                "expedition_privacy_removals",
                "user_id = ? AND expedition_id = (SELECT id FROM expeditions WHERE slug = 'owen-kili')",
                (sam_id,),
            ) == 1
            rejoined = accept_invitation(sam_id, token=link["token"])
            assert rejoined["participation"]["display_credits_after"] is not None
            assert _count("expedition_privacy_removals", "user_id = ?", (sam_id,)) == 0
            assert _board_total(owner_id, "owen-kili", sam_id) == 0
            assert _journey_total(rejoined["journey"]["id"]) == personal
        cutoff = rejoined["participation"]["display_credits_after"]
        _log(sam_id, "100")
        leave_expedition(sam_id, "owen-kili")
        again = accept_invitation(sam_id, token=link["token"])
        assert again["participation"]["display_credits_after"] == cutoff
        assert _board_total(owner_id, "owen-kili", sam_id) == feet_to_meters(Decimal("100"))
        listed = list_expedition_invitations(owner_id, "owen-kili")
        assert {row["id"] for row in listed} == {link["id"], named["id"]}
        assert _count(
            "expedition_invitations",
            "expedition_id = (SELECT id FROM expeditions WHERE slug = 'owen-kili')",
        ) == invites_before
        accepted = accept_invitation(rita_id, invitation_id=named["id"])
        assert accepted["participation"]["display_credits_after"] is None
        assert get_db().execute(
            "SELECT status FROM expedition_invitations WHERE id = ?",
            (named["id"],),
        ).fetchone()["status"] == "accepted"


def test_a_failed_privacy_removal_leaves_no_marker(app, monkeypatch):
    with app.app_context():
        owner_id = _user("Owen")
        user_id = _user("Sam")
        _route("kili", "1000")
        create_private_expedition(owner_id, slug="owen-kili", name="Owen Kili", route_slug="kili")
        link = create_share_link(owner_id, "owen-kili")
        accept_invitation(user_id, token=link["token"])
        logged = _log(user_id, "100")
        invites_before = _count("expedition_invitations", "TRUE")
        original = DB.execute

        def wrapped(self, sql, params=()):
            outcome = original(self, sql, params)
            normalized = " ".join(sql.split()).lower()
            if "insert into expedition_privacy_removals" in normalized:
                raise RuntimeError("privacy removal stopped")
            return outcome

        monkeypatch.setattr(DB, "execute", wrapped)
        with pytest.raises(RuntimeError, match="privacy removal stopped"):
            remove_participation_privacy(user_id, "owen-kili", confirm=True)
        monkeypatch.undo()
        assert _participation(user_id, "owen-kili")["status"] == "active"
        assert _participation(user_id, "owen-kili")["display_credits_after"] is None
        assert _count("expedition_privacy_removals", "user_id = ?", (user_id,)) == 0
        assert _count("expedition_invitations", "TRUE") == invites_before
        assert get_db().execute(
            "SELECT status FROM expedition_invitations WHERE id = ?",
            (link["id"],),
        ).fetchone()["status"] == "pending"
        assert _journey_total(logged["journey"]["id"]) == feet_to_meters(Decimal("100"))
        assert _board_total(user_id, "owen-kili", user_id) == feet_to_meters(Decimal("100"))


def test_privacy_removal_migration_upgrades_and_downgrades():
    url = "postgresql://postgres:postgres@127.0.0.1:54329/adventurer_migration_test"
    root = os.path.dirname(os.path.dirname(__file__))

    def alembic(*args: str) -> None:
        env = os.environ.copy()
        env.pop("DATABASE_URL", None)
        env["TEST_DATABASE_URL"] = url
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"alembic {' '.join(args)} failed\n{result.stdout}\n{result.stderr}"
            )

    def relation() -> str | None:
        with psycopg.connect(url, connect_timeout=3) as conn:
            row = conn.execute(
                "SELECT to_regclass('adventurer.expedition_privacy_removals') AS name"
            ).fetchone()
            return row[0]

    try:
        alembic("upgrade", "head")
        with psycopg.connect(url, connect_timeout=3) as conn:
            columns = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'adventurer'
                      AND table_name = 'expedition_privacy_removals'
                    """
                ).fetchall()
            }
            constraints = {
                row[0]: row[1]
                for row in conn.execute(
                    """
                    SELECT conname, pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conrelid = 'adventurer.expedition_privacy_removals'::regclass
                    """
                ).fetchall()
            }
        assert columns == {"expedition_id", "user_id", "removed_at"}
        assert "PRIMARY KEY (expedition_id, user_id)" in constraints[
            "expedition_privacy_removals_pkey"
        ]
        assert "ON DELETE CASCADE" in constraints["expedition_privacy_removals_expedition_id_fkey"]
        assert "ON DELETE CASCADE" in constraints["expedition_privacy_removals_user_id_fkey"]
        alembic("downgrade", "0007_domain_foundation")
        assert relation() is None
    finally:
        alembic("upgrade", "head")
        assert relation() is not None


def test_archived_owner_release_keeps_no_privacy_row(app):
    with app.app_context():
        owner_id = _user("Fay")
        member_id = _user("Sam")
        route_id = _route("kili", "500")
        create_private_expedition(owner_id, slug="fay-kili", name="Fay Kili", route_slug="kili")
        logged = _log(owner_id, "100")
        assert _count("expedition_updates", "user_id = ?", (owner_id,)) == 1
        link = create_share_link(owner_id, "fay-kili")
        accept_invitation(member_id, token=link["token"])
        switch_journey(member_id, route_id)
        _log(member_id, "500")
        leave_expedition(member_id, "fay-kili")
        remove_participation_privacy(owner_id, "fay-kili", confirm=True)
        expedition = get_db().execute(
            "SELECT id, status, owner_user_id FROM expeditions WHERE slug = 'fay-kili'"
        ).fetchone()
        assert expedition["status"] == "archived"
        assert expedition["owner_user_id"] is None
        assert _participation(owner_id, "fay-kili") is None
        assert _count(
            "expedition_updates",
            "expedition_id = ? AND user_id = ?",
            (expedition["id"], owner_id),
        ) == 0
        assert _count(
            "expedition_result_entries",
            "expedition_id = ? AND user_id = ?",
            (expedition["id"], owner_id),
        ) == 0
        assert _count(
            "expedition_result_entries",
            "expedition_id = ? AND user_id = ?",
            (expedition["id"], member_id),
        ) == 1
        assert _count(
            "expedition_privacy_removals",
            "expedition_id = ? AND user_id = ?",
            (expedition["id"], owner_id),
        ) == 0
        assert [row["user_id"] for row in get_leaderboard(member_id, "fay-kili")["entries"]] == [
            member_id
        ]
        assert _journey_total(logged["journey"]["id"]) == feet_to_meters(Decimal("100"))


def test_creation_that_locks_the_organization_first_is_closed_by_suspension(app, monkeypatch):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        _org(app, admin_id, "ridge-club", "Owen")
        _route("kili")
        reached, allow = _pause_after_organization_lock(monkeypatch)
        holder = {"create": {}, "suspend": {}}

        def create() -> None:
            with app.app_context():
                try:
                    holder["create"]["result"] = _season(
                        owner_id, "ridge-club", "ridge-season", "kili"
                    )
                except Exception as exc:
                    holder["create"]["error"] = exc
                finally:
                    get_db().close()

        def suspend() -> None:
            with app.app_context():
                try:
                    holder["suspend"]["result"] = suspend_organization(admin_id, "ridge-club")
                except Exception as exc:
                    holder["suspend"]["error"] = exc
                finally:
                    get_db().close()

        creator = threading.Thread(target=create)
        suspender = threading.Thread(target=suspend)
        creator.start()
        suspender_started = False
        try:
            assert reached.wait(timeout=10)
            suspender.start()
            suspender_started = True
            _wait_for_organization_lock_wait()
        finally:
            allow.set()
        creator.join(timeout=20)
        if suspender_started:
            suspender.join(timeout=20)
        assert not creator.is_alive()
        assert not suspender.is_alive()
        assert "error" not in holder["create"]
        assert "error" not in holder["suspend"]
        assert holder["suspend"]["result"]["status"] == "suspended"
        assert get_db().execute(
            "SELECT status FROM organizations WHERE slug = 'ridge-club'"
        ).fetchone()["status"] == "suspended"
        expedition = get_db().execute(
            "SELECT status FROM expeditions WHERE slug = 'ridge-season'"
        ).fetchone()
        assert expedition["status"] == "completed"
        assert _count(
            "expeditions",
            "organization_id = (SELECT id FROM organizations WHERE slug = 'ridge-club') AND status = 'active'",
        ) == 0


def test_suspension_that_locks_the_organization_first_blocks_creation(app, monkeypatch):
    with app.app_context():
        admin_id = _user("Ada")
        owner_id = _user("Owen")
        _org(app, admin_id, "ridge-club", "Owen")
        _route("kili")
        reached, allow = _pause_after_organization_lock(monkeypatch)
        holder = {"create": {}, "suspend": {}}

        def create() -> None:
            with app.app_context():
                try:
                    holder["create"]["result"] = _season(
                        owner_id, "ridge-club", "ridge-season", "kili"
                    )
                except Exception as exc:
                    holder["create"]["error"] = exc
                finally:
                    get_db().close()

        def suspend() -> None:
            with app.app_context():
                try:
                    holder["suspend"]["result"] = suspend_organization(admin_id, "ridge-club")
                except Exception as exc:
                    holder["suspend"]["error"] = exc
                finally:
                    get_db().close()

        creator = threading.Thread(target=create)
        suspender = threading.Thread(target=suspend)
        suspender.start()
        creator_started = False
        try:
            assert reached.wait(timeout=10)
            creator.start()
            creator_started = True
            _wait_for_organization_lock_wait()
        finally:
            allow.set()
        suspender.join(timeout=20)
        if creator_started:
            creator.join(timeout=20)
        assert not suspender.is_alive()
        assert not creator.is_alive()
        assert "error" not in holder["suspend"]
        assert holder["suspend"]["result"]["status"] == "suspended"
        assert isinstance(holder["create"]["error"], ExpeditionError)
        assert holder["create"]["error"].code == "invalid_state"
        assert get_db().execute(
            "SELECT status FROM organizations WHERE slug = 'ridge-club'"
        ).fetchone()["status"] == "suspended"
        assert _count(
            "expeditions",
            "organization_id = (SELECT id FROM organizations WHERE slug = 'ridge-club')",
        ) == 0
        assert _count("expeditions", "status = 'active' AND kind = 'organization'") == 0
