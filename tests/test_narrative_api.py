"""Field journal reads and acknowledgments against the isolated Postgres database."""

from __future__ import annotations

import threading
from decimal import Decimal

from adventurer.db import get_db
from adventurer.measures import feet_to_meters
from adventurer.metrics import MILE_IN_METERS
from adventurer.narrative import acknowledge_arrival

DAY = "2026-06-01"
PHOTO = "/static/photos/lukla.jpg"
MISSING_PHOTO = "/static/photos/does-not-exist.jpg"
SECRET_NOTE = "secret commercial grant from the test"


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _register(client, name="Sam", *, cookie=False):
    response = client.post(
        "/api/v1/auth/register",
        json={"name": name, "password": "trailhead"},
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    if not cookie:
        client.delete_cookie("session")
    return body["you"]["id"], body["token"], body["csrf"]


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


def _stop(route_id: int, slug: str, position: int, thresholds: list[tuple[str, Decimal]]) -> int:
    database = get_db()
    checkpoint_id = database.execute(
        """
        INSERT INTO route_checkpoints (route_id, slug, name, position)
        VALUES (?, ?, ?, ?)
        RETURNING id
        """,
        (route_id, slug, slug.replace("-", " ").title(), position),
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


def _story(checkpoint_id: int, route_id: int, *, status: str = "published", journal: bool = False) -> None:
    get_db().execute(
        """
        INSERT INTO checkpoint_stories (
            checkpoint_id, route_id, arrival_title, arrival_body,
            journal_title, journal_body, status
        )
        VALUES (?, ?, 'The gate', 'A short reveal.', ?, ?, ?)
        """,
        (
            checkpoint_id,
            route_id,
            "Below the ridge" if journal else None,
            "The longer entry." if journal else None,
            status,
        ),
    )


def _publish(route_id: int) -> None:
    database = get_db()
    database.execute("UPDATE routes SET status = 'published' WHERE id = ?", (route_id,))
    _check_shape(database)
    database.commit()


def _start(client, token, slug: str) -> dict:
    response = client.post(
        "/api/v1/journeys/start",
        json={"route_slug": slug},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["journey"]


def _credit(client, token, **fields) -> dict:
    body = {"happened_on": DAY, **fields}
    headers = _auth(token)
    response = client.post("/api/v1/activities", json=body, headers=headers)
    if response.status_code == 409 and response.get_json().get("code") == "confirmation_required":
        body["confirmation_token"] = response.get_json()["details"]["preview"]["confirmation_token"]
        response = client.post("/api/v1/activities", json=body, headers=headers)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _journal(client, token, journey_id: int):
    return client.get(f"/api/v1/journeys/{journey_id}/journal", headers=_auth(token))


def _story_response(client, token, journey_id: int, slug: str):
    return client.get(
        f"/api/v1/journeys/{journey_id}/journal/{slug}",
        headers=_auth(token),
    )


def _entries(payload: dict) -> list[dict]:
    return [entry for chapter in payload["chapters"] for entry in chapter["entries"]]


def test_route_catalog_lists_only_published_chapters(client, app):
    feet = [feet_to_meters(Decimal(amount)) for amount in ("100", "200", "400")]
    with app.app_context():
        route_id = _route("narrative-catalog", [("elevation", feet[2])])
        gate = _stop(route_id, "gate", 0, [("elevation", feet[0])])
        ridge = _stop(route_id, "ridge", 1, [("elevation", feet[1])])
        camp = _stop(route_id, "camp", 2, [("elevation", feet[2])])
        approach = _chapter(route_id, "approach", 0)
        _chapter(route_id, "hidden", 1, status="draft")
        _chapter(route_id, "old", 2, status="retired")
        _assign(gate, approach)
        _assign(ridge, approach)
        _story(gate, route_id, journal=True)
        _publish(route_id)
        assert camp
    detail = client.get("/api/v1/routes/narrative-catalog").get_json()
    listed = next(
        row for row in client.get("/api/v1/routes").get_json()["routes"] if row["slug"] == "narrative-catalog"
    )
    assert listed["chapters"] == detail["chapters"] == [
        {"slug": "approach", "title": "Approach", "position": 0, "checkpoints": ["gate", "ridge"]}
    ]
    assert [stop["slug"] for stop in detail["checkpoints"]] == ["gate", "ridge", "camp"]
    assert detail["requirements"] == listed["requirements"]
    assert "arrival_body" not in str(detail["chapters"])
    everest = client.get("/api/v1/routes/everest").get_json()
    assert everest["chapters"] == []
    assert len(everest["checkpoints"]) == 16


def test_combined_route_payload_keeps_its_thresholds(client, app):
    elevation = [feet_to_meters(Decimal(amount)) for amount in ("100", "200")]
    distance = [Decimal(amount) * MILE_IN_METERS for amount in ("1", "2")]
    with app.app_context():
        route_id = _route(
            "narrative-combined",
            [("distance", distance[1]), ("elevation", elevation[1])],
        )
        _stop(
            route_id,
            "gate",
            0,
            [("distance", distance[0]), ("elevation", elevation[0])],
        )
        _stop(
            route_id,
            "summit",
            1,
            [("distance", distance[1]), ("elevation", elevation[1])],
        )
        _publish(route_id)
    detail = client.get("/api/v1/routes/narrative-combined").get_json()
    assert detail["chapters"] == []
    assert [item["metric"] for item in detail["requirements"]] == ["distance", "elevation"]
    gate = detail["checkpoints"][0]
    assert [item["metric"] for item in gate["requirements"]] == ["distance", "elevation"]
    assert "summit_ft" not in detail


def test_journal_reaches_only_published_stories_for_the_owner(client, app):
    feet = [feet_to_meters(Decimal(amount)) for amount in ("100", "100.01", "200", "350", "400", "1000")]
    with app.app_context():
        route_id = _route("narrative-journal", [("elevation", feet[5])])
        gate = _stop(route_id, "gate", 0, [("elevation", feet[0])])
        almost = _stop(route_id, "almost", 1, [("elevation", feet[1])])
        meadow = _stop(route_id, "meadow", 2, [("elevation", feet[2])])
        retired = _stop(route_id, "old-stop", 3, [("elevation", feet[3])])
        camp = _stop(route_id, "camp", 4, [("elevation", feet[4])])
        summit = _stop(route_id, "summit", 5, [("elevation", feet[5])])
        approach = _chapter(route_id, "approach", 0)
        draft_chapter = _chapter(route_id, "later", 1, status="draft")
        _assign(gate, approach)
        _assign(almost, approach)
        _assign(camp, draft_chapter)
        _story(gate, route_id, journal=True)
        _story(almost, route_id)
        _story(meadow, route_id, status="draft")
        _story(retired, route_id, status="retired")
        _story(camp, route_id)
        assert summit
        database = get_db()
        database.execute(
            """
            INSERT INTO story_discoveries (
                checkpoint_id, route_id, slug, kind, title, body, position, status
            )
            VALUES
                (?, ?, 'stone', 'historical', 'The old gate', 'A longer note.', 0, 'published'),
                (?, ?, 'hidden-note', 'cultural', 'Hidden', 'Secret body.', 1, 'draft')
            """,
            (gate, route_id, gate, route_id),
        )
        _publish(route_id)
    _owner, token, _csrf = _register(client, "JournalOwner")
    _other, other_token, _other_csrf = _register(client, "JournalOther")
    journey = _start(client, token, "narrative-journal")
    other = _start(client, other_token, "narrative-journal")
    anonymous = client.get(f"/api/v1/journeys/{journey['id']}/journal")
    assert anonymous.status_code == 401
    assert anonymous.get_json()["code"] == "unauthenticated"
    foreign = _journal(client, other_token, journey["id"])
    assert foreign.status_code == 403
    assert foreign.get_json()["code"] == "forbidden"
    assert "A short reveal." not in foreign.get_json()["error"]
    with app.app_context():
        expedition_id = get_db().execute(
            """
            INSERT INTO expeditions (
                slug, name, route_id, visibility, status, kind, owner_user_id
            )
            VALUES ('narrative-party', 'Narrative Party', ?, 'private', 'active', 'private', ?)
            RETURNING id
            """,
            (journey["route_id"], other["user_id"]),
        ).fetchone()["id"]
        get_db().execute(
            """
            INSERT INTO expedition_participations (
                user_id, expedition_id, route_id, journey_id, status
            )
            VALUES (?, ?, ?, ?, 'active')
            """,
            (other["user_id"], expedition_id, journey["route_id"], other["id"]),
        )
        get_db().commit()
    still_foreign = _journal(client, other_token, journey["id"])
    assert still_foreign.status_code == 403
    empty = _journal(client, token, journey["id"]).get_json()
    assert empty["chapters"] == []
    assert empty["status"] == "active"
    _credit(client, token, elevation_ft="100")
    partial = _journal(client, token, journey["id"]).get_json()
    assert [entry["checkpoint_slug"] for entry in _entries(partial)] == ["gate"]
    assert partial["chapters"][0]["slug"] == "approach"
    assert _entries(partial)[0]["has_journal"] is True
    assert _entries(partial)[0]["discovery_count"] == 1
    assert _entries(partial)[0]["acknowledged"] is False
    assert "journal_body" not in _entries(partial)[0]
    assert "The longer entry." not in str(partial)
    _credit(client, token, elevation_ft="300")
    full = _journal(client, token, journey["id"]).get_json()
    assert [entry["checkpoint_slug"] for entry in _entries(full)] == ["gate", "almost", "camp"]
    assert full["chapters"][0]["slug"] == "approach"
    assert [entry["checkpoint_slug"] for entry in full["chapters"][0]["entries"]] == ["gate", "almost"]
    assert full["chapters"][1]["slug"] is None
    assert full["chapters"][1]["title"] is None
    assert full["chapters"][1]["position"] is None
    assert [entry["checkpoint_slug"] for entry in full["chapters"][1]["entries"]] == ["camp"]
    missing = client.get(f"/api/v1/journeys/{journey['id'] + 999999}/journal", headers=_auth(token))
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "not_found"


def test_distance_and_combined_thresholds_gate_the_journal(client, app):
    mile = Decimal("1") * MILE_IN_METERS
    two_miles = Decimal("2") * MILE_IN_METERS
    hundred = feet_to_meters(Decimal("100"))
    two_hundred = feet_to_meters(Decimal("200"))
    with app.app_context():
        distance_id = _route("narrative-distance", [("distance", two_miles)])
        gate = _stop(distance_id, "milepost", 0, [("distance", mile)])
        _stop(distance_id, "finish", 1, [("distance", two_miles)])
        _story(gate, distance_id)
        _publish(distance_id)
        combined_id = _route(
            "narrative-both",
            [("distance", mile), ("elevation", two_hundred)],
        )
        both = _stop(
            combined_id,
            "both",
            0,
            [("distance", mile), ("elevation", hundred)],
        )
        _stop(
            combined_id,
            "end",
            1,
            [("distance", mile), ("elevation", two_hundred)],
        )
        _story(both, combined_id)
        _publish(combined_id)
    _id, token, _csrf = _register(client, "MetricReader")
    distance = _start(client, token, "narrative-distance")
    _credit(client, token, elevation_ft="10", distance_mi="1")
    assert [entry["checkpoint_slug"] for entry in _entries(_journal(client, token, distance["id"]).get_json())] == [
        "milepost"
    ]
    client.post("/api/v1/journeys/abandon", json={"route_slug": "narrative-distance"}, headers=_auth(token))
    combined = _start(client, token, "narrative-both")
    _credit(client, token, elevation_ft="100")
    assert _entries(_journal(client, token, combined["id"]).get_json()) == []
    _credit(client, token, elevation_ft="1", distance_mi="1")
    assert [entry["checkpoint_slug"] for entry in _entries(_journal(client, token, combined["id"]).get_json())] == [
        "both"
    ]


def test_paused_completed_and_abandoned_attempts_keep_their_journal(client, app):
    hundred = feet_to_meters(Decimal("100"))
    thousand = feet_to_meters(Decimal("1000"))
    with app.app_context():
        for slug, target in (("narrative-open", thousand), ("narrative-done", hundred), ("narrative-side", hundred)):
            route_id = _route(slug, [("elevation", target)])
            stop = _stop(route_id, "gate", 0, [("elevation", hundred if slug != "narrative-done" else target)])
            if slug == "narrative-open":
                _stop(route_id, "summit", 1, [("elevation", target)])
            _story(stop, route_id, journal=True)
            _publish(route_id)
    _id, token, _csrf = _register(client, "AttemptReader")
    headers = _auth(token)
    active = _start(client, token, "narrative-open")
    _credit(client, token, elevation_ft="100")
    client.post("/api/v1/journeys/switch", json={"route_slug": "narrative-side"}, headers=headers)
    paused = _journal(client, token, active["id"]).get_json()
    assert paused["status"] == "paused"
    assert [entry["checkpoint_slug"] for entry in _entries(paused)] == ["gate"]
    client.post("/api/v1/journeys/abandon", json={"route_slug": "narrative-side"}, headers=headers)
    done = _start(client, token, "narrative-done")
    _credit(client, token, elevation_ft="100")
    completed = _journal(client, token, done["id"]).get_json()
    assert completed["status"] == "completed"
    assert _entries(completed)[0]["checkpoint_slug"] == "gate"
    client.post("/api/v1/journeys/restart", json={"route_slug": "narrative-open"}, headers=headers)
    abandoned = _journal(client, token, active["id"]).get_json()
    assert abandoned["status"] == "abandoned"
    assert _entries(abandoned)[0]["checkpoint_slug"] == "gate"


def test_story_detail_hides_unpublished_and_missing_media(client, app):
    target = feet_to_meters(Decimal("100"))
    with app.app_context():
        route_id = _route("narrative-story", [("elevation", feet_to_meters(Decimal("1000")))])
        gate = _stop(route_id, "gate", 0, [("elevation", target)])
        quiet = _stop(route_id, "quiet", 1, [("elevation", target)])
        locked = _stop(route_id, "locked", 2, [("elevation", feet_to_meters(Decimal("1000")))])
        _stop(route_id, "summit", 3, [("elevation", feet_to_meters(Decimal("1000")))])
        _story(gate, route_id, journal=True)
        _story(quiet, route_id, status="draft", journal=True)
        _story(locked, route_id, journal=True)
        database = get_db()
        discovery_id = database.execute(
            """
            INSERT INTO story_discoveries (
                checkpoint_id, route_id, slug, kind, title, body, position, status
            )
            VALUES (?, ?, 'stone', 'historical', 'The old gate', 'A longer note.', 0, 'published')
            RETURNING id
            """,
            (gate, route_id),
        ).fetchone()["id"]
        database.execute(
            """
            INSERT INTO story_discoveries (
                checkpoint_id, route_id, slug, kind, title, body, position, status
            )
            VALUES (?, ?, 'secret', 'cultural', 'Secret', 'Do not show.', 1, 'draft')
            """,
            (gate, route_id),
        )
        database.execute(
            """
            INSERT INTO route_media (
                route_id, checkpoint_id, role, asset_path, alt_text, rights_basis,
                author, license, license_url, source_url, permission_note, position, status
            )
            VALUES
                (
                    ?, ?, 'journal', ?, 'The gate from the path', 'creative_commons',
                    'Ada Lovelace', 'CC BY 4.0',
                    'https://creativecommons.org/licenses/by/4.0',
                    'https://example.com/gate', ?, 0, 'published'
                ),
                (
                    ?, ?, 'journal', ?, 'Missing file', 'creative_commons',
                    'Ada Lovelace', 'CC BY 4.0',
                    'https://creativecommons.org/licenses/by/4.0',
                    'https://example.com/missing', NULL, 1, 'published'
                ),
                (
                    ?, ?, 'journal', ?, 'Draft image', 'undocumented',
                    NULL, NULL, NULL, NULL, NULL, 2, 'draft'
                ),
                (
                    ?, ?, 'checkpoint_scene', ?, 'Scene', 'creative_commons',
                    'Ada Lovelace', 'CC BY 4.0',
                    'https://creativecommons.org/licenses/by/4.0',
                    'https://example.com/scene', NULL, 0, 'published'
                )
            """,
            (
                route_id, gate, PHOTO, SECRET_NOTE,
                route_id, gate, MISSING_PHOTO,
                route_id, gate, PHOTO,
                route_id, gate, PHOTO,
            ),
        )
        database.execute(
            """
            INSERT INTO route_media (
                route_id, checkpoint_id, discovery_id, role, asset_path, alt_text,
                rights_basis, license, source_url, position, status
            )
            VALUES (?, ?, ?, 'discovery', ?, 'A stone', 'public_domain', 'Public domain',
                    'https://example.com/stone', 0, 'published')
            """,
            (route_id, gate, discovery_id, PHOTO),
        )
        _publish(route_id)
    _id, token, _csrf = _register(client, "StoryReader")
    journey = _start(client, token, "narrative-story")
    _credit(client, token, elevation_ft="100")
    response = _story_response(client, token, journey["id"], "gate")
    assert response.status_code == 200
    body = response.get_json()
    assert body["chapter_slug"] is None
    assert body["journal"] == {"title": "Below the ridge", "body": "The longer entry."}
    assert [item["slug"] for item in body["discoveries"]] == ["stone"]
    assert "Do not show." not in str(body)
    assert SECRET_NOTE not in str(body)
    assert "permission_note" not in str(body)
    assert body["media"] == [
        {
            "role": "journal",
            "path": PHOTO,
            "alt_text": "The gate from the path",
            "rights_basis": "creative_commons",
            "license": "CC BY 4.0",
            "position": 0,
            "author": "Ada Lovelace",
            "license_url": "https://creativecommons.org/licenses/by/4.0",
            "source_url": "https://example.com/gate",
        }
    ]
    assert body["discoveries"][0]["media"][0]["rights_basis"] == "public_domain"
    assert body["discoveries"][0]["media"][0]["license"] == "Public domain"
    hidden = [
        _story_response(client, token, journey["id"], slug)
        for slug in ("locked", "quiet", "missing")
    ]
    assert [item.status_code for item in hidden] == [404, 404, 404]
    assert hidden[0].get_json() == hidden[1].get_json() == hidden[2].get_json()
    assert hidden[0].get_json()["code"] == "not_found"
    assert "A short reveal." not in hidden[0].get_json()["error"]
    _other, other_token, _csrf = _register(client, "StoryOther")
    assert _story_response(client, other_token, journey["id"], "gate").status_code == 403


def test_acknowledgment_is_idempotent_and_does_not_change_progress(client, app):
    target = feet_to_meters(Decimal("1000"))
    gate_amount = feet_to_meters(Decimal("100"))
    with app.app_context():
        route_id = _route("narrative-ack", [("elevation", target)])
        gate = _stop(route_id, "gate", 0, [("elevation", gate_amount)])
        _stop(route_id, "summit", 1, [("elevation", target)])
        _story(gate, route_id)
        _publish(route_id)
    _id, token, csrf = _register(client, "AckReader", cookie=True)
    headers = _auth(token)
    journey = _start(client, token, "narrative-ack")
    created = _credit(client, token, elevation_ft="100")
    activity_id = created["activity"]["id"]
    with app.app_context():
        before = _snapshot(journey["id"])
    cookie_headers = {"X-CSRF-Token": csrf}
    missing_csrf = client.post(
        f"/api/v1/journeys/{journey['id']}/journal/gate/acknowledgment",
    )
    assert missing_csrf.status_code == 400
    assert missing_csrf.get_json()["code"] == "csrf"
    first = client.post(
        f"/api/v1/journeys/{journey['id']}/journal/gate/acknowledgment",
        headers={**headers, **cookie_headers},
    )
    assert first.status_code == 200, first.get_json()
    second = client.post(
        f"/api/v1/journeys/{journey['id']}/journal/gate/acknowledgment",
        headers=headers,
    )
    assert second.get_json() == first.get_json()
    assert _journal(client, token, journey["id"]).get_json()["chapters"][0]["entries"][0]["acknowledged"] is True
    with app.app_context():
        after = _snapshot(journey["id"])
        assert after["views"] == 1
        assert after["progress_version"] == before["progress_version"]
        assert after["credits"] == before["credits"]
        assert after["total"] == before["total"]
        assert after["status"] == before["status"]
        assert after["updates"] == before["updates"]
    unreached = client.post(
        f"/api/v1/journeys/{journey['id']}/journal/summit/acknowledgment",
        headers=headers,
    )
    assert unreached.status_code == 404
    lowered = client.patch(
        f"/api/v1/activities/{activity_id}",
        json={"elevation_ft": "40"},
        headers=headers,
    )
    assert lowered.status_code == 200, lowered.get_json()
    hidden = _story_response(client, token, journey["id"], "gate")
    assert hidden.status_code == 404
    with app.app_context():
        assert _snapshot(journey["id"])["views"] == 1
    restored = client.patch(
        f"/api/v1/activities/{activity_id}",
        json={"elevation_ft": "100"},
        headers=headers,
    )
    assert restored.status_code == 200, restored.get_json()
    again = _story_response(client, token, journey["id"], "gate").get_json()
    assert again["acknowledged"] is True
    client.post("/api/v1/journeys/abandon", json={"route_slug": "narrative-ack"}, headers=headers)
    restarted = client.post(
        "/api/v1/journeys/restart",
        json={"route_slug": "narrative-ack"},
        headers=headers,
    ).get_json()["journey"]
    fresh = _journal(client, token, restarted["id"]).get_json()
    assert fresh["chapters"] == []
    kept = _journal(client, token, journey["id"]).get_json()
    assert kept["status"] == "abandoned"
    assert kept["chapters"][0]["entries"][0]["acknowledged"] is True


def test_concurrent_acknowledgments_share_one_row(app):
    target = feet_to_meters(Decimal("100"))
    with app.app_context():
        route_id = _route("narrative-race", [("elevation", target)])
        gate = _stop(route_id, "gate", 0, [("elevation", target)])
        _story(gate, route_id)
        _publish(route_id)
        user_id = get_db().execute(
            """
            INSERT INTO users (username, username_key, password_hash)
            VALUES ('RaceUser', 'raceuser', 'hash')
            RETURNING id
            """
        ).fetchone()["id"]
        journey_id = get_db().execute(
            """
            INSERT INTO route_journeys (user_id, route_id, status, attempt_number, is_current)
            VALUES (?, ?, 'active', 1, true)
            RETURNING id
            """,
            (user_id, route_id),
        ).fetchone()["id"]
        activity_id = get_db().execute(
            """
            INSERT INTO activities (user_id, activity_date, elevation_gain_m, note)
            VALUES (?, DATE '2026-06-01', ?, 'note')
            RETURNING id
            """,
            (user_id, target),
        ).fetchone()["id"]
        get_db().execute(
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
        assignment_id = get_db().execute(
            """
            INSERT INTO activity_journey_assignments (
                activity_id, journey_id, user_id, route_id
            )
            VALUES (?, ?, ?, ?)
            RETURNING id
            """,
            (activity_id, journey_id, user_id, route_id),
        ).fetchone()["id"]
        get_db().execute(
            """
            INSERT INTO journey_credits (
                assignment_id, journey_id, route_id, metric_code, credited_amount
            )
            VALUES (?, ?, ?, 'elevation', ?)
            """,
            (assignment_id, journey_id, route_id, target),
        )
        get_db().commit()
    results = []
    errors = []

    def run() -> None:
        with app.app_context():
            try:
                results.append(acknowledge_arrival(user_id, journey_id, "gate"))
            except Exception as exc:
                errors.append(exc)
            finally:
                get_db().close()

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert errors == []
    assert len(results) == 2
    assert results[0]["acknowledged_at"] == results[1]["acknowledged_at"]
    with app.app_context():
        count = get_db().execute(
            "SELECT COUNT(*) AS n FROM journey_story_views WHERE journey_id = ?",
            (journey_id,),
        ).fetchone()["n"]
    assert count == 1


def _snapshot(journey_id: int) -> dict:
    database = get_db()
    journey = database.execute(
        "SELECT status, progress_version FROM route_journeys WHERE id = ?",
        (journey_id,),
    ).fetchone()
    total = database.execute(
        """
        SELECT credited_total
        FROM journey_metric_totals
        WHERE journey_id = ? AND metric_code = 'elevation'
        """,
        (journey_id,),
    ).fetchone()
    return {
        "status": journey["status"],
        "progress_version": journey["progress_version"],
        "credits": database.execute(
            "SELECT COUNT(*) AS n FROM journey_credits WHERE journey_id = ?",
            (journey_id,),
        ).fetchone()["n"],
        "total": None if total is None else total["credited_total"],
        "views": database.execute(
            "SELECT COUNT(*) AS n FROM journey_story_views WHERE journey_id = ?",
            (journey_id,),
        ).fetchone()["n"],
        "updates": database.execute("SELECT COUNT(*) AS n FROM expedition_updates").fetchone()["n"],
    }
