"""Multi-metric expedition boards, snapshots, and activity events."""

from __future__ import annotations

from decimal import Decimal

from adventurer.activities import (
    create_activity,
    delete_activity,
    preview_activity,
    preview_activity_update,
    update_activity,
)
from adventurer.db import get_db
from adventurer.expeditions import (
    accept_invitation,
    archive_private_expedition,
    create_private_expedition,
    create_share_link,
    get_leaderboard,
    get_updates,
    leave_expedition,
    remove_participation_privacy,
)
from adventurer.journeys import restart_journey
from adventurer.metrics import parse_measurement
from adventurer.services import create_adventurer

DAY = "2026-01-02"


def _user(name: str) -> int:
    return create_adventurer(name, "trailhead")["id"]


def _feet(amount: str) -> Decimal:
    return parse_measurement("elevation", amount)


def _miles(amount: str) -> Decimal:
    return parse_measurement("distance", amount)


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


def _save(user_id: int, **fields) -> dict:
    body = {"happened_on": DAY, **fields}
    preview = preview_activity(user_id, body)
    token = preview["confirmation_token"] if preview["will_complete"] else None
    return create_activity(user_id, body, confirmation_token=token)


def _amount(entry: dict, metric: str) -> Decimal:
    row = next(item for item in entry["metrics"] if item["metric"] == metric)
    if "total_m" in row:
        return row["total_m"]
    return row["credited_m"]


def _entry(board: dict, user_id: int) -> dict:
    return next(row for row in board["entries"] if row["user_id"] == user_id)


def _share(owner_id: int, slug: str) -> str:
    return create_share_link(owner_id, slug)["token"]


def _join(user_id: int, token: str) -> None:
    accept_invitation(user_id, token=token)


def _stored(journey_id: int) -> dict[str, Decimal]:
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


def _credit_rows(journey_id: int) -> int:
    return get_db().execute(
        "SELECT COUNT(*) AS n FROM journey_credits WHERE journey_id = ?",
        (journey_id,),
    ).fetchone()["n"]


def test_elevation_board_orders_finishers_then_credit(app):
    with app.app_context():
        owner_id = _user("Cy")
        early_id = _user("Zed")
        later_id = _user("Ann")
        partial_id = _user("Bea")
        route_id = _route(
            "elev",
            {"elevation": _feet("500")},
            [("summit", 0, {"elevation": _feet("500")})],
        )
        create_private_expedition(owner_id, slug="elev-party", name="Elev", route_slug="elev")
        token = _share(owner_id, "elev-party")
        for user_id in (early_id, later_id, partial_id):
            _join(user_id, token)
        _save(early_id, elevation_ft="500")
        _save(later_id, elevation_ft="500")
        _save(partial_id, elevation_ft="100")
        live = get_leaderboard(owner_id, "elev-party")
        assert [row["user_id"] for row in live["entries"]] == [
            early_id,
            later_id,
            partial_id,
            owner_id,
        ]
        assert live["entries"][0]["rank_basis"] == "actual_completion"
        assert live["entries"][0]["live_rank"] == 1
        assert live["entries"][0]["projected_completion_at"] is None
        assert live["entries"][2]["rank_basis"] == "credited_amount"
        assert _amount(live["entries"][2], "elevation") == _feet("100")
        assert live["entries"][3]["live_rank"] == 4
        assert _amount(live["entries"][3], "elevation") == 0
        assert live["entries"][3]["checkpoint_name"] == "Start"
        same = _route(
            "tied",
            {"elevation": _feet("500")},
            [("summit", 0, {"elevation": _feet("500")})],
        )
        create_private_expedition(owner_id, slug="tied-party", name="Tied", route_slug="tied")
        first_id = _user("Moe")
        second_id = _user("Amy")
        token = _share(owner_id, "tied-party")
        _join(first_id, token)
        _join(second_id, token)
        _save(first_id, elevation_ft="80")
        _save(second_id, elevation_ft="80")
        tied = get_leaderboard(owner_id, "tied-party")
        assert [row["user_id"] for row in tied["entries"] if row["user_id"] != owner_id] == [
            first_id,
            second_id,
        ]


def test_distance_board_uses_only_distance(app):
    with app.app_context():
        owner_id = _user("Cy")
        far_id = _user("Bea")
        near_id = _user("Ann")
        _route(
            "walk",
            {"distance": _miles("10")},
            [("finish", 0, {"distance": _miles("10")})],
        )
        create_private_expedition(owner_id, slug="walk-party", name="Walk", route_slug="walk")
        token = _share(owner_id, "walk-party")
        _join(far_id, token)
        _join(near_id, token)
        _save(far_id, distance_mi="3")
        _save(near_id, distance_mi="1")
        live = get_leaderboard(owner_id, "walk-party")
        assert [row["user_id"] for row in live["entries"]] == [far_id, near_id, owner_id]
        assert [item["metric"] for item in live["entries"][0]["metrics"]] == ["distance"]
        assert _amount(live["entries"][0], "distance") == _miles("3")
        assert live["entries"][0]["projected_completion_at"] is None
        assert live["entries"][2]["live_rank"] == 3
        assert _amount(live["entries"][2], "distance") == 0


def test_combined_board_keeps_unfinished_people_unranked(app):
    with app.app_context():
        owner_id = _user("Lee")
        finisher_id = _user("Ada")
        ahead_id = _user("Sam")
        _route(
            "both",
            {"elevation": _feet("500"), "distance": _miles("2")},
            [
                ("camp", 0, {"elevation": _feet("100"), "distance": _miles("1")}),
                ("summit", 1, {"elevation": _feet("500"), "distance": _miles("2")}),
            ],
        )
        create_private_expedition(owner_id, slug="both-party", name="Both", route_slug="both")
        token = _share(owner_id, "both-party")
        _join(finisher_id, token)
        _join(ahead_id, token)
        _save(finisher_id, elevation_ft="500", distance_mi="2")
        _save(ahead_id, elevation_ft="400", distance_mi="0.2")
        live = get_leaderboard(owner_id, "both-party")
        assert [row["user_id"] for row in live["entries"]] == [finisher_id, owner_id, ahead_id]
        finished = _entry(live, finisher_id)
        assert finished["rank_basis"] == "actual_completion"
        assert finished["live_rank"] == 1
        assert finished["projected_completion_at"] is None
        assert _amount(finished, "elevation") == _feet("500")
        assert _amount(finished, "distance") == _miles("2")
        for user_id in (owner_id, ahead_id):
            row = _entry(live, user_id)
            assert row["rank_basis"] == "insufficient_data"
            assert row["live_rank"] is None
            assert row["projected_completion_at"] is None
            assert row["completed_at"] is None
        assert _amount(_entry(live, ahead_id), "elevation") == _feet("400")
        assert _amount(_entry(live, ahead_id), "distance") == _miles("0.2")
        assert _amount(_entry(live, owner_id), "elevation") == 0
        assert _amount(_entry(live, owner_id), "distance") == 0


def test_display_cutoff_applies_to_each_metric(app):
    with app.app_context():
        owner_id = _user("Owen")
        sam_id = _user("Sam")
        _route(
            "split",
            {"elevation": _feet("1000"), "distance": _miles("5")},
            [("summit", 0, {"elevation": _feet("1000"), "distance": _miles("5")})],
        )
        create_private_expedition(owner_id, slug="split-party", name="Split", route_slug="split")
        link = create_share_link(owner_id, "split-party")
        joined = accept_invitation(sam_id, token=link["token"])
        _save(sam_id, elevation_ft="100", distance_mi="0.5")
        remove_participation_privacy(sam_id, "split-party", confirm=True)
        rejoined = accept_invitation(sam_id, token=link["token"])
        assert rejoined["participation"]["display_credits_after"] is not None
        hidden = _entry(get_leaderboard(owner_id, "split-party"), sam_id)
        assert _amount(hidden, "elevation") == 0
        assert _amount(hidden, "distance") == 0
        assert _stored(joined["journey"]["id"]) == {
            "elevation": _feet("100"),
            "distance": _miles("0.5"),
        }
        _save(sam_id, elevation_ft="80")
        shown = _entry(get_leaderboard(owner_id, "split-party"), sam_id)
        assert _amount(shown, "elevation") == _feet("80")
        assert _amount(shown, "distance") == 0
        elevation = next(item for item in shown["metrics"] if item["metric"] == "elevation")
        assert elevation["remaining_m"] == _feet("1000") - _feet("80")
        assert _stored(rejoined["journey"]["id"])["distance"] == _miles("0.5")
        assert _stored(rejoined["journey"]["id"])["elevation"] == _feet("180")


def test_activity_events_follow_the_credited_metrics(app):
    with app.app_context():
        owner_id = _user("Owen")
        route_id = _route(
            "feed",
            {"elevation": _feet("1000"), "distance": _miles("5")},
            [
                ("camp", 0, {"elevation": _feet("100")}),
                ("hut", 1, {"elevation": _feet("400"), "distance": _miles("1")}),
                ("summit", 2, {"elevation": _feet("1000"), "distance": _miles("5")}),
            ],
        )
        create_private_expedition(owner_id, slug="feed-party", name="Feed", route_slug="feed")
        both = _save(owner_id, elevation_ft="450", distance_mi="1.2")
        updates = get_updates(owner_id, "feed-party")
        logged = [row for row in updates if row["kind"] == "activity_logged"]
        checkpoints = [row for row in updates if row["kind"] == "checkpoint_reached"]
        assert len(logged) == 1
        assert {item["metric"] for item in logged[0]["metrics"]} == {"elevation", "distance"}
        assert _amount(logged[0], "elevation") == _feet("450")
        assert _amount(logged[0], "distance") == _miles("1.2")
        assert [row["checkpoint_name"] for row in checkpoints] == ["camp", "hut"]
        assert "summit_reached" not in {row["kind"] for row in updates}
        elevation_only = _save(owner_id, elevation_ft="10")
        only = [
            row
            for row in get_updates(owner_id, "feed-party")
            if row["kind"] == "activity_logged" and row["activity_id"] == elevation_only["activity"]["id"]
        ]
        assert [item["metric"] for item in only[0]["metrics"]] == ["elevation"]
        before = [row["kind"] for row in get_updates(owner_id, "feed-party")]
        update_activity(owner_id, elevation_only["activity"]["id"], {"note": "clearer"})
        delete_activity(owner_id, both["activity"]["id"])
        assert [row["kind"] for row in get_updates(owner_id, "feed-party")] == before


def test_summit_is_recorded_once_when_an_edit_finishes_the_journey(app):
    with app.app_context():
        owner_id = _user("Owen")
        _route(
            "top",
            {"elevation": _feet("500"), "distance": _miles("2")},
            [("summit", 0, {"elevation": _feet("500"), "distance": _miles("2")})],
        )
        create_private_expedition(owner_id, slug="top-party", name="Top", route_slug="top")
        started = _save(owner_id, elevation_ft="100", distance_mi="0.5")
        assert "summit_reached" not in {row["kind"] for row in get_updates(owner_id, "top-party")}
        preview = preview_activity_update(
            owner_id,
            started["activity"]["id"],
            {"elevation_ft": "500", "distance_mi": "2"},
        )
        assert preview["will_complete"] is True
        update_activity(
            owner_id,
            started["activity"]["id"],
            {"elevation_ft": "500", "distance_mi": "2"},
            confirmation_token=preview["confirmation_token"],
        )
        kinds = [row["kind"] for row in get_updates(owner_id, "top-party")]
        assert kinds.count("summit_reached") == 1
        assert kinds.count("activity_logged") == 1
        assert "checkpoint_reached" not in kinds
        summit = next(row for row in get_updates(owner_id, "top-party") if row["kind"] == "summit_reached")
        assert _amount(summit, "elevation") == _feet("500")
        assert _amount(summit, "distance") == _miles("2")


def test_final_snapshot_keeps_each_metric_and_leaves_nonfinishers_unranked(app):
    with app.app_context():
        owner_id = _user("Owen")
        finisher_id = _user("Fay")
        partial_id = _user("Ned")
        leaver_id = _user("Lee")
        route_id = _route(
            "final",
            {"elevation": _feet("500"), "distance": _miles("2")},
            [("summit", 0, {"elevation": _feet("500"), "distance": _miles("2")})],
        )
        create_private_expedition(owner_id, slug="final-party", name="Final", route_slug="final")
        token = _share(owner_id, "final-party")
        for user_id in (finisher_id, partial_id, leaver_id):
            _join(user_id, token)
        _save(finisher_id, elevation_ft="500", distance_mi="2")
        leave_expedition(finisher_id, "final-party")
        _save(partial_id, elevation_ft="120", distance_mi="0.4")
        _save(leaver_id, elevation_ft="40", distance_mi="0.1")
        leave_expedition(leaver_id, "final-party")
        closed = archive_private_expedition(owner_id, "final-party")
        by_user = {row["user_id"]: row for row in closed["entries"]}
        assert leaver_id not in by_user
        assert by_user[finisher_id]["final_rank"] == 1
        assert _amount(by_user[finisher_id], "elevation") == _feet("500")
        assert _amount(by_user[finisher_id], "distance") == _miles("2")
        assert "projected_completion_at" not in by_user[finisher_id]
        for user_id in (owner_id, partial_id):
            assert by_user[user_id]["final_rank"] is None
        assert _amount(by_user[partial_id], "elevation") == _feet("120")
        assert _amount(by_user[partial_id], "distance") == _miles("0.4")
        assert _amount(by_user[owner_id], "elevation") == 0
        assert _amount(by_user[owner_id], "distance") == 0
        stored = {
            row["user_id"]: (row["final_rank"], row["journey_id"], row["metrics"])
            for row in closed["entries"]
        }
        restart_journey(finisher_id, route_id)
        _save(partial_id, elevation_ft="50")
        again = get_leaderboard(owner_id, "final-party")
        assert again["mode"] == "final"
        reread = {
            row["user_id"]: (row["final_rank"], row["journey_id"], row["metrics"])
            for row in again["entries"]
        }
        assert reread == stored
        remove_participation_privacy(partial_id, "final-party", confirm=True)
        remaining = {
            row["user_id"]: (row["final_rank"], row["metrics"])
            for row in get_leaderboard(owner_id, "final-party")["entries"]
        }
        assert set(remaining) == {owner_id, finisher_id}
        assert remaining[finisher_id][0] == 1
        assert remaining[finisher_id][1] == stored[finisher_id][2]
        assert remaining[owner_id][1] == stored[owner_id][2]
        assert get_db().execute(
            "SELECT COUNT(*) AS n FROM activities WHERE user_id = ?",
            (partial_id,),
        ).fetchone()["n"] == 2


def test_two_active_expeditions_share_one_journey(app):
    with app.app_context():
        owner_id = _user("Owen")
        _route(
            "shared",
            {"elevation": _feet("1000"), "distance": _miles("4")},
            [("summit", 0, {"elevation": _feet("1000"), "distance": _miles("4")})],
        )
        first = create_private_expedition(
            owner_id, slug="shared-one", name="One", route_slug="shared"
        )
        second = create_private_expedition(
            owner_id, slug="shared-two", name="Two", route_slug="shared"
        )
        assert first["journey"]["id"] == second["journey"]["id"]
        _save(owner_id, elevation_ft="100", distance_mi="0.25")
        for slug in ("shared-one", "shared-two"):
            row = _entry(get_leaderboard(owner_id, slug), owner_id)
            assert _amount(row, "elevation") == _feet("100")
            assert _amount(row, "distance") == _miles("0.25")
            logged = [item for item in get_updates(owner_id, slug) if item["kind"] == "activity_logged"]
            assert len(logged) == 1
        assert _credit_rows(first["journey"]["id"]) == 2
        assert get_db().execute(
            "SELECT COUNT(*) AS n FROM activity_journey_assignments WHERE journey_id = ?",
            (first["journey"]["id"],),
        ).fetchone()["n"] == 1


def test_close_after_an_activity_snapshots_the_committed_metrics(app):
    with app.app_context():
        owner_id = _user("Owen")
        _route(
            "race",
            {"distance": _miles("8")},
            [("finish", 0, {"distance": _miles("8")})],
        )
        create_private_expedition(owner_id, slug="race-party", name="Race", route_slug="race")
        _save(owner_id, distance_mi="1.5")
        closed = archive_private_expedition(owner_id, "race-party")
        assert _amount(closed["entries"][0], "distance") == _miles("1.5")
        assert closed["entries"][0]["final_rank"] == 1
