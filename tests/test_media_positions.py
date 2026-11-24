"""Journal and discovery position conflicts during content import."""

from __future__ import annotations

import pytest

import adventurer.content_pipeline as pipeline
from adventurer.content_pipeline import ContentRejected, import_route, validate_content
from adventurer.db import get_db
from test_content_pipeline import (
    _cc,
    _discovery,
    _draft_scene,
    _geography,
    _public_domain,
    _story,
    _write,
)
from test_narrative_api import _credit, _register, _start


def _package(root, slug, *, title="Approach", checkpoints=None, media=None):
    return _write(
        root,
        slug,
        chapters=[{"slug": "approach", "title": title, "position": 0, "status": "published"}],
        checkpoints=checkpoints
        or [_story("gate", status="published", chapter="approach", title="Gate", body="A reveal.")],
        media=media if media is not None else [],
    )


def _rows(database, route_id):
    return database.execute(
        """
        SELECT id, role, position, status, alt_text
        FROM route_media
        WHERE route_id = ?
        ORDER BY id
        """,
        (route_id,),
    ).fetchall()


def _title(database, route_id):
    return database.execute(
        "SELECT title FROM route_chapters WHERE route_id = ? AND slug = 'approach'",
        (route_id,),
    ).fetchone()["title"]


def _hide_identity(monkeypatch, *, role: str) -> None:
    """Make an existing photograph look like a new insert at the same index slot.

    The stored row still occupies the partial unique index. Clearing the slug
    that forms its editorial identity stops the package from matching it, which
    is the apply-time collision this preflight has to reject first.
    """
    real = pipeline._read_catalog

    def wrapped(database, package):
        catalog, issues = real(database, package)
        if catalog is not None:
            for row in catalog.media:
                if row["role"] != role:
                    continue
                if role == "discovery":
                    row["discovery_slug"] = None
                else:
                    row["checkpoint_slug"] = None
        return catalog, issues

    monkeypatch.setattr(pipeline, "_read_catalog", wrapped)


def _rejection(package):
    issues = validate_content(package, get_db())
    with pytest.raises(ContentRejected) as dry:
        import_route(get_db(), package, dry_run=True)
    with pytest.raises(ContentRejected) as live:
        import_route(get_db(), package)
    rendered = [str(issue) for issue in issues]
    assert rendered
    assert rendered == [str(issue) for issue in dry.value.issues]
    assert rendered == [str(issue) for issue in live.value.issues]
    return rendered


def test_omitted_published_journal_position_is_rejected(client, app, tmp_path, monkeypatch):
    slug = "content-journal-published"
    with app.app_context():
        route_id = _geography(slug, ["gate"])
        import_route(get_db(), _package(tmp_path, slug, media=[_cc("gate", position=0)]))
        _user, token, _csrf = _register(client, "JournalHold")
        journey = _start(client, token, slug)
        _credit(client, token, elevation_ft=100)
        before_progress = get_db().execute(
            """
            SELECT journey.progress_version, totals.credited_total
            FROM route_journeys AS journey
            JOIN journey_metric_totals AS totals ON totals.journey_id = journey.id
            WHERE journey.id = ?
            """,
            (journey["id"],),
        ).fetchone()
        before_media = _rows(get_db(), route_id)
        replacement = _package(
            tmp_path,
            slug,
            title="Changed",
            media=[_cc("gate", position=0)],
        )
        _hide_identity(monkeypatch, role="journal")
        rendered = _rejection(replacement)
        assert any(
            "position 0 is still used by an omitted published journal" in issue
            and f"route {slug}" in issue
            and "checkpoint gate" in issue
            and "role journal" in issue
            for issue in rendered
        )
        assert _title(get_db(), route_id) == "Approach"
        assert _rows(get_db(), route_id) == before_media
        after_progress = get_db().execute(
            """
            SELECT journey.progress_version, totals.credited_total
            FROM route_journeys AS journey
            JOIN journey_metric_totals AS totals ON totals.journey_id = journey.id
            WHERE journey.id = ?
            """,
            (journey["id"],),
        ).fetchone()
        assert after_progress == before_progress


def test_omitted_draft_journal_position_is_rejected(app, tmp_path, monkeypatch):
    slug = "content-journal-draft"
    with app.app_context():
        route_id = _geography(slug, ["gate"])
        import_route(
            get_db(),
            _package(tmp_path, slug, media=[_cc("gate", position=0, status="draft")]),
        )
        before = _rows(get_db(), route_id)
        _hide_identity(monkeypatch, role="journal")
        rendered = _rejection(_package(tmp_path, slug, title="Changed", media=[_cc("gate")]))
        assert any("an omitted draft journal" in issue and f"route {slug}" in issue for issue in rendered)
        assert _title(get_db(), route_id) == "Approach"
        assert _rows(get_db(), route_id) == before


def test_omitted_discovery_media_position_is_rejected(app, tmp_path, monkeypatch):
    slug = "content-discovery-hold"
    discoveries = [_discovery("old-post", "A fictional post.")]
    media = [_public_domain("gate", "old-post")]
    checkpoints = [
        _story(
            "gate",
            status="published",
            chapter="approach",
            title="Gate",
            body="A reveal.",
            discoveries=discoveries,
        )
    ]
    with app.app_context():
        route_id = _geography(slug, ["gate"])
        import_route(get_db(), _package(tmp_path, slug, checkpoints=checkpoints, media=media))
        before = _rows(get_db(), route_id)
        _hide_identity(monkeypatch, role="discovery")
        rendered = _rejection(
            _package(tmp_path, slug, title="Changed", checkpoints=checkpoints, media=media)
        )
        assert any(
            "an omitted published discovery" in issue
            and f"route {slug}" in issue
            and "checkpoint gate" in issue
            and "discovery old-post" in issue
            and "role discovery" in issue
            for issue in rendered
        )
        assert _title(get_db(), route_id) == "Approach"
        assert _rows(get_db(), route_id) == before


def test_retired_media_position_is_reused_by_its_identity(app, tmp_path):
    slug = "content-media-reuse"
    with app.app_context():
        route_id = _geography(slug, ["gate"])
        published = _cc("gate", position=0)
        import_route(get_db(), _package(tmp_path, slug, media=[published]))
        original = _rows(get_db(), route_id)[0]
        retired = dict(published, status="retired", rights_basis="undocumented")
        retired.pop("author", None)
        retired.pop("license", None)
        retired.pop("license_url", None)
        retired.pop("source_url", None)
        import_route(get_db(), _package(tmp_path, slug, media=[retired]))
        held = _rows(get_db(), route_id)
        assert held == [{**original, "status": "retired", "alt_text": original["alt_text"]}]
        assert held[0]["id"] == original["id"]
        revived = dict(published, alt_text="A revised stand-in photograph.")
        import_route(get_db(), _package(tmp_path, slug, media=[revived]))
        again = _rows(get_db(), route_id)
        assert len(again) == 1
        assert again[0]["id"] == original["id"]
        assert again[0]["status"] == "published"
        assert again[0]["position"] == 0
        assert again[0]["alt_text"] == "A revised stand-in photograph."
        import_route(
            get_db(),
            _package(tmp_path, slug, media=[revived, _cc("gate", position=2, status="draft")]),
        )
        added = _rows(get_db(), route_id)
        assert [row["id"] for row in added if row["position"] == 0] == [original["id"]]
        assert [(row["position"], row["status"]) for row in added] == [(0, "published"), (2, "draft")]


def test_omitted_journal_is_left_in_place(app, tmp_path):
    slug = "content-journal-omit"
    with app.app_context():
        route_id = _geography(slug, ["gate"])
        import_route(get_db(), _package(tmp_path, slug, media=[_cc("gate", position=0)]))
        original = _rows(get_db(), route_id)[0]
        import_route(get_db(), _package(tmp_path, slug, media=[_cc("gate", position=1, status="draft")]))
        rows = _rows(get_db(), route_id)
        assert rows[0] == original
        assert rows[1]["position"] == 1
        assert rows[1]["status"] == "draft"


def test_media_positions_are_independent_and_imports_are_idempotent(app, tmp_path):
    slug = "content-media-namespaces"
    lichen = _public_domain("gate", "blue-lichen")
    checkpoints = [
        _story(
            "gate",
            status="published",
            chapter="approach",
            title="Gate",
            body="A reveal.",
            discoveries=[
                _discovery("old-post", "A fictional post.", position=0),
                _discovery("blue-lichen", "A lichen note.", position=1),
            ],
        ),
        _story("camp", status="draft", chapter="approach", title="Camp", body="A later note."),
    ]
    media = [
        _cc("gate", position=0),
        _cc("camp", position=0),
        _public_domain("gate", "old-post"),
        lichen,
        _draft_scene("gate"),
    ]
    with app.app_context():
        route_id = _geography(slug, ["gate", "camp"])
        import_route(get_db(), _package(tmp_path, slug, checkpoints=checkpoints, media=media))
        first = _rows(get_db(), route_id)
        assert sorted((row["role"], row["position"]) for row in first) == sorted(
            [
                ("journal", 0),
                ("journal", 0),
                ("discovery", 0),
                ("discovery", 0),
                ("checkpoint_scene", 0),
            ]
        )
        again = import_route(get_db(), _package(tmp_path, slug, checkpoints=checkpoints, media=media))
        assert [change.action for change in again if change.kind == "media"] == ["unchanged"] * 5
        assert _rows(get_db(), route_id) == first
