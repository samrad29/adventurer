"""Published route_media is the only runtime source for a route scene."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path

from adventurer.content_pipeline import import_route
from adventurer.db import DB, get_db
from adventurer.home import compose_home
from adventurer.journeys import start_journey
from adventurer.measures import feet_to_meters
from adventurer.metrics import MILE_IN_METERS
from adventurer.narrative import _asset_is_readable
from adventurer.scene import select_scene
from adventurer.services import create_adventurer

LUKLA = "/static/photos/lukla.jpg"
NAMCHE = "/static/photos/namche.jpg"
SUMMIT = "/static/photos/summit.jpg"
MISSING = "/static/photos/does-not-exist.jpg"
MALFORMED = "/static/photos/../photo_credits.json"
SECRET = "private grant for the scene test"


def test_published_registry_selects_hero_scene_and_lower_fallback(app):
    low = Decimal("10")
    high = Decimal("40")
    with app.app_context():
        route_id = _route("scene-ridge", [("elevation", high)])
        first = _stop(route_id, "low", 1, [("elevation", low)])
        second = _stop(route_id, "bare", 2, [("elevation", high)])
        _publish(route_id)
        _media(route_id, None, "route_hero", LUKLA, "The approach", status="published")
        _media(route_id, first, "checkpoint_scene", NAMCHE, "The lower camp", status="published")
        _media(route_id, second, "checkpoint_scene", SUMMIT, "The locked summit", status="draft")
        _media(route_id, first, "journal", SUMMIT, "A journal picture", status="published", position=1)
        get_db().commit()
        stops = _stops(route_id)

        below = _scene(route_id, stops, {})
        assert below["role"] == "route_hero"
        assert below["checkpoint_slug"] is None
        assert below["fallback"] is None
        assert below["image"]["path"] == LUKLA
        assert below["image"]["alt"] == "The approach"
        assert below["image"]["credit"]["rights_basis"] == "creative_commons"
        assert below["image"]["credit"]["author"] == "Ada"
        assert SECRET not in json.dumps(below)

        reached = _scene(route_id, stops, {"elevation": low})
        assert reached["role"] == "checkpoint"
        assert reached["checkpoint_slug"] == "low"
        assert reached["fallback"] is None
        assert reached["image"]["path"] == NAMCHE
        assert "summit.jpg" not in reached["image"]["path"]

        missing_own_scene = _scene(route_id, stops, {"elevation": high})
        assert missing_own_scene["checkpoint_slug"] == "bare"
        assert missing_own_scene["image"]["path"] == NAMCHE
        assert missing_own_scene["fallback"] == "lower_checkpoint"
        assert "summit.jpg" not in missing_own_scene["image"]["path"]


def test_unreached_draft_retired_missing_and_malformed_media_are_skipped(app):
    target = Decimal("50")
    with app.app_context():
        route_id = _route("scene-skip", [("elevation", target)])
        low = _stop(route_id, "low", 1, [("elevation", Decimal("10"))])
        high = _stop(route_id, "high", 2, [("elevation", target)])
        _publish(route_id)
        _media(route_id, high, "checkpoint_scene", SUMMIT, "Unreached summit", status="published")
        _media(route_id, low, "checkpoint_scene", NAMCHE, "Draft camp", status="draft")
        _media(route_id, None, "route_hero", LUKLA, "Retired hero", status="retired")
        _media(route_id, low, "checkpoint_scene", MISSING, "Missing file", status="published", position=1)
        get_db().commit()
        stops = _stops(route_id)
        reached = _scene(route_id, stops, {"elevation": Decimal("10")})
        assert reached["image"] is None
        assert reached["role"] == "none"
        assert reached["checkpoint_slug"] == "low"
        assert SUMMIT not in json.dumps(reached)

        database = get_db()
        database.execute(
            """
            UPDATE route_media
            SET asset_path = ?, status = 'published'
            WHERE route_id = ? AND alt_text = 'Retired hero'
            """,
            (MALFORMED, route_id),
        )
        database.commit()
        malformed = _scene(route_id, stops, {})
        assert malformed["image"] is None
        assert malformed["role"] == "none"


def test_route_without_registry_rows_and_legacy_files_return_null(app):
    assert _asset_is_readable(LUKLA) is True
    with app.app_context():
        route_id = _route("scene-empty", [("elevation", Decimal("10"))])
        _stop(route_id, "lukla", 1, [("elevation", Decimal("10"))])
        _publish(route_id)
        stops = _stops(route_id)
        scene = _scene(route_id, stops, {"elevation": Decimal("10")})
        assert scene["image"] is None
        assert scene["role"] == "none"
        assert scene["checkpoint_slug"] == "lukla"
        removed = Path(__file__).resolve().parents[1] / "adventurer" / "photo_credits.json"
        renamed = removed.with_name("photo_credits.json.held")
        removed.rename(renamed)
        try:
            still = _scene(route_id, stops, {"elevation": Decimal("10")})
        finally:
            renamed.rename(removed)
        assert still == scene


def test_cross_route_media_stays_on_its_route(app):
    target = Decimal("10")
    with app.app_context():
        first = _route("scene-east", [("elevation", target)])
        east = _stop(first, "camp", 1, [("elevation", target)])
        second = _route("scene-west", [("elevation", target)])
        _stop(second, "camp", 1, [("elevation", target)])
        _publish(first)
        _publish(second)
        _media(first, east, "checkpoint_scene", NAMCHE, "East camp")
        get_db().commit()
        east_scene = _scene(first, _stops(first), {"elevation": target})
        west_scene = _scene(second, _stops(second), {"elevation": target})
        assert east_scene["image"]["path"] == NAMCHE
        assert west_scene["image"] is None


def test_combined_route_scene_waits_for_every_threshold(app):
    elevation = [feet_to_meters(Decimal(amount)) for amount in ("100", "200")]
    distance = [Decimal(amount) * MILE_IN_METERS for amount in ("1", "2")]
    with app.app_context():
        route_id = _route(
            "scene-combined",
            [("distance", distance[1]), ("elevation", elevation[1])],
        )
        gate = _stop(
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
        _media(route_id, gate, "checkpoint_scene", LUKLA, "Combined gate")
        get_db().commit()
        stops = _stops(route_id)
        one_metric = _scene(route_id, stops, {"elevation": elevation[0]})
        assert one_metric["image"] is None
        assert one_metric["checkpoint_slug"] is None
        both = _scene(
            route_id,
            stops,
            {"elevation": elevation[0], "distance": distance[0]},
        )
        assert both["checkpoint_slug"] == "gate"
        assert both["image"]["path"] == LUKLA
        assert both["fallback"] is None


def test_import_publishes_and_retires_the_scene_without_a_filename_fallback(app, tmp_path):
    with app.app_context():
        route_id = _route("scene-import", [("elevation", Decimal("1000"))])
        _stop(route_id, "gate", 0, [("elevation", Decimal("100"))])
        _stop(route_id, "summit", 1, [("elevation", Decimal("1000"))])
        _publish(route_id)
        package = _package(
            tmp_path,
            "scene-import",
            scene_status="published",
            hero_status="published",
        )
        import_route(get_db(), package)
        stops = _stops(route_id)
        below = _scene(route_id, stops, {})
        assert below["image"]["path"] == LUKLA
        assert below["image"]["alt"] == "Imported approach"
        reached = _scene(route_id, stops, {"elevation": Decimal("100")})
        assert reached["image"]["path"] == NAMCHE
        assert reached["image"]["alt"] == "Imported gate"
        assert reached["fallback"] is None

        retired_scene = _package(
            tmp_path,
            "scene-import",
            scene_status="retired",
            hero_status="published",
        )
        import_route(get_db(), retired_scene)
        fallen_back = _scene(route_id, stops, {"elevation": Decimal("100")})
        assert fallen_back["image"]["path"] == LUKLA
        assert fallen_back["fallback"] == "route_hero"
        assert fallen_back["checkpoint_slug"] == "gate"

        retired_hero = _package(
            tmp_path,
            "scene-import",
            scene_status="retired",
            hero_status="retired",
        )
        import_route(get_db(), retired_hero)
        empty = _scene(route_id, stops, {"elevation": Decimal("100")})
        assert empty["image"] is None
        assert empty["role"] == "none"
        assert _asset_is_readable(LUKLA) is True
        assert _asset_is_readable(NAMCHE) is True


def test_expedition_cover_does_not_replace_the_route_scene(client, app):
    _owner, owner, _csrf = _register(client, "SceneOwner")
    with app.app_context():
        route_id = _route("scene-brand", [("elevation", Decimal("1000"))])
        _stop(route_id, "summit", 1, [("elevation", Decimal("1000"))])
        _publish(route_id)
        _media(route_id, None, "route_hero", LUKLA, "Branded route approach")
        get_db().commit()
    started = client.post(
        "/api/v1/journeys/start",
        json={"route_slug": "scene-brand"},
        headers=_auth(owner),
    )
    assert started.status_code == 201, started.get_json()
    created = client.post(
        "/api/v1/expeditions",
        json={"slug": "scene-party", "name": "Scene Party", "route_slug": "scene-brand"},
        headers=_auth(owner),
    )
    assert created.status_code == 201, created.get_json()
    with app.app_context():
        get_db().execute(
            "UPDATE expeditions SET image_ref = ? WHERE slug = 'scene-party'",
            ("/branding/expeditions/scene-party/cover/plain.png",),
        )
        get_db().commit()
    home = client.get("/api/v1/home", headers=_auth(owner)).get_json()
    assert home["journey"]["scene"]["image"]["path"] == LUKLA
    card = next(row for row in home["expeditions"] if row["slug"] == "scene-party")
    assert card["branding"]["image_ref"] == "/branding/expeditions/scene-party/cover/plain.png"
    assert card["image_ref"] == card["branding"]["image_ref"]
    assert home["journey"]["scene"]["image"]["path"] != card["branding"]["image_ref"]


def test_home_scene_stays_on_the_snapshot_when_media_publishes(app, monkeypatch):
    with app.app_context():
        route_id = _route("scene-snapshot", [("elevation", Decimal("1000"))])
        _stop(route_id, "summit", 1, [("elevation", Decimal("1000"))])
        _publish(route_id)
        user = create_adventurer("SceneSnap", "trailhead")
        start_journey(user["id"], route_id)
        main = get_db()
        reached = threading.Event()
        allow = threading.Event()
        seen = {"paused": False}
        original = DB.execute

        def wrapped(self, sql, params=()):
            normalized = " ".join(sql.split()).lower()
            if (
                not seen["paused"]
                and self is not main
                and "from route_media" in normalized
                and "checkpoint_scene" in normalized
            ):
                seen["paused"] = True
                reached.set()
                assert allow.wait(timeout=10)
            return original(self, sql, params)

        monkeypatch.setattr(DB, "execute", wrapped)
        holder: dict = {}

        def run() -> None:
            with app.app_context():
                try:
                    holder["result"] = compose_home(user["id"])
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            _media(route_id, None, "route_hero", LUKLA, "Published during the read")
            main.commit()
        finally:
            allow.set()
            thread.join(timeout=20)
            monkeypatch.undo()
        assert "error" not in holder, holder.get("error")
        assert holder["result"]["journey"]["scene"]["image"] is None
        fresh = compose_home(user["id"])
        assert fresh["journey"]["scene"]["image"]["path"] == LUKLA
        assert fresh["journey"]["progress_version"] == holder["result"]["journey"]["progress_version"]
        assert fresh["journey"]["pending_arrivals"] == holder["result"]["journey"]["pending_arrivals"]
        get_db().close()


def _register(client, name):
    response = client.post(
        "/api/v1/auth/register",
        json={"name": name, "password": "trailhead"},
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    client.delete_cookie("session")
    return body["you"]["id"], body["token"], body["csrf"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


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
        (route_id, slug, slug, position),
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


def _publish(route_id: int) -> None:
    database = get_db()
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


def _media(
    route_id: int,
    checkpoint_id: int | None,
    role: str,
    path: str,
    alt: str,
    *,
    status: str = "published",
    position: int = 0,
) -> None:
    get_db().execute(
        """
        INSERT INTO route_media (
            route_id, checkpoint_id, role, asset_path, alt_text, rights_basis,
            author, license, license_url, source_url, permission_note, position, status
        )
        VALUES (
            ?, ?, ?, ?, ?, 'creative_commons',
            'Ada', 'CC BY 4.0', 'https://creativecommons.org/licenses/by/4.0',
            'https://example.com/scene', ?, ?, ?
        )
        """,
        (route_id, checkpoint_id, role, path, alt, SECRET, position, status),
    )


def _stops(route_id: int) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT checkpoint.id, checkpoint.slug, checkpoint.position,
               requirement.metric_code, requirement.threshold_amount
        FROM route_checkpoints AS checkpoint
        JOIN checkpoint_requirements AS requirement
          ON requirement.checkpoint_id = checkpoint.id
        WHERE checkpoint.route_id = ?
        ORDER BY checkpoint.position, checkpoint.id, requirement.metric_code
        """,
        (route_id,),
    ).fetchall()
    stops: dict[int, dict] = {}
    for row in rows:
        stop = stops.setdefault(
            row["id"],
            {
                "id": row["id"],
                "slug": row["slug"],
                "position": row["position"],
                "thresholds": [],
            },
        )
        stop["thresholds"].append((row["metric_code"], row["threshold_amount"]))
    return list(stops.values())


def _scene(route_id: int, stops: list[dict], credited: dict[str, Decimal]) -> dict:
    scene = select_scene(get_db(), route_id, stops, credited)
    get_db().rollback()
    return scene


def _package(root: Path, slug: str, *, scene_status: str, hero_status: str) -> Path:
    package = root / slug
    package.mkdir(parents=True, exist_ok=True)
    (package / "checkpoints").mkdir(exist_ok=True)
    (package / "manifest.json").write_text(
        json.dumps({"format_version": 1, "route_slug": slug}) + "\n",
        encoding="utf-8",
    )
    (package / "chapters.json").write_text(
        json.dumps(
            {
                "chapters": [
                    {
                        "slug": "approach",
                        "title": "The approach",
                        "summary": "A fictional approach.",
                        "position": 0,
                        "status": "published",
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (package / "checkpoints" / "gate.md").write_text(
        "\n".join(
            [
                "---",
                json.dumps(
                    {
                        "status": "published",
                        "chapter": "approach",
                        "arrival_title": "The gate",
                        "journal_title": "Field note",
                    }
                ),
                "---",
                "",
                "# Arrival",
                "",
                "A short fictional reveal.",
                "",
                "# Journal",
                "",
                "A longer fictional note.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (package / "media.json").write_text(
        json.dumps(
            {
                "media": [
                    {
                        "role": "route_hero",
                        "path": LUKLA,
                        "alt_text": "Imported approach",
                        "rights_basis": "creative_commons",
                        "author": "Ada",
                        "license": "CC BY 4.0",
                        "license_url": "https://creativecommons.org/licenses/by/4.0",
                        "source_url": "https://example.com/scene/hero",
                        "position": 0,
                        "status": hero_status,
                    },
                    {
                        "role": "checkpoint_scene",
                        "checkpoint": "gate",
                        "path": NAMCHE,
                        "alt_text": "Imported gate",
                        "rights_basis": "creative_commons",
                        "author": "Ada",
                        "license": "CC BY 4.0",
                        "license_url": "https://creativecommons.org/licenses/by/4.0",
                        "source_url": "https://example.com/scene/gate",
                        "position": 0,
                        "status": scene_status,
                    },
                    {
                        "role": "journal",
                        "checkpoint": "gate",
                        "path": SUMMIT,
                        "alt_text": "Imported journal",
                        "rights_basis": "creative_commons",
                        "author": "Ada",
                        "license": "CC BY 4.0",
                        "license_url": "https://creativecommons.org/licenses/by/4.0",
                        "source_url": "https://example.com/scene/journal",
                        "position": 0,
                        "status": "published",
                    },
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return package
