"""Activity arrivals, distance-only writes, and home pending arrivals."""

from __future__ import annotations

import threading
from decimal import Decimal

from adventurer.activities import create_activity
from adventurer.db import DB, get_db
from adventurer.measures import feet_to_meters
from adventurer.metrics import MILE_IN_METERS

DAY = "2026-06-01"
REVEAL = " reveal."
JOURNAL = "The longer entry."


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _register(client, name):
    response = client.post(
        "/api/v1/auth/register",
        json={"name": name, "password": "trailhead"},
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    client.delete_cookie("session")
    return body["you"]["id"], body["token"]


def _check_shape(database) -> None:
    database.execute(
        """
        SET CONSTRAINTS
            adventurer.route_progress_shape_routes,
            adventurer.route_progress_shape_requirements,
            adventurer.route_progress_shape_checkpoints,
            adventurer.route_progress_shape_checkpoint_requirements,
            adventurer.route_chapter_spans,
            adventurer.route_chapter_spans_chapters
        IMMEDIATE
        """
    )
    database.execute(
        """
        SET CONSTRAINTS
            adventurer.route_progress_shape_routes,
            adventurer.route_progress_shape_requirements,
            adventurer.route_progress_shape_checkpoints,
            adventurer.route_progress_shape_checkpoint_requirements,
            adventurer.route_chapter_spans,
            adventurer.route_chapter_spans_chapters
        DEFERRED
        """
    )


def _route(slug: str, requirements: list[tuple[str, Decimal]]) -> int:
    database = get_db()
    route_id = database.execute(
        """
        INSERT INTO routes (slug, name, region, summary, status)
        VALUES (?, ?, 'Test', 'A test route', 'draft')
        RETURNING id
        """,
        (slug, slug),
    ).fetchone()["id"]
    for metric, amount in requirements:
        database.execute(
            """
            INSERT INTO route_requirements (route_id, metric_code, target_amount)
            VALUES (?, ?, ?)
            """,
            (route_id, metric, amount),
        )
    return route_id


def _stop(route_id: int, slug: str, position: int, thresholds: list[tuple[str, Decimal]], *, name: str) -> int:
    database = get_db()
    checkpoint_id = database.execute(
        """
        INSERT INTO route_checkpoints (route_id, slug, name, position)
        VALUES (?, ?, ?, ?)
        RETURNING id
        """,
        (route_id, slug, name, position),
    ).fetchone()["id"]
    for metric, amount in thresholds:
        database.execute(
            """
            INSERT INTO checkpoint_requirements (
                checkpoint_id, route_id, metric_code, threshold_amount
            )
            VALUES (?, ?, ?, ?)
            """,
            (checkpoint_id, route_id, metric, amount),
        )
    return checkpoint_id


def _chapter(route_id: int, slug: str, position: int, status: str = "published") -> int:
    return get_db().execute(
        """
        INSERT INTO route_chapters (route_id, slug, title, position, status)
        VALUES (?, ?, ?, ?, ?)
        RETURNING id
        """,
        (route_id, slug, slug.replace("-", " ").title(), position, status),
    ).fetchone()["id"]


def _assign(checkpoint_id: int, chapter_id: int) -> None:
    get_db().execute(
        "UPDATE route_checkpoints SET chapter_id = ? WHERE id = ?",
        (chapter_id, checkpoint_id),
    )


def _story(checkpoint_id: int, route_id: int, title: str, *, status: str = "published", journal: bool = False) -> None:
    get_db().execute(
        """
        INSERT INTO checkpoint_stories (
            checkpoint_id, route_id, arrival_title, arrival_body,
            journal_title, journal_body, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            checkpoint_id,
            route_id,
            title,
            f"{title}{REVEAL}",
            "Below the ridge" if journal else None,
            JOURNAL if journal else None,
            status,
        ),
    )


def _publish(route_id: int) -> None:
    database = get_db()
    database.execute("UPDATE routes SET status = 'published' WHERE id = ?", (route_id,))
    _check_shape(database)
    database.commit()


def _post(client, token, **fields):
    body = {"happened_on": DAY, **fields}
    return client.post("/api/v1/activities", json=body, headers=_auth(token))


def _save(client, token, **fields) -> dict:
    response = _post(client, token, **fields)
    if response.status_code == 409 and response.get_json().get("code") == "confirmation_required":
        body = {"happened_on": DAY, **fields}
        body["confirmation_token"] = response.get_json()["details"]["preview"]["confirmation_token"]
        response = client.post("/api/v1/activities", json=body, headers=_auth(token))
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _start(client, token, slug: str) -> None:
    response = client.post(
        "/api/v1/journeys/start",
        json={"route_slug": slug},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.get_json()


def _abandon(client, token, slug: str) -> None:
    response = client.post(
        "/api/v1/journeys/abandon",
        json={"route_slug": slug},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.get_json()


def _party(client, token, slug: str, route_slug: str) -> None:
    response = client.post(
        "/api/v1/expeditions",
        json={"slug": slug, "name": slug.replace("-", " ").title(), "route_slug": route_slug},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.get_json()


def _home(client, token) -> dict:
    response = client.get("/api/v1/home", headers=_auth(token))
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _journal_slugs(client, token, journey_id: int) -> list[str]:
    response = client.get(f"/api/v1/journeys/{journey_id}/journal", headers=_auth(token))
    assert response.status_code == 200, response.get_json()
    return [
        entry["checkpoint_slug"]
        for chapter in response.get_json()["chapters"]
        for entry in chapter["entries"]
    ]


def _ack(client, token, journey_id: int, slug: str) -> dict:
    response = client.post(
        f"/api/v1/journeys/{journey_id}/journal/{slug}/acknowledgment",
        headers=_auth(token),
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _count(table: str, where: str = "TRUE", params: tuple = ()) -> int:
    return get_db().execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE {where}",
        params,
    ).fetchone()["n"]


def _feed(user_id: int) -> list[tuple]:
    return [
        (row["kind"], row["checkpoint_name"])
        for row in get_db().execute(
            """
            SELECT kind, checkpoint_name
            FROM expedition_updates
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()
    ]


def _arrival(slug: str, title: str, chapter: str | None, *, acknowledged: bool = False) -> dict:
    return {
        "checkpoint_slug": slug,
        "chapter_slug": chapter,
        "arrival_title": title,
        "arrival_body": f"{title}{REVEAL}",
        "acknowledged": acknowledged,
    }


def _pending(slug: str, title: str, chapter: str | None) -> dict:
    return {
        "checkpoint_slug": slug,
        "chapter_slug": chapter,
        "arrival_title": title,
    }


def test_each_metric_can_be_logged_and_a_distance_route_can_be_finished(client, app):
    mile = MILE_IN_METERS
    with app.app_context():
        elevation_id = _route("arrival-elev", [("elevation", feet_to_meters(Decimal("200")))])
        _stop(elevation_id, "top", 0, [("elevation", feet_to_meters(Decimal("200")))], name="Top")
        _publish(elevation_id)
        combined_id = _route(
            "arrival-combined",
            [("distance", mile * 2), ("elevation", feet_to_meters(Decimal("500")))],
        )
        gate = _stop(
            combined_id,
            "gate",
            0,
            [("distance", mile), ("elevation", feet_to_meters(Decimal("100")))],
            name="Gate",
        )
        _stop(
            combined_id,
            "finish",
            1,
            [("distance", mile * 2), ("elevation", feet_to_meters(Decimal("500")))],
            name="Finish",
        )
        chapter_id = _chapter(combined_id, "approach", 0)
        _assign(gate, chapter_id)
        _story(gate, combined_id, "Both")
        _publish(combined_id)
        distance_id = _route("arrival-distance", [("distance", mile * 2)])
        milepost = _stop(distance_id, "mile", 0, [("distance", mile)], name="Mile")
        finish = _stop(distance_id, "finish", 1, [("distance", mile * 2)], name="Finish")
        road = _chapter(distance_id, "road", 0)
        _assign(milepost, road)
        _assign(finish, road)
        _story(milepost, distance_id, "Mile")
        _story(finish, distance_id, "Finish", journal=True)
        _publish(distance_id)

    user_id, token = _register(client, "MetricWalker")
    _start(client, token, "arrival-elev")
    elevation_only = _save(client, token, elevation_ft="10")
    assert elevation_only["activity"]["distance_m"] is None
    assert Decimal(elevation_only["activity"]["elevation_gain_m"]) == feet_to_meters(Decimal("10"))
    assert elevation_only["arrivals"] == []
    assert elevation_only["journey"]["id"]
    measured_zero = _save(client, token, elevation_ft="0")
    assert Decimal(measured_zero["activity"]["elevation_gain_m"]) == 0
    assert measured_zero["activity"]["distance_m"] is None
    assert measured_zero["credits"] == []
    assert measured_zero["arrivals"] == []
    assert measured_zero["journey"] is None
    missing = _post(client, token)
    assert missing.status_code == 400
    assert missing.get_json()["code"] == "invalid"
    assert missing.get_json()["error"] == "Add elevation or distance."
    negative = _post(client, token, distance_mi="-1")
    assert negative.status_code == 400
    assert negative.get_json()["code"] == "invalid"
    _abandon(client, token, "arrival-elev")

    _start(client, token, "arrival-combined")
    elevation_first = _save(client, token, elevation_ft="100")
    assert elevation_first["activity"]["distance_m"] is None
    assert elevation_first["arrivals"] == []
    assert _journal_slugs(client, token, elevation_first["journey"]["id"]) == []
    distance_second = _save(client, token, distance_mi="1")
    assert distance_second["activity"]["elevation_gain_m"] is None
    assert Decimal(distance_second["activity"]["distance_m"]) == mile
    assert distance_second["arrivals"] == [_arrival("gate", "Both", "approach")]
    both = _save(client, token, elevation_ft="10", distance_mi="0.1")
    assert Decimal(both["activity"]["elevation_gain_m"]) == feet_to_meters(Decimal("10"))
    assert Decimal(both["activity"]["distance_m"]) == mile * Decimal("0.1")
    assert both["arrivals"] == []
    noted = client.patch(
        f"/api/v1/activities/{distance_second['activity']['id']}",
        json={"note": "kept"},
        headers=_auth(token),
    )
    assert noted.status_code == 200, noted.get_json()
    assert noted.get_json()["activity"]["elevation_gain_m"] is None
    assert Decimal(noted.get_json()["activity"]["distance_m"]) == mile
    assert noted.get_json()["arrivals"] == []
    shifted = client.patch(
        f"/api/v1/activities/{both['activity']['id']}",
        json={"elevation_ft": "20"},
        headers=_auth(token),
    )
    assert shifted.status_code == 200, shifted.get_json()
    assert Decimal(shifted.get_json()["activity"]["elevation_gain_m"]) == feet_to_meters(Decimal("20"))
    assert Decimal(shifted.get_json()["activity"]["distance_m"]) == mile * Decimal("0.1")
    assert shifted.get_json()["arrivals"] == []
    _abandon(client, token, "arrival-combined")

    _start(client, token, "arrival-distance")
    first_mile = _save(client, token, distance_mi="1")
    journey_id = first_mile["journey"]["id"]
    assert first_mile["activity"]["elevation_gain_m"] is None
    assert first_mile["arrivals"] == [_arrival("mile", "Mile", "road")]
    with app.app_context():
        views_before = _count("journey_story_views", "journey_id = ?", (journey_id,))
    preview = client.post(
        "/api/v1/activities/preview",
        json={"distance_mi": "1", "happened_on": DAY},
        headers=_auth(token),
    )
    assert preview.status_code == 200, preview.get_json()
    assert "arrivals" not in preview.get_json()
    assert preview.get_json()["preview"]["will_complete"] is True
    assert preview.get_json()["preview"]["elevation_m"] is None
    rejected = _post(client, token, distance_mi="1")
    assert rejected.status_code == 409
    assert rejected.get_json()["code"] == "confirmation_required"
    assert "arrivals" not in rejected.get_json()
    with app.app_context():
        assert _count("activities", "user_id = ?", (user_id,)) == 6
        assert _count("journey_story_views", "journey_id = ?", (journey_id,)) == views_before
    finished = client.post(
        "/api/v1/activities",
        json={
            "distance_mi": "1",
            "happened_on": DAY,
            "confirmation_token": preview.get_json()["preview"]["confirmation_token"],
        },
        headers=_auth(token),
    )
    assert finished.status_code == 201, finished.get_json()
    body = finished.get_json()
    assert body["activity"]["elevation_gain_m"] is None
    assert body["journey"]["id"] == journey_id
    assert body["journey"]["status"] == "completed"
    assert body["arrivals"] == [_arrival("finish", "Finish", "road")]
    assert JOURNAL not in body["arrivals"][0].values()
    assert _journal_slugs(client, token, journey_id) == ["mile", "finish"]
    version = client.get(f"/api/v1/journeys/{journey_id}", headers=_auth(token)).get_json()["journey"]
    home = _home(client, token)
    assert home["journey"] is None
    assert home["focus"]["status"] == "completed"
    assert home["focus"]["journey_id"] == journey_id
    assert "pending_arrivals" not in home["focus"]
    acknowledged = _ack(client, token, journey_id, "finish")
    assert acknowledged["journey_id"] == journey_id
    after = client.get(f"/api/v1/journeys/{journey_id}", headers=_auth(token)).get_json()["journey"]
    assert after["status"] == "completed"
    assert after["progress_version"] == version["progress_version"]


def test_one_activity_returns_only_the_published_arrivals_it_newly_reached(client, app):
    feet = feet_to_meters
    with app.app_context():
        route_id = _route("arrival-cross", [("elevation", feet(Decimal("1000")))])
        almost = _stop(route_id, "almost", 0, [("elevation", feet(Decimal("100.01")))], name="Almost")
        gate = _stop(route_id, "gate", 1, [("elevation", feet(Decimal("200")))], name="Gate")
        quiet = _stop(route_id, "quiet", 2, [("elevation", feet(Decimal("250")))], name="Quiet")
        old = _stop(route_id, "old", 3, [("elevation", feet(Decimal("300")))], name="Old")
        camp = _stop(route_id, "camp", 4, [("elevation", feet(Decimal("400")))], name="Camp")
        _stop(route_id, "summit", 5, [("elevation", feet(Decimal("1000")))], name="Summit")
        approach = _chapter(route_id, "approach", 0)
        hidden = _chapter(route_id, "hidden", 1, status="draft")
        _assign(almost, approach)
        _assign(gate, approach)
        _assign(quiet, approach)
        _assign(old, approach)
        _assign(camp, hidden)
        _story(almost, route_id, "Almost")
        _story(gate, route_id, "Gate", journal=True)
        _story(quiet, route_id, "Quiet", status="draft")
        _story(old, route_id, "Old", status="retired")
        _story(camp, route_id, "Camp")
        _publish(route_id)

    user_id, token = _register(client, "ArrivalReader")
    _party(client, token, "arrival-cross-party", "arrival-cross")
    journey_id = _home(client, token)["journey"]["id"]
    short = _save(client, token, elevation_ft="100")
    assert short["arrivals"] == []
    assert short["journey"]["id"] == journey_id
    crossed = _save(client, token, elevation_ft="300")
    assert crossed["journey"]["id"] == journey_id
    assert crossed["arrivals"] == [
        _arrival("almost", "Almost", "approach"),
        _arrival("gate", "Gate", "approach"),
        _arrival("camp", "Camp", None),
    ]
    assert JOURNAL not in crossed["arrivals"][1].values()
    assert _journal_slugs(client, token, journey_id) == ["almost", "gate", "camp"]
    with app.app_context():
        assert _count("journey_story_views", "journey_id = ?", (journey_id,)) == 0
        assert _feed(user_id) == [
            ("activity_logged", None),
            ("activity_logged", None),
            ("checkpoint_reached", "Almost"),
            ("checkpoint_reached", "Gate"),
            ("checkpoint_reached", "Quiet"),
            ("checkpoint_reached", "Old"),
            ("checkpoint_reached", "Camp"),
        ]
        feed_before = len(_feed(user_id))
    removed = client.delete(
        f"/api/v1/activities/{crossed['activity']['id']}",
        headers=_auth(token),
    )
    assert removed.status_code == 200, removed.get_json()
    assert removed.get_json()["arrivals"] == []
    assert removed.get_json()["activity_id"] == crossed["activity"]["id"]
    assert removed.get_json()["journey"]["id"] == journey_id
    assert _journal_slugs(client, token, journey_id) == []
    with app.app_context():
        assert _count("journey_story_views", "journey_id = ?", (journey_id,)) == 0
        assert len(_feed(user_id)) == feed_before


def test_an_edit_uses_the_credited_journey_and_leaves_the_feed_rules_alone(client, app):
    feet = feet_to_meters
    with app.app_context():
        route_id = _route("arrival-edit", [("elevation", feet(Decimal("1000")))])
        gate = _stop(route_id, "gate", 0, [("elevation", feet(Decimal("100")))], name="Gate")
        ridge = _stop(route_id, "ridge", 1, [("elevation", feet(Decimal("200")))], name="Ridge")
        camp = _stop(route_id, "camp", 2, [("elevation", feet(Decimal("400")))], name="Camp")
        _stop(route_id, "summit", 3, [("elevation", feet(Decimal("1000")))], name="Summit")
        chapter_id = _chapter(route_id, "approach", 0)
        _assign(gate, chapter_id)
        _assign(ridge, chapter_id)
        _assign(camp, chapter_id)
        _story(gate, route_id, "Gate")
        _story(ridge, route_id, "Ridge")
        _story(camp, route_id, "Camp")
        _publish(route_id)
        side_id = _route("arrival-side", [("elevation", feet(Decimal("1000")))])
        _stop(side_id, "end", 0, [("elevation", feet(Decimal("1000")))], name="End")
        _publish(side_id)

    user_id, token = _register(client, "EditReader")
    _party(client, token, "arrival-edit-party", "arrival-edit")
    journey_id = _home(client, token)["journey"]["id"]
    opened = _save(client, token, elevation_ft="40")
    assert opened["arrivals"] == []
    with app.app_context():
        feed_after_create = _feed(user_id)
    edited = client.patch(
        f"/api/v1/activities/{opened['activity']['id']}",
        json={"elevation_ft": "100"},
        headers=_auth(token),
    )
    assert edited.status_code == 200, edited.get_json()
    assert edited.get_json()["journey"]["id"] == journey_id
    assert edited.get_json()["journey"]["status"] == "active"
    assert edited.get_json()["arrivals"] == [_arrival("gate", "Gate", "approach")]
    again = client.patch(
        f"/api/v1/activities/{opened['activity']['id']}",
        json={"elevation_ft": "150"},
        headers=_auth(token),
    )
    assert again.status_code == 200, again.get_json()
    assert again.get_json()["arrivals"] == []
    with app.app_context():
        assert _feed(user_id) == feed_after_create
        assert feed_after_create == [("activity_logged", None)]
    second = _save(client, token, elevation_ft="50")
    assert second["arrivals"] == [_arrival("ridge", "Ridge", "approach")]
    with app.app_context():
        assert _feed(user_id) == [
            ("activity_logged", None),
            ("activity_logged", None),
            ("checkpoint_reached", "Ridge"),
        ]
        updates_before_pause = len(_feed(user_id))
    switched = client.post(
        "/api/v1/journeys/switch",
        json={"route_slug": "arrival-side"},
        headers=_auth(token),
    )
    assert switched.status_code == 200, switched.get_json()
    paused_edit = client.patch(
        f"/api/v1/activities/{second['activity']['id']}",
        json={"elevation_ft": "250"},
        headers=_auth(token),
    )
    assert paused_edit.status_code == 200, paused_edit.get_json()
    assert paused_edit.get_json()["journey"]["id"] == journey_id
    assert paused_edit.get_json()["journey"]["status"] == "paused"
    assert paused_edit.get_json()["arrivals"] == [_arrival("camp", "Camp", "approach")]
    home = _home(client, token)
    assert home["journey"]["route"]["slug"] == "arrival-side"
    assert home["journey"]["pending_arrivals"] == []
    assert "pending_arrivals" not in home["paused_journeys"][0]
    with app.app_context():
        assert len(_feed(user_id)) == updates_before_pause
    _abandon(client, token, "arrival-edit")
    abandoned_edit = client.patch(
        f"/api/v1/activities/{second['activity']['id']}",
        json={"elevation_ft": "260"},
        headers=_auth(token),
    )
    assert abandoned_edit.status_code == 200, abandoned_edit.get_json()
    assert abandoned_edit.get_json()["journey"]["id"] == journey_id
    assert abandoned_edit.get_json()["journey"]["status"] == "abandoned"
    assert abandoned_edit.get_json()["arrivals"] == []
    before = client.get(f"/api/v1/journeys/{journey_id}", headers=_auth(token)).get_json()["journey"]
    blocked = client.patch(
        f"/api/v1/activities/{second['activity']['id']}",
        json={"elevation_ft": "900"},
        headers=_auth(token),
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "resume_required"
    assert "arrivals" not in blocked.get_json()
    after = client.get(f"/api/v1/journeys/{journey_id}", headers=_auth(token)).get_json()["journey"]
    assert after["status"] == "abandoned"
    assert after["progress_version"] == before["progress_version"]
    assert after["metrics"] == before["metrics"]
    with app.app_context():
        assert _count("journey_story_views", "journey_id = ?", (journey_id,)) == 0


def test_a_rejected_summit_writes_nothing_and_a_confirmed_one_returns_the_final_arrival(client, app):
    feet = feet_to_meters
    with app.app_context():
        route_id = _route("arrival-summit", [("elevation", feet(Decimal("100")))])
        summit = _stop(route_id, "summit", 0, [("elevation", feet(Decimal("100")))], name="Summit")
        _story(summit, route_id, "Summit", journal=True)
        _publish(route_id)

    user_id, token = _register(client, "SummitReader")
    _party(client, token, "arrival-summit-party", "arrival-summit")
    journey_id = _home(client, token)["journey"]["id"]
    preview = client.post(
        "/api/v1/activities/preview",
        json={"elevation_ft": "100", "happened_on": DAY},
        headers=_auth(token),
    ).get_json()["preview"]
    assert preview["will_complete"] is True
    assert "arrivals" not in preview
    logged = _save(client, token, elevation_ft="10")
    assert logged["arrivals"] == []
    stale = _post(
        client,
        token,
        elevation_ft="100",
        confirmation_token=preview["confirmation_token"],
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "confirmation_required"
    assert "arrivals" not in stale.get_json()
    with app.app_context():
        assert _count("activities", "user_id = ?", (user_id,)) == 1
        assert _count("journey_story_views", "journey_id = ?", (journey_id,)) == 0
    assert _journal_slugs(client, token, journey_id) == []
    edit_preview = client.post(
        f"/api/v1/activities/{logged['activity']['id']}/preview",
        json={"elevation_ft": "100"},
        headers=_auth(token),
    ).get_json()["preview"]
    assert edit_preview["will_complete"] is True
    rejected_edit = client.patch(
        f"/api/v1/activities/{logged['activity']['id']}",
        json={"elevation_ft": "100"},
        headers=_auth(token),
    )
    assert rejected_edit.status_code == 409
    assert "arrivals" not in rejected_edit.get_json()
    confirmed = client.patch(
        f"/api/v1/activities/{logged['activity']['id']}",
        json={
            "elevation_ft": "100",
            "confirmation_token": edit_preview["confirmation_token"],
        },
        headers=_auth(token),
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    assert confirmed.get_json()["journey"]["status"] == "completed"
    assert confirmed.get_json()["arrivals"] == [_arrival("summit", "Summit", None)]
    assert _journal_slugs(client, token, journey_id) == ["summit"]
    with app.app_context():
        assert _feed(user_id) == [
            ("activity_logged", None),
            ("summit_reached", None),
        ]
        assert _count("journey_story_views", "journey_id = ?", (journey_id,)) == 0
    version = client.get(f"/api/v1/journeys/{journey_id}", headers=_auth(token)).get_json()["journey"]
    _ack(client, token, journey_id, "summit")
    after = client.get(f"/api/v1/journeys/{journey_id}", headers=_auth(token)).get_json()["journey"]
    assert after["status"] == "completed"
    assert after["progress_version"] == version["progress_version"]


def test_home_pending_arrivals_follow_the_active_attempt(client, app):
    feet = feet_to_meters
    with app.app_context():
        route_id = _route("arrival-home", [("elevation", feet(Decimal("1000")))])
        gate = _stop(route_id, "gate", 0, [("elevation", feet(Decimal("100")))], name="Gate")
        ridge = _stop(route_id, "ridge", 1, [("elevation", feet(Decimal("200")))], name="Ridge")
        quiet = _stop(route_id, "quiet", 2, [("elevation", feet(Decimal("250")))], name="Quiet")
        camp = _stop(route_id, "camp", 3, [("elevation", feet(Decimal("400")))], name="Camp")
        _stop(route_id, "summit", 4, [("elevation", feet(Decimal("1000")))], name="Summit")
        approach = _chapter(route_id, "approach", 0)
        _assign(gate, approach)
        _assign(ridge, approach)
        _assign(quiet, approach)
        _story(gate, route_id, "Gate")
        _story(ridge, route_id, "Ridge")
        _story(quiet, route_id, "Quiet", status="draft")
        _story(camp, route_id, "Camp")
        _publish(route_id)
        other_id = _route("arrival-other", [("elevation", feet(Decimal("1000")))])
        _stop(other_id, "end", 0, [("elevation", feet(Decimal("1000")))], name="End")
        _publish(other_id)

    _user, token = _register(client, "HomeReader")
    _start(client, token, "arrival-home")
    below = _home(client, token)
    journey_id = below["journey"]["id"]
    scene = below["journey"]["scene"]
    assert below["journey"]["pending_arrivals"] == []
    saved = _save(client, token, elevation_ft="400")
    assert [item["checkpoint_slug"] for item in saved["arrivals"]] == ["gate", "ridge", "camp"]
    home = _home(client, token)
    assert home["journey"]["pending_arrivals"] == [
        _pending("gate", "Gate", "approach"),
        _pending("ridge", "Ridge", "approach"),
        _pending("camp", "Camp", None),
    ]
    assert home["journey"]["scene"] == scene or home["journey"]["checkpoint"]["current"]["slug"] == "camp"
    current_scene = home["journey"]["scene"]
    with app.app_context():
        before = (
            _count("journey_story_views"),
            _count("expedition_updates"),
            _count("activities"),
            get_db().execute(
                "SELECT progress_version FROM route_journeys WHERE id = ?",
                (journey_id,),
            ).fetchone()["progress_version"],
        )
    reread = _home(client, token)
    assert reread["journey"]["scene"] == current_scene
    assert reread["journey"]["pending_arrivals"] == home["journey"]["pending_arrivals"]
    with app.app_context():
        after = (
            _count("journey_story_views"),
            _count("expedition_updates"),
            _count("activities"),
            get_db().execute(
                "SELECT progress_version FROM route_journeys WHERE id = ?",
                (journey_id,),
            ).fetchone()["progress_version"],
        )
    assert after == before
    _ack(client, token, journey_id, "gate")
    acknowledged = _home(client, token)
    assert acknowledged["journey"]["pending_arrivals"] == [
        _pending("ridge", "Ridge", "approach"),
        _pending("camp", "Camp", None),
    ]
    assert acknowledged["journey"]["scene"] == current_scene
    lowered = client.patch(
        f"/api/v1/activities/{saved['activity']['id']}",
        json={"elevation_ft": "150"},
        headers=_auth(token),
    )
    assert lowered.status_code == 200, lowered.get_json()
    assert lowered.get_json()["arrivals"] == []
    assert _home(client, token)["journey"]["pending_arrivals"] == []
    hidden = client.get(
        f"/api/v1/journeys/{journey_id}/journal/ridge",
        headers=_auth(token),
    )
    assert hidden.status_code == 404
    with app.app_context():
        assert _count("journey_story_views", "journey_id = ?", (journey_id,)) == 1
    restored = client.patch(
        f"/api/v1/activities/{saved['activity']['id']}",
        json={"elevation_ft": "400"},
        headers=_auth(token),
    )
    assert restored.status_code == 200, restored.get_json()
    assert restored.get_json()["arrivals"] == [
        _arrival("ridge", "Ridge", "approach"),
        _arrival("camp", "Camp", None),
    ]
    assert _home(client, token)["journey"]["pending_arrivals"] == [
        _pending("ridge", "Ridge", "approach"),
        _pending("camp", "Camp", None),
    ]
    _ack(client, token, journey_id, "ridge")
    _ack(client, token, journey_id, "camp")
    assert _home(client, token)["journey"]["pending_arrivals"] == []
    switched = client.post(
        "/api/v1/journeys/switch",
        json={"route_slug": "arrival-other"},
        headers=_auth(token),
    )
    assert switched.status_code == 200, switched.get_json()
    moved = _home(client, token)
    assert moved["journey"]["route"]["slug"] == "arrival-other"
    assert moved["journey"]["pending_arrivals"] == []
    assert "pending_arrivals" not in moved["paused_journeys"][0]
    assert _journal_slugs(client, token, journey_id) == ["gate", "ridge", "camp"]
    _abandon(client, token, "arrival-other")
    paused_only = _home(client, token)
    assert paused_only["journey"] is None
    assert paused_only["focus"] is None
    assert "pending_arrivals" not in paused_only["paused_journeys"][0]
    _abandon(client, token, "arrival-home")
    finished = _home(client, token)
    assert finished["journey"] is None
    assert "pending_arrivals" not in finished["focus"]
    restarted = client.post(
        "/api/v1/journeys/restart",
        json={"route_slug": "arrival-home"},
        headers=_auth(token),
    )
    assert restarted.status_code == 200, restarted.get_json()
    new_id = restarted.get_json()["journey"]["id"]
    assert new_id != journey_id
    fresh = _home(client, token)
    assert fresh["journey"]["id"] == new_id
    assert fresh["journey"]["pending_arrivals"] == []
    assert _journal_slugs(client, token, journey_id) == ["gate", "ridge", "camp"]
    second = _save(client, token, elevation_ft="100")
    assert second["journey"]["id"] == new_id
    assert second["arrivals"] == [_arrival("gate", "Gate", "approach")]
    assert _home(client, token)["journey"]["pending_arrivals"] == [
        _pending("gate", "Gate", "approach"),
    ]
    old_story = client.get(
        f"/api/v1/journeys/{journey_id}/journal/gate",
        headers=_auth(token),
    )
    assert old_story.status_code == 200
    assert old_story.get_json()["acknowledged"] is True


def test_concurrent_creates_attribute_a_checkpoint_to_the_write_that_reached_it(client, app, monkeypatch):
    feet = feet_to_meters
    user_id, token = _register(client, "RaceReader")
    with app.app_context():
        route_id = _route("arrival-race", [("elevation", feet(Decimal("1000")))])
        gate = _stop(route_id, "gate", 0, [("elevation", feet(Decimal("100")))], name="Gate")
        _stop(route_id, "summit", 1, [("elevation", feet(Decimal("1000")))], name="Summit")
        _story(gate, route_id, "Gate")
        _publish(route_id)
    _party(client, token, "arrival-race-party", "arrival-race")
    with app.app_context():
        raced = _race_creates(
            app,
            monkeypatch,
            lambda: create_activity(user_id, {"elevation_ft": "100", "happened_on": DAY}),
            lambda: create_activity(user_id, {"elevation_ft": "100", "happened_on": DAY}),
        )
        assert "error" not in raced
        first = raced["other"]
        second = raced["result"]
        assert [item["checkpoint_slug"] for item in first["arrivals"]] == ["gate"]
        assert second["arrivals"] == []
        assert first["journey"]["id"] == second["journey"]["id"]
        total = get_db().execute(
            """
            SELECT credited_total
            FROM journey_metric_totals
            WHERE journey_id = ? AND metric_code = 'elevation'
            """,
            (first["journey"]["id"],),
        ).fetchone()["credited_total"]
        assert total == feet(Decimal("200"))
        assert _count(
            "journey_story_views",
            "journey_id = ?",
            (first["journey"]["id"],),
        ) == 0
        assert _feed(user_id) == [
            ("activity_logged", None),
            ("checkpoint_reached", "Gate"),
            ("activity_logged", None),
        ]


def _race_creates(app, monkeypatch, first_write, second_write) -> dict:
    """Commit the first activity before the second takes its user lock."""
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
                holder["result"] = second_write()
            except Exception as exc:
                holder["error"] = exc
            finally:
                get_db().close()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert reached.wait(timeout=10)
        holder["other"] = first_write()
    finally:
        allow.set()
        thread.join(timeout=20)
        monkeypatch.undo()
    assert not thread.is_alive()
    return holder
