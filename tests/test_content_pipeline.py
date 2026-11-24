"""Editorial package validation and import against the isolated Postgres database."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from adventurer.content_pipeline import (
    ContentRejected,
    format_plan,
    import_route,
    main,
    validate_content,
)
from adventurer.db import get_db
from adventurer.measures import feet_to_meters
from test_narrative_api import (
    PHOTO,
    SECRET_NOTE,
    _auth,
    _credit,
    _publish,
    _register,
    _route,
    _start,
    _stop,
    _story_response,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "content" / "routes" / "sample-ridge"
DAY = "2026-06-01"


def _issues(path: Path, database=None):
    return validate_content(path, database)


def _matching(issues, file: str, field: str, text: str) -> bool:
    return any(
        issue.file == file and issue.field == field and text in issue.message
        for issue in issues
    )


def _geography(slug: str, stops: list[str]) -> int:
    target_ft = Decimal("1000")
    route_id = _route(slug, [("elevation", feet_to_meters(target_ft))])
    for index, name in enumerate(stops):
        feet = target_ft if index == len(stops) - 1 else Decimal(100 * (index + 1))
        _stop(route_id, name, index, [("elevation", feet_to_meters(feet))])
    _publish(route_id)
    return route_id


def _markdown(checkpoint: dict) -> str:
    front = {
        "status": checkpoint["status"],
        "chapter": checkpoint["chapter"],
        "arrival_title": checkpoint["arrival_title"],
    }
    if "journal_title" in checkpoint:
        front["journal_title"] = checkpoint["journal_title"]
    discoveries = checkpoint.get("discoveries", [])
    if discoveries:
        front["discoveries"] = [item["meta"] for item in discoveries]
    lines = ["---", json.dumps(front, indent=2), "---", "", "# Arrival", "", checkpoint["arrival_body"], ""]
    if "journal_body" in checkpoint:
        lines.extend(["# Journal", "", checkpoint["journal_body"], ""])
    for item in discoveries:
        lines.extend([f"# Discovery {item['meta']['slug']}", "", item["body"], ""])
    return "\n".join(lines)


def _write(root: Path, slug: str, *, chapters: list[dict], checkpoints: list[dict], media: list[dict]) -> Path:
    package = root / slug
    if package.exists():
        shutil.rmtree(package)
    (package / "checkpoints").mkdir(parents=True)
    (package / "manifest.json").write_text(
        json.dumps({"format_version": 1, "route_slug": slug}, indent=2) + "\n",
        encoding="utf-8",
    )
    (package / "chapters.json").write_text(
        json.dumps({"chapters": chapters}, indent=2) + "\n",
        encoding="utf-8",
    )
    (package / "media.json").write_text(
        json.dumps({"media": media}, indent=2) + "\n",
        encoding="utf-8",
    )
    for checkpoint in checkpoints:
        (package / "checkpoints" / f"{checkpoint['slug']}.md").write_text(
            _markdown(checkpoint),
            encoding="utf-8",
        )
    return package


def _story(slug: str, *, status: str, chapter, title: str, body: str, journal: bool = False, discoveries=None) -> dict:
    checkpoint = {
        "slug": slug,
        "status": status,
        "chapter": chapter,
        "arrival_title": title,
        "arrival_body": body,
        "discoveries": discoveries or [],
    }
    if journal:
        checkpoint["journal_title"] = "Field note"
        checkpoint["journal_body"] = "A longer fictional note."
    return checkpoint


def _discovery(slug: str, body: str, *, position: int = 0, status: str = "published") -> dict:
    return {
        "meta": {
            "slug": slug,
            "kind": "historical",
            "title": slug.replace("-", " ").title(),
            "position": position,
            "status": status,
        },
        "body": body,
    }


def _cc(checkpoint: str, *, position: int = 0, status: str = "published", author: str | None = "Sample Author") -> dict:
    row = {
        "role": "journal",
        "checkpoint": checkpoint,
        "path": PHOTO,
        "alt_text": "A stand-in photograph for a fictional gate.",
        "rights_basis": "creative_commons",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "source_url": "https://example.com/content/gate",
        "position": position,
        "status": status,
    }
    if author is not None:
        row["author"] = author
    return row


def _commissioned(checkpoint: str) -> dict:
    return {
        "role": "journal",
        "checkpoint": checkpoint,
        "path": PHOTO,
        "alt_text": "A commissioned stand-in photograph.",
        "rights_basis": "commissioned",
        "author": "Sample Author",
        "license": "Sample commission",
        "permission_note": SECRET_NOTE,
        "position": 1,
        "status": "published",
    }


def _public_domain(checkpoint: str, discovery: str) -> dict:
    return {
        "role": "discovery",
        "checkpoint": checkpoint,
        "discovery": discovery,
        "path": PHOTO,
        "alt_text": "A stand-in photograph for a fictional discovery.",
        "rights_basis": "public_domain",
        "license": "Public domain",
        "source_url": "https://example.com/content/discovery",
        "position": 0,
        "status": "published",
    }


def _draft_scene(checkpoint: str) -> dict:
    return {
        "role": "checkpoint_scene",
        "checkpoint": checkpoint,
        "path": PHOTO,
        "alt_text": "An unpublished stand-in scene.",
        "rights_basis": "undocumented",
        "position": 0,
        "status": "draft",
    }


def _valid(root: Path, slug: str, *, include_camp: bool = True, include_lichen: bool = True) -> Path:
    discoveries = [_discovery("old-post", "A fictional post.")]
    if include_lichen:
        discoveries.append(_discovery("blue-lichen", "A draft lichen note.", position=1, status="draft"))
    checkpoints = [
        _story(
            "gate",
            status="published",
            chapter="approach",
            title="The cedar gate",
            body="A short fictional reveal.",
            journal=True,
            discoveries=discoveries,
        )
    ]
    if include_camp:
        checkpoints.append(
            _story("camp", status="draft", chapter="approach", title="Blue camp", body="A draft arrival.")
        )
    checkpoints.append(
        _story("summit", status="draft", chapter="ridge", title="The ridge mark", body="A draft ridge arrival.")
    )
    return _write(
        root,
        slug,
        chapters=[
            {
                "slug": "approach",
                "title": "The approach",
                "summary": "A fictional approach.",
                "position": 0,
                "status": "published",
            },
            {"slug": "ridge", "title": "The ridge", "position": 1, "status": "draft"},
        ],
        checkpoints=checkpoints,
        media=[
            _cc("gate"),
            _commissioned("gate"),
            _public_domain("gate", "old-post"),
            _draft_scene("gate"),
        ],
    )


def _ids(database, route_id: int) -> dict:
    return {
        "chapters": {
            row["slug"]: row["id"]
            for row in database.execute(
                "SELECT id, slug FROM route_chapters WHERE route_id = ?",
                (route_id,),
            ).fetchall()
        },
        "stories": {
            row["slug"]: row["id"]
            for row in database.execute(
                """
                SELECT story.id, checkpoint.slug
                FROM checkpoint_stories AS story
                JOIN route_checkpoints AS checkpoint ON checkpoint.id = story.checkpoint_id
                WHERE story.route_id = ?
                """,
                (route_id,),
            ).fetchall()
        },
        "discoveries": {
            (row["checkpoint"], row["slug"]): row["id"]
            for row in database.execute(
                """
                SELECT discovery.id, discovery.slug, checkpoint.slug AS checkpoint
                FROM story_discoveries AS discovery
                JOIN route_checkpoints AS checkpoint ON checkpoint.id = discovery.checkpoint_id
                WHERE discovery.route_id = ?
                """,
                (route_id,),
            ).fetchall()
        },
        "media": {
            (row["role"], row["checkpoint"], row["discovery"], row["position"]): row["id"]
            for row in database.execute(
                """
                SELECT media.id, media.role, media.position,
                       checkpoint.slug AS checkpoint, discovery.slug AS discovery
                FROM route_media AS media
                LEFT JOIN route_checkpoints AS checkpoint ON checkpoint.id = media.checkpoint_id
                LEFT JOIN story_discoveries AS discovery ON discovery.id = media.discovery_id
                WHERE media.route_id = ?
                """,
                (route_id,),
            ).fetchall()
        },
    }


def test_example_package_is_fictional_and_valid():
    prose = "\n".join(path.read_text(encoding="utf-8") for path in EXAMPLE.rglob("*") if path.is_file())
    assert "everest" not in prose.lower()
    assert _issues(EXAMPLE) == []


def test_example_is_not_created_when_the_route_is_missing(app):
    with app.app_context():
        issues = _issues(EXAMPLE, get_db())
        assert _matching(issues, "manifest.json", "route_slug", "route does not exist")
        assert (
            get_db()
            .execute("SELECT id FROM routes WHERE slug = 'sample-ridge'")
            .fetchone()
            is None
        )
        with pytest.raises(ContentRejected) as caught:
            import_route(get_db(), EXAMPLE)
        assert "route does not exist" in str(caught.value)


def test_cli_validates_the_example_and_refuses_other_databases(monkeypatch, capsys, tmp_path):
    completed = subprocess.run(
        [sys.executable, "-m", "adventurer.content_pipeline", "validate", str(EXAMPLE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "valid"

    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.supabase.co/postgres")
    assert main(["import", str(EXAMPLE), "--dry-run"]) == 1
    assert "does not read DATABASE_URL" in capsys.readouterr().err

    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql://postgres:postgres@example.com/adventurer_migration_test",
    )
    assert main(["validate", str(tmp_path), "--database"]) == 1
    assert "Refusing database host" in capsys.readouterr().err

    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql://postgres:postgres@127.0.0.1:54329/postgres",
    )
    assert main(["import", str(EXAMPLE)]) == 1
    assert "adventurer_migration_test" in capsys.readouterr().err


def test_valid_import_is_idempotent_and_serves_the_journal(client, app, tmp_path):
    slug = "content-valid"
    with app.app_context():
        route_id = _geography(slug, ["gate", "camp", "summit"])
        before = client.get(f"/api/v1/routes/{slug}").get_json()
        package = _valid(tmp_path, slug)
        plan = import_route(get_db(), package)
        assert "insert chapter approach" in format_plan(plan)
        assert "insert story gate" in format_plan(plan)
        first = _ids(get_db(), route_id)
        stamped = get_db().execute(
            """
            SELECT story.updated_at
            FROM checkpoint_stories AS story
            JOIN route_checkpoints AS checkpoint ON checkpoint.id = story.checkpoint_id
            WHERE checkpoint.slug = 'gate' AND story.route_id = ?
            """,
            (route_id,),
        ).fetchone()["updated_at"]
        again = import_route(get_db(), package)
        assert {change.action for change in again} == {"unchanged"}
        assert _ids(get_db(), route_id) == first
        assert (
            get_db()
            .execute(
                """
                SELECT story.updated_at
                FROM checkpoint_stories AS story
                JOIN route_checkpoints AS checkpoint ON checkpoint.id = story.checkpoint_id
                WHERE checkpoint.slug = 'gate' AND story.route_id = ?
                """,
                (route_id,),
            )
            .fetchone()["updated_at"]
            == stamped
        )
        after = client.get(f"/api/v1/routes/{slug}").get_json()
        assert after["requirements"] == before["requirements"]
        assert after["checkpoints"] == before["checkpoints"]
        assert after["chapters"] == [
            {
                "slug": "approach",
                "title": "The approach",
                "position": 0,
                "checkpoints": ["gate", "camp"],
            }
        ]
        _user, token, _csrf = _register(client, "ContentAuthor")
        journey = _start(client, token, slug)
        _credit(client, token, elevation_ft=200)
        journal = client.get(
            f"/api/v1/journeys/{journey['id']}/journal",
            headers=_auth(token),
        ).get_json()
        assert [chapter["slug"] for chapter in journal["chapters"]] == ["approach"]
        assert [entry["checkpoint_slug"] for entry in journal["chapters"][0]["entries"]] == ["gate"]
        assert journal["chapters"][0]["entries"][0]["has_journal"] is True
        assert journal["chapters"][0]["entries"][0]["discovery_count"] == 1
        story = _story_response(client, token, journey["id"], "gate").get_json()
        assert story["arrival_title"] == "The cedar gate"
        assert story["journal"]["title"] == "Field note"
        assert [item["slug"] for item in story["discoveries"]] == ["old-post"]
        assert [item["role"] for item in story["media"]] == ["journal", "journal"]
        assert story["discoveries"][0]["media"][0]["rights_basis"] == "public_domain"
        assert "permission_note" not in story
        assert SECRET_NOTE not in json.dumps(story)
        assert _story_response(client, token, journey["id"], "camp").status_code == 404
        stored = get_db().execute(
            """
            SELECT permission_note
            FROM route_media
            WHERE route_id = ? AND rights_basis = 'commissioned'
            """,
            (route_id,),
        ).fetchone()
        assert stored["permission_note"] == SECRET_NOTE


def test_dry_run_prints_the_plan_and_does_not_commit(app, tmp_path, monkeypatch, capsys):
    slug = "content-dry"
    with app.app_context():
        route_id = _geography(slug, ["gate", "summit"])
        package = _write(
            tmp_path,
            slug,
            chapters=[{"slug": "approach", "title": "Approach", "position": 0, "status": "published"}],
            checkpoints=[
                _story("gate", status="published", chapter="approach", title="Gate", body="A reveal.")
            ],
            media=[],
        )
        plan = import_route(get_db(), package, dry_run=True)
        assert "insert chapter approach" in format_plan(plan)
        assert (
            get_db()
            .execute("SELECT COUNT(*) AS n FROM route_chapters WHERE route_id = ?", (route_id,))
            .fetchone()["n"]
            == 0
        )
        monkeypatch.setenv(
            "TEST_DATABASE_URL",
            "postgresql://postgres:postgres@127.0.0.1:54329/adventurer_migration_test",
        )
        monkeypatch.setenv("DATABASE_URL", "postgresql://example.supabase.co/postgres")
        assert main(["import", str(package), "--dry-run"]) == 0
        assert "insert story gate" in capsys.readouterr().out
        assert (
            get_db()
            .execute("SELECT COUNT(*) AS n FROM route_chapters WHERE route_id = ?", (route_id,))
            .fetchone()["n"]
            == 0
        )


def test_update_preserves_identity_progress_and_acknowledgments(client, app, tmp_path):
    slug = "content-update"
    with app.app_context():
        route_id = _geography(slug, ["gate", "camp", "summit"])
        package = _valid(tmp_path, slug)
        import_route(get_db(), package)
        positions = [
            row["position"]
            for row in get_db()
            .execute(
                "SELECT position FROM route_checkpoints WHERE route_id = ? ORDER BY position",
                (route_id,),
            )
            .fetchall()
        ]
        threshold = get_db().execute(
            """
            SELECT requirement.threshold_amount
            FROM checkpoint_requirements AS requirement
            JOIN route_checkpoints AS checkpoint ON checkpoint.id = requirement.checkpoint_id
            WHERE checkpoint.route_id = ? AND checkpoint.slug = 'gate'
            """,
            (route_id,),
        ).fetchone()["threshold_amount"]
        _user, token, _csrf = _register(client, "ContentEditor")
        journey = _start(client, token, slug)
        _credit(client, token, elevation_ft=200)
        acknowledged = client.post(
            f"/api/v1/journeys/{journey['id']}/journal/gate/acknowledgment",
            headers=_auth(token),
        )
        assert acknowledged.status_code == 200, acknowledged.get_json()
        seen_at = acknowledged.get_json()["acknowledged_at"]
        before = get_db().execute(
            """
            SELECT journey.progress_version, journey.status, totals.credited_total
            FROM route_journeys AS journey
            JOIN journey_metric_totals AS totals ON totals.journey_id = journey.id
            WHERE journey.id = ?
            """,
            (journey["id"],),
        ).fetchone()
        original = _ids(get_db(), route_id)
        gate = (package / "checkpoints" / "gate.md").read_text(encoding="utf-8")
        (package / "checkpoints" / "gate.md").write_text(
            gate.replace("A short fictional reveal.", "A revised fictional reveal."),
            encoding="utf-8",
        )
        chapters = json.loads((package / "chapters.json").read_text(encoding="utf-8"))
        chapters["chapters"][0]["title"] = "The revised approach"
        (package / "chapters.json").write_text(json.dumps(chapters, indent=2) + "\n", encoding="utf-8")
        plan = import_route(get_db(), package)
        assert "update chapter approach (title)" in format_plan(plan)
        assert "update story gate (arrival_body)" in format_plan(plan)
        assert _ids(get_db(), route_id) == original
        after = get_db().execute(
            """
            SELECT journey.progress_version, journey.status, totals.credited_total
            FROM route_journeys AS journey
            JOIN journey_metric_totals AS totals ON totals.journey_id = journey.id
            WHERE journey.id = ?
            """,
            (journey["id"],),
        ).fetchone()
        assert after == before
        assert [
            row["position"]
            for row in get_db()
            .execute(
                "SELECT position FROM route_checkpoints WHERE route_id = ? ORDER BY position",
                (route_id,),
            )
            .fetchall()
        ] == positions
        assert (
            get_db()
            .execute(
                """
                SELECT requirement.threshold_amount
                FROM checkpoint_requirements AS requirement
                JOIN route_checkpoints AS checkpoint ON checkpoint.id = requirement.checkpoint_id
                WHERE checkpoint.route_id = ? AND checkpoint.slug = 'gate'
                """,
                (route_id,),
            )
            .fetchone()["threshold_amount"]
            == threshold
        )
        story = _story_response(client, token, journey["id"], "gate").get_json()
        assert story["arrival_body"] == "A revised fictional reveal."
        assert story["acknowledged"] is True
        repeat = client.post(
            f"/api/v1/journeys/{journey['id']}/journal/gate/acknowledgment",
            headers=_auth(token),
        )
        assert repeat.get_json()["acknowledged_at"] == seen_at


def test_omitted_records_are_left_in_place(app, tmp_path):
    slug = "content-omit"
    with app.app_context():
        route_id = _geography(slug, ["gate", "camp", "summit"])
        import_route(get_db(), _valid(tmp_path, slug))
        original = _ids(get_db(), route_id)
        import_route(get_db(), _valid(tmp_path, slug, include_camp=False, include_lichen=False))
        kept = _ids(get_db(), route_id)
        assert kept["chapters"] == original["chapters"]
        assert kept["stories"] == original["stories"]
        assert kept["discoveries"] == original["discoveries"]
        assert (
            get_db()
            .execute(
                """
                SELECT chapter.slug
                FROM route_checkpoints AS checkpoint
                JOIN route_chapters AS chapter ON chapter.id = checkpoint.chapter_id
                WHERE checkpoint.route_id = ? AND checkpoint.slug = 'camp'
                """,
                (route_id,),
            )
            .fetchone()["slug"]
            == "approach"
        )


def test_retirement_keeps_the_row_and_the_acknowledgment(client, app, tmp_path):
    slug = "content-retire"
    with app.app_context():
        route_id = _geography(slug, ["gate", "summit"])
        package = _write(
            tmp_path,
            slug,
            chapters=[{"slug": "approach", "title": "Approach", "position": 0, "status": "published"}],
            checkpoints=[
                _story(
                    "gate",
                    status="published",
                    chapter="approach",
                    title="Gate",
                    body="A reveal.",
                    journal=True,
                )
            ],
            media=[_cc("gate")],
        )
        import_route(get_db(), package)
        story_id = _ids(get_db(), route_id)["stories"]["gate"]
        _user, token, _csrf = _register(client, "ContentRetire")
        journey = _start(client, token, slug)
        _credit(client, token, elevation_ft=100)
        acknowledged = client.post(
            f"/api/v1/journeys/{journey['id']}/journal/gate/acknowledgment",
            headers=_auth(token),
        )
        seen_at = acknowledged.get_json()["acknowledged_at"]
        text = (package / "checkpoints" / "gate.md").read_text(encoding="utf-8")
        (package / "checkpoints" / "gate.md").write_text(
            text.replace('"status": "published"', '"status": "retired"', 1),
            encoding="utf-8",
        )
        plan = import_route(get_db(), package)
        assert "retire story gate (status)" in format_plan(plan)
        assert _ids(get_db(), route_id)["stories"]["gate"] == story_id
        assert (
            get_db()
            .execute("SELECT status FROM checkpoint_stories WHERE id = ?", (story_id,))
            .fetchone()["status"]
            == "retired"
        )
        journal = client.get(
            f"/api/v1/journeys/{journey['id']}/journal",
            headers=_auth(token),
        ).get_json()
        assert journal["chapters"] == []
        assert _story_response(client, token, journey["id"], "gate").status_code == 404
        view = get_db().execute(
            """
            SELECT acknowledged_at
            FROM journey_story_views
            WHERE journey_id = ?
            """,
            (journey["id"],),
        ).fetchone()
        assert view is not None
        repeat = client.post(
            f"/api/v1/journeys/{journey['id']}/journal/gate/acknowledgment",
            headers=_auth(token),
        )
        assert repeat.status_code == 404
        assert view["acknowledged_at"].isoformat() == seen_at


def test_position_swap_keeps_chapter_and_discovery_ids(app, tmp_path):
    slug = "content-swap"
    with app.app_context():
        route_id = _geography(slug, ["gate", "summit"])
        discoveries = [
            _discovery("old-post", "A fictional post.", position=0),
            _discovery("blue-lichen", "A draft lichen note.", position=1, status="draft"),
        ]
        package = _write(
            tmp_path,
            slug,
            chapters=[
                {"slug": "approach", "title": "Approach", "position": 0, "status": "published"},
                {"slug": "notes", "title": "Notes", "position": 1, "status": "draft"},
            ],
            checkpoints=[
                _story(
                    "gate",
                    status="published",
                    chapter="approach",
                    title="Gate",
                    body="A reveal.",
                    discoveries=discoveries,
                )
            ],
            media=[],
        )
        import_route(get_db(), package)
        original = _ids(get_db(), route_id)
        package = _write(
            tmp_path,
            slug,
            chapters=[
                {"slug": "approach", "title": "Approach", "position": 1, "status": "published"},
                {"slug": "notes", "title": "Notes", "position": 0, "status": "draft"},
            ],
            checkpoints=[
                _story(
                    "gate",
                    status="published",
                    chapter="approach",
                    title="Gate",
                    body="A reveal.",
                    discoveries=[
                        _discovery("old-post", "A fictional post.", position=1),
                        _discovery("blue-lichen", "A draft lichen note.", position=0, status="draft"),
                    ],
                )
            ],
            media=[],
        )
        plan = import_route(get_db(), package)
        rendered = format_plan(plan)
        assert "update chapter approach (position)" in rendered
        assert "update chapter notes (position)" in rendered
        assert "update discovery gate/old-post (position)" in rendered
        assert "update discovery gate/blue-lichen (position)" in rendered
        swapped = _ids(get_db(), route_id)
        assert swapped["chapters"] == original["chapters"]
        assert swapped["discoveries"] == original["discoveries"]
        positions = {
            row["slug"]: row["position"]
            for row in get_db()
            .execute("SELECT slug, position FROM route_chapters WHERE route_id = ?", (route_id,))
            .fetchall()
        }
        assert positions == {"approach": 1, "notes": 0}


def test_failed_import_rolls_the_route_back(client, app, tmp_path, monkeypatch):
    slug = "content-rollback"
    with app.app_context():
        route_id = _geography(slug, ["gate", "summit"])
        package = _write(
            tmp_path,
            slug,
            chapters=[{"slug": "approach", "title": "Approach", "position": 0, "status": "published"}],
            checkpoints=[
                _story("gate", status="published", chapter="approach", title="Gate", body="A reveal.")
            ],
            media=[],
        )
        import_route(get_db(), package)
        _user, token, _csrf = _register(client, "ContentRollback")
        journey = _start(client, token, slug)
        _credit(client, token, elevation_ft=100)
        client.post(
            f"/api/v1/journeys/{journey['id']}/journal/gate/acknowledgment",
            headers=_auth(token),
        )
        database = get_db()
        before = database.execute(
            """
            SELECT journey.progress_version, totals.credited_total, story.arrival_body,
                   chapter.title, seen.acknowledged_at
            FROM route_journeys AS journey
            JOIN journey_metric_totals AS totals ON totals.journey_id = journey.id
            JOIN checkpoint_stories AS story ON story.route_id = journey.route_id
            JOIN route_chapters AS chapter ON chapter.route_id = journey.route_id
            JOIN journey_story_views AS seen ON seen.journey_id = journey.id
            WHERE journey.id = ?
            """,
            (journey["id"],),
        ).fetchone()
        chapters = json.loads((package / "chapters.json").read_text(encoding="utf-8"))
        chapters["chapters"][0]["title"] = "Changed"
        (package / "chapters.json").write_text(json.dumps(chapters, indent=2) + "\n", encoding="utf-8")
        text = (package / "checkpoints" / "gate.md").read_text(encoding="utf-8")
        (package / "checkpoints" / "gate.md").write_text(
            text.replace("A reveal.", "A changed reveal."),
            encoding="utf-8",
        )
        original = database.execute

        def fail_story_update(sql, params=()):
            compact = " ".join(sql.split()).lower()
            if compact.startswith("update checkpoint_stories"):
                raise RuntimeError("forced failure")
            return original(sql, params)

        monkeypatch.setattr(database, "execute", fail_story_update)
        with pytest.raises(RuntimeError, match="forced failure"):
            import_route(database, package)
        after = original(
            """
            SELECT journey.progress_version, totals.credited_total, story.arrival_body,
                   chapter.title, seen.acknowledged_at
            FROM route_journeys AS journey
            JOIN journey_metric_totals AS totals ON totals.journey_id = journey.id
            JOIN checkpoint_stories AS story ON story.route_id = journey.route_id
            JOIN route_chapters AS chapter ON chapter.route_id = journey.route_id
            JOIN journey_story_views AS seen ON seen.journey_id = journey.id
            WHERE journey.id = ?
            """,
            (journey["id"],),
        ).fetchone()
        assert after == before
        assert (
            original(
                "SELECT COUNT(*) AS n FROM route_chapters WHERE route_id = ?",
                (route_id,),
            ).fetchone()["n"]
            == 1
        )


def test_missing_files_and_invalid_markdown_name_the_field(tmp_path):
    slug = "content-broken"
    package = _valid(tmp_path, slug)
    (package / "chapters.json").unlink()
    issues = _issues(package)
    assert _matching(issues, "chapters.json", "path", "required file is missing")

    package = _valid(tmp_path, slug)
    (package / "checkpoints" / "gate.md").write_text(
        "---\n" + json.dumps({"status": "published", "chapter": "approach", "arrival_title": "Gate"}) + "\n---\n\nHello.\n",
        encoding="utf-8",
    )
    issues = _issues(package)
    assert _matching(issues, "checkpoints/gate.md", "arrival_body", "Arrival section is required")
    assert _matching(issues, "checkpoints/gate.md", "body", "prose must sit under")

    (package / "checkpoints" / "gate.md").write_text(
        "---\n"
        + json.dumps(
            {
                "status": "published",
                "chapter": "approach",
                "arrival_title": "Gate",
                "journal_title": "Note",
            }
        )
        + "\n---\n\n# Arrival\n\nA reveal.\n",
        encoding="utf-8",
    )
    issues = _issues(package)
    assert _matching(issues, "checkpoints/gate.md", "journal_body", "Journal section is required")


def test_duplicate_editorial_identities_are_rejected(tmp_path):
    slug = "content-duplicate"
    package = _write(
        tmp_path,
        slug,
        chapters=[
            {"slug": "approach", "title": "Approach", "position": 0, "status": "draft"},
            {"slug": "approach", "title": "Again", "position": 1, "status": "draft"},
        ],
        checkpoints=[
            _story(
                "gate",
                status="draft",
                chapter="approach",
                title="Gate",
                body="A reveal.",
                discoveries=[
                    _discovery("old-post", "One."),
                    _discovery("old-post", "Two.", position=1),
                ],
            )
        ],
        media=[_draft_scene("gate"), _draft_scene("gate")],
    )
    issues = _issues(package)
    assert _matching(issues, "chapters.json", "slug", "duplicate chapter slug")
    assert _matching(issues, "checkpoints/gate.md", "slug", "duplicate discovery slug")
    assert _matching(issues, "media.json", "position", "duplicate media identity")


def test_unknown_route_and_checkpoint_fail_before_writing(app, tmp_path):
    with app.app_context():
        missing = _write(
            tmp_path,
            "content-missing-route",
            chapters=[{"slug": "approach", "title": "Approach", "position": 0, "status": "draft"}],
            checkpoints=[
                _story("gate", status="draft", chapter="approach", title="Gate", body="A reveal.")
            ],
            media=[],
        )
        with pytest.raises(ContentRejected) as caught:
            import_route(get_db(), missing)
        assert "route does not exist" in str(caught.value)
        assert (
            get_db()
            .execute("SELECT id FROM routes WHERE slug = 'content-missing-route'")
            .fetchone()
            is None
        )

        slug = "content-missing-stop"
        route_id = _geography(slug, ["gate", "summit"])
        package = _write(
            tmp_path,
            slug,
            chapters=[{"slug": "approach", "title": "Approach", "position": 0, "status": "published"}],
            checkpoints=[
                _story("gate", status="published", chapter="approach", title="Gate", body="A reveal."),
                _story("extra", status="published", chapter="approach", title="Extra", body="Not on the route."),
            ],
            media=[],
        )
        issues = _issues(package, get_db())
        assert _matching(issues, "checkpoints/extra.md", "slug", "checkpoint is not on this route")
        with pytest.raises(ContentRejected):
            import_route(get_db(), package)
        assert (
            get_db()
            .execute("SELECT COUNT(*) AS n FROM route_chapters WHERE route_id = ?", (route_id,))
            .fetchone()["n"]
            == 0
        )


def test_chapter_order_and_spans_fail_without_writing(app, tmp_path):
    slug = "content-span"
    with app.app_context():
        route_id = _geography(slug, ["gate", "camp", "summit"])
        gap = _write(
            tmp_path,
            slug,
            chapters=[{"slug": "approach", "title": "Approach", "position": 0, "status": "published"}],
            checkpoints=[
                _story("gate", status="published", chapter="approach", title="Gate", body="A reveal."),
                _story("summit", status="draft", chapter="approach", title="Summit", body="A later reveal."),
            ],
            media=[],
        )
        issues = _issues(gap, get_db())
        assert _matching(issues, "chapters.json", "chapter", "contiguous span")
        assert (
            get_db()
            .execute("SELECT COUNT(*) AS n FROM route_chapters WHERE route_id = ?", (route_id,))
            .fetchone()["n"]
            == 0
        )

        crossed = _write(
            tmp_path,
            slug,
            chapters=[
                {"slug": "approach", "title": "Approach", "position": 0, "status": "published"},
                {"slug": "ridge", "title": "Ridge", "position": 1, "status": "published"},
            ],
            checkpoints=[
                _story("gate", status="published", chapter="ridge", title="Gate", body="A reveal."),
                _story("summit", status="draft", chapter="approach", title="Summit", body="A later reveal."),
            ],
            media=[],
        )
        issues = _issues(crossed, get_db())
        assert _matching(issues, "chapters.json", "position", "chapter order must follow checkpoint position")
        with pytest.raises(ContentRejected):
            import_route(get_db(), crossed)
        assert (
            get_db()
            .execute("SELECT COUNT(*) AS n FROM route_chapters WHERE route_id = ?", (route_id,))
            .fetchone()["n"]
            == 0
        )


def test_incomplete_rights_and_bad_paths_are_rejected(tmp_path):
    slug = "content-rights"
    package = _write(
        tmp_path,
        slug,
        chapters=[{"slug": "approach", "title": "Approach", "position": 0, "status": "draft"}],
        checkpoints=[
            _story("gate", status="draft", chapter="approach", title="Gate", body="A reveal.")
        ],
        media=[
            _cc("gate", author=None),
            {
                "role": "journal",
                "checkpoint": "gate",
                "path": PHOTO,
                "alt_text": "A noncommercial stand-in.",
                "rights_basis": "creative_commons",
                "author": "Sample Author",
                "license": "CC BY-NC 4.0",
                "license_url": "https://creativecommons.org/licenses/by-nc/4.0/",
                "source_url": "https://example.com/content/nc",
                "position": 2,
                "status": "published",
            },
            {
                "role": "journal",
                "checkpoint": "gate",
                "path": "/static/photos/does-not-exist.jpg",
                "alt_text": "A missing file.",
                "rights_basis": "undocumented",
                "position": 3,
                "status": "draft",
            },
            {
                "role": "journal",
                "checkpoint": "gate",
                "path": "https://example.com/photo.jpg",
                "alt_text": "A remote file.",
                "rights_basis": "undocumented",
                "position": 4,
                "status": "draft",
            },
            {
                "role": "checkpoint_scene",
                "checkpoint": "gate",
                "path": "/static/css/site.css",
                "alt_text": "Not a curated photograph.",
                "rights_basis": "undocumented",
                "position": 0,
                "status": "draft",
            },
        ],
    )
    issues = _issues(package)
    assert _matching(issues, "media.json", "rights_basis", "not proof the license is authentic")
    assert sum("not proof the license is authentic" in issue.message for issue in issues) >= 2
    assert _matching(issues, "media.json", "path", "file is missing")
    assert _matching(issues, "media.json", "path", "curated file under /static/photos/")
    draft = _write(
        tmp_path,
        slug,
        chapters=[{"slug": "approach", "title": "Approach", "position": 0, "status": "draft"}],
        checkpoints=[
            _story("gate", status="draft", chapter="approach", title="Gate", body="A reveal.")
        ],
        media=[_draft_scene("gate")],
    )
    assert _issues(draft) == []
