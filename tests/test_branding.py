"""Organization and expedition branding resolution, edits, and read surfaces."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from adventurer.branding import (
    DEFAULT_ACCENT,
    DEFAULT_PRIMARY,
    BrandingError,
    normalize_color,
    normalize_colors,
    normalize_welcome,
    resolve_branding,
)
from adventurer.db import DB, get_db
from adventurer.home import compose_home
from adventurer.measures import feet_to_meters

DAY = "2026-06-01"
COVER = "/branding/expeditions/legacy/cover/old.png"


def test_resolution_inherits_invalid_legacy_values_without_rewriting_them():
    defaults = resolve_branding(
        kind="global",
        expedition={
            "accent_color": "#112233",
            "image_ref": COVER,
            "welcome_override": "Ignored",
        },
    )
    assert defaults == {
        "primary_color": DEFAULT_PRIMARY,
        "accent_color": DEFAULT_ACCENT,
        "logo_ref": None,
        "image_ref": None,
        "welcome_message": None,
    }

    inherited = resolve_branding(
        kind="organization",
        organization={
            "colors": {"primary": "green", "accent": "#445566", "extra": 1},
            "logo_ref": "  /logo.png  ",
            "image_ref": "   ",
            "welcome_message": "  Hello  ",
        },
        expedition={
            "accent_color": "blue",
            "image_ref": None,
            "welcome_override": "   ",
        },
    )
    assert inherited["primary_color"] == DEFAULT_PRIMARY
    assert inherited["accent_color"] == "#445566"
    assert inherited["logo_ref"] == "/logo.png"
    assert inherited["image_ref"] is None
    assert inherited["welcome_message"] == "Hello"

    overridden = resolve_branding(
        kind="organization",
        organization={
            "colors": {"primary": "#111111", "accent": "#222222"},
            "logo_ref": "/logo.png",
            "image_ref": "/org-cover.png",
            "welcome_message": "Org",
        },
        expedition={
            "accent_color": "#abcdef",
            "image_ref": COVER,
            "welcome_override": "Season",
        },
    )
    assert overridden == {
        "primary_color": "#111111",
        "accent_color": "#ABCDEF",
        "logo_ref": "/logo.png",
        "image_ref": COVER,
        "welcome_message": "Season",
    }

    private = resolve_branding(
        kind="private",
        organization={
            "colors": {"accent": "#111111"},
            "logo_ref": "/logo.png",
            "welcome_message": "Org",
        },
        expedition={"accent_color": None, "welcome_override": "", "image_ref": ""},
    )
    assert private["primary_color"] == DEFAULT_PRIMARY
    assert private["accent_color"] == DEFAULT_ACCENT
    assert private["logo_ref"] is None
    assert private["welcome_message"] is None


def test_color_and_welcome_normalization():
    assert normalize_color("#aabbcc") == "#AABBCC"
    assert normalize_colors({"accent": "#aabbcc"}) == {"accent": "#AABBCC"}
    assert normalize_colors({}) == {}
    assert normalize_colors(None) is None
    assert normalize_colors({"primary": None, "accent": "#112233"}) == {"accent": "#112233"}
    assert normalize_welcome("  hi  ") == "hi"
    assert normalize_welcome("   ") is None
    assert normalize_welcome(None) is None
    assert normalize_welcome("x" * 280) == "x" * 280
    for value in ("#abc", "#aabbccdd", "rgb(0,0,0)", "green", " #112233", 123, True):
        with pytest.raises(BrandingError):
            normalize_color(value)
    with pytest.raises(BrandingError):
        normalize_colors({"primary": "#112233", "theme": "dark"})
    with pytest.raises(BrandingError):
        normalize_colors(["#112233"])
    with pytest.raises(BrandingError):
        normalize_colors({"primary": 1})
    with pytest.raises(BrandingError):
        normalize_welcome("x" * 281)
    with pytest.raises(BrandingError):
        normalize_welcome(12)


def test_branding_edits_authorization_and_reads(client, app):
    _ada, ada, _csrf = _register(client, "Ada")
    _owen, owen, _csrf = _register(client, "Owen")
    _sam, sam, _csrf = _register(client, "Sam")
    _lee, lee, _csrf = _register(client, "Lee")
    app.config["PLATFORM_ADMIN_IDS"] = {_ada}
    with app.app_context():
        _route("brand-ridge")
        _route("brand-other")
    _org(client, ada, "ridge-club", "Owen")
    _org(client, ada, "other-club", "Lee")

    before = _count(app, "expeditions")
    rejected = _season(
        client,
        owen,
        "ridge-club",
        "bad-season",
        "brand-ridge",
        accent_color="green",
    )
    assert rejected.status_code == 400
    assert rejected.get_json()["error"] == "Use a color like #18354C."
    assert _count(app, "expeditions") == before
    rejected_cover = _season(
        client,
        owen,
        "ridge-club",
        "bad-cover",
        "brand-ridge",
        image_ref=COVER,
    )
    assert rejected_cover.status_code == 400
    assert "cover" in rejected_cover.get_json()["error"]
    assert _count(app, "expeditions") == before
    rejected_welcome = _season(
        client,
        owen,
        "ridge-club",
        "bad-welcome",
        "brand-ridge",
        welcome_override="x" * 281,
    )
    assert rejected_welcome.status_code == 400
    assert _count(app, "expeditions") == before

    created = _season(
        client,
        owen,
        "ridge-club",
        "ridge-season",
        "brand-ridge",
        accent_color="#aabbcc",
        welcome_override="  Hello ridge  ",
    )
    assert created.status_code == 201, created.get_json()
    expedition = created.get_json()["expedition"]
    assert expedition["accent_color"] == "#AABBCC"
    assert expedition["welcome_override"] == "Hello ridge"
    assert expedition["image_ref"] is None
    assert expedition["branding"]["accent_color"] == "#AABBCC"
    assert expedition["branding"]["primary_color"] == DEFAULT_PRIMARY
    assert expedition["branding"]["welcome_message"] == "Hello ridge"

    themed = client.patch(
        "/api/v1/organizations/ridge-club/branding",
        json={
            "colors": {"primary": "#111111", "accent": "#222222"},
            "welcome_message": "  Club welcome  ",
        },
        headers=_auth(owen),
    )
    assert themed.status_code == 200, themed.get_json()
    organization = themed.get_json()["organization"]
    assert organization["colors"] == {"primary": "#111111", "accent": "#222222"}
    assert organization["welcome_message"] == "Club welcome"
    assert organization["branding"]["primary_color"] == "#111111"
    assert organization["branding"]["welcome_message"] == "Club welcome"
    assert "role" in organization

    listed = client.get("/api/v1/organizations", headers=_auth(owen))
    assert listed.status_code == 200
    listed_org = next(row for row in listed.get_json()["organizations"] if row["slug"] == "ridge-club")
    assert listed_org["colors"]["primary"] == "#111111"
    assert listed_org["branding"]["accent_color"] == "#222222"

    detail = client.get("/api/v1/organizations/ridge-club", headers=_auth(owen))
    assert detail.get_json()["organization"]["branding"]["logo_ref"] is None
    assert detail.get_json()["organization"]["welcome_message"] == "Club welcome"

    still = client.get("/api/v1/expeditions/ridge-season", headers=_auth(sam))
    assert still.status_code == 200, still.get_json()
    profile = still.get_json()["expedition"]
    assert profile["accent_color"] == "#AABBCC"
    assert profile["organization_id"]
    assert profile["branding"]["primary_color"] == "#111111"
    assert profile["branding"]["accent_color"] == "#AABBCC"
    assert profile["branding"]["welcome_message"] == "Hello ridge"

    partial = client.patch(
        "/api/v1/organizations/ridge-club/branding",
        json={"colors": {"accent": "#333333"}},
        headers=_auth(owen),
    )
    assert partial.status_code == 200, partial.get_json()
    stored = partial.get_json()["organization"]
    assert stored["colors"] == {"accent": "#333333"}
    assert stored["welcome_message"] == "Club welcome"
    assert stored["branding"]["primary_color"] == DEFAULT_PRIMARY
    assert stored["branding"]["accent_color"] == "#333333"

    cleared = client.patch(
        "/api/v1/expeditions/ridge-season/branding",
        json={"accent_color": None, "welcome_override": "   "},
        headers=_auth(owen),
    )
    assert cleared.status_code == 200, cleared.get_json()
    cleared_body = cleared.get_json()["expedition"]
    assert cleared_body["accent_color"] is None
    assert cleared_body["welcome_override"] is None
    assert cleared_body["branding"]["accent_color"] == "#333333"
    assert cleared_body["branding"]["welcome_message"] == "Club welcome"

    kept = client.patch(
        "/api/v1/expeditions/ridge-season/branding",
        json={"welcome_override": "Season note"},
        headers=_auth(owen),
    )
    assert kept.get_json()["expedition"]["accent_color"] is None
    assert kept.get_json()["expedition"]["welcome_override"] == "Season note"
    assert kept.get_json()["expedition"]["branding"]["accent_color"] == "#333333"

    empty_colors = client.patch(
        "/api/v1/organizations/ridge-club/branding",
        json={"colors": {}},
        headers=_auth(owen),
    )
    assert empty_colors.get_json()["organization"]["colors"] == {}
    assert empty_colors.get_json()["organization"]["branding"]["primary_color"] == DEFAULT_PRIMARY
    assert empty_colors.get_json()["organization"]["branding"]["accent_color"] == DEFAULT_ACCENT
    none_colors = client.patch(
        "/api/v1/organizations/ridge-club/branding",
        json={"colors": None, "welcome_message": None},
        headers=_auth(owen),
    )
    assert none_colors.get_json()["organization"]["colors"] is None
    assert none_colors.get_json()["organization"]["welcome_message"] == ""
    assert none_colors.get_json()["organization"]["branding"]["welcome_message"] is None

    restored = client.patch(
        "/api/v1/organizations/ridge-club/branding",
        json={"colors": {"primary": "#111111", "accent": "#222222"}, "welcome_message": "Club welcome"},
        headers=_auth(owen),
    )
    assert restored.status_code == 200, restored.get_json()

    unknown = client.patch(
        "/api/v1/organizations/ridge-club/branding",
        json={"theme": "dark"},
        headers=_auth(owen),
    )
    assert unknown.status_code == 400
    assert unknown.get_json()["error"] == "That branding field is not supported."
    bad_logo = client.patch(
        "/api/v1/organizations/ridge-club/branding",
        json={"logo_ref": COVER},
        headers=_auth(owen),
    )
    assert bad_logo.status_code == 400
    assert "logo" in bad_logo.get_json()["error"]

    added = client.post(
        "/api/v1/organizations/ridge-club/members",
        json={"username": "Sam", "role": "member"},
        headers=_auth(owen),
    )
    assert added.status_code == 201, added.get_json()
    member = client.patch(
        "/api/v1/organizations/ridge-club/branding",
        json={"welcome_message": "Nope"},
        headers=_auth(sam),
    )
    assert member.status_code == 403
    participant = client.patch(
        "/api/v1/expeditions/ridge-season/branding",
        json={"accent_color": "#445566"},
        headers=_auth(sam),
    )
    assert participant.status_code == 403
    assert participant.get_json()["error"] == "You cannot manage this expedition."
    stranger = client.patch(
        "/api/v1/organizations/ridge-club/branding",
        json={"welcome_message": "Nope"},
        headers=_auth(lee),
    )
    assert stranger.status_code == 404
    cross = client.patch(
        "/api/v1/expeditions/ridge-season/branding",
        json={"accent_color": "#445566"},
        headers=_auth(lee),
    )
    assert cross.status_code == 403

    invited = client.post(
        "/api/v1/expeditions/ridge-season/invitations",
        json={"username": "Lee"},
        headers=_auth(owen),
    )
    assert invited.status_code == 201, invited.get_json()
    assert invited.get_json()["branding"]["primary_color"] == "#111111"
    lee_invites = client.get("/api/v1/invitations", headers=_auth(lee))
    assert lee_invites.status_code == 200
    offer = lee_invites.get_json()["invitations"][0]
    assert offer["branding"]["primary_color"] == "#111111"
    assert "roster" not in offer
    lee_home = client.get("/api/v1/home", headers=_auth(lee)).get_json()
    incoming = lee_home["incoming_invitations"][0]
    assert incoming["branding"]["accent_color"] == "#222222"
    assert incoming["expedition"]["slug"] == "ridge-season"

    catalog = client.get("/api/v1/routes")
    assert catalog.status_code == 200
    assert all("branding" not in row for row in catalog.get_json()["routes"])
    enrollment = client.get("/api/v1/routes/brand-ridge")
    assert enrollment.status_code == 200
    listed_expedition = next(
        row for row in enrollment.get_json()["expeditions"] if row["slug"] == "ridge-season"
    )
    assert listed_expedition["branding"]["primary_color"] == "#111111"
    assert "organization" not in listed_expedition
    assert "colors" not in listed_expedition
    assert "roster" not in listed_expedition

    org_list = client.get("/api/v1/organizations/ridge-club/expeditions", headers=_auth(owen))
    assert org_list.status_code == 200
    assert org_list.get_json()["expeditions"][0]["branding"]["primary_color"] == "#111111"

    private = client.post(
        "/api/v1/expeditions",
        json={"slug": "owen-walk", "name": "Owen Walk", "route_slug": "brand-other"},
        headers=_auth(owen),
    )
    assert private.status_code == 201, private.get_json()
    assert private.get_json()["expedition"]["branding"]["accent_color"] == DEFAULT_ACCENT
    branded = client.patch(
        "/api/v1/expeditions/owen-walk/branding",
        json={"accent_color": "#abcdef", "welcome_override": "Private note"},
        headers=_auth(owen),
    )
    assert branded.status_code == 200, branded.get_json()
    assert branded.get_json()["expedition"]["accent_color"] == "#ABCDEF"
    assert branded.get_json()["expedition"]["branding"]["primary_color"] == DEFAULT_PRIMARY
    assert branded.get_json()["expedition"]["branding"]["logo_ref"] is None
    link = client.post(
        "/api/v1/expeditions/owen-walk/invitations",
        json={"username": "Sam"},
        headers=_auth(owen),
    )
    assert link.status_code == 201, link.get_json()
    joined = client.post(
        "/api/v1/invitations/accept",
        json={"token": link.get_json()["token"]},
        headers=_auth(sam),
    )
    assert joined.status_code == 200, joined.get_json()
    denied = client.patch(
        "/api/v1/expeditions/owen-walk/branding",
        json={"accent_color": "#111111"},
        headers=_auth(sam),
    )
    assert denied.status_code == 403
    admin_edit = client.patch(
        "/api/v1/expeditions/owen-walk/branding",
        json={"welcome_override": "From the platform"},
        headers=_auth(ada),
    )
    assert admin_edit.status_code == 200, admin_edit.get_json()
    assert admin_edit.get_json()["expedition"]["accent_color"] == "#ABCDEF"
    assert admin_edit.get_json()["expedition"]["welcome_override"] == "From the platform"
    home = client.get("/api/v1/home", headers=_auth(sam)).get_json()
    card = next(row for row in home["expeditions"] if row["slug"] == "owen-walk")
    assert card["accent_color"] == "#ABCDEF"
    assert card["welcome_override"] == "From the platform"
    assert card["branding"]["accent_color"] == "#ABCDEF"
    assert card["branding"]["primary_color"] == DEFAULT_PRIMARY
    assert "organization" not in card

    archived = client.post("/api/v1/expeditions/owen-walk/archive", headers=_auth(owen))
    assert archived.status_code == 200, archived.get_json()
    closed = client.patch(
        "/api/v1/expeditions/owen-walk/branding",
        json={"accent_color": None},
        headers=_auth(owen),
    )
    assert closed.status_code == 409
    assert closed.get_json()["error"] == "This expedition is not open."

    global_edit = client.patch(
        "/api/v1/expeditions/everest/branding",
        json={"accent_color": "#112233"},
        headers=_auth(ada),
    )
    assert global_edit.status_code == 403
    assert global_edit.get_json()["error"] == "This expedition keeps the product brand."

    suspended = client.post("/api/v1/admin/organizations/ridge-club/suspend", headers=_auth(ada))
    assert suspended.status_code == 200, suspended.get_json()
    owner_blocked = client.patch(
        "/api/v1/organizations/ridge-club/branding",
        json={"welcome_message": "After suspend"},
        headers=_auth(owen),
    )
    assert owner_blocked.status_code == 409
    platform = client.patch(
        "/api/v1/organizations/ridge-club/branding",
        json={"welcome_message": "Platform welcome"},
        headers=_auth(ada),
    )
    assert platform.status_code == 200, platform.get_json()
    assert platform.get_json()["organization"]["welcome_message"] == "Platform welcome"
    finished = client.patch(
        "/api/v1/expeditions/ridge-season/branding",
        json={"accent_color": "#445566"},
        headers=_auth(ada),
    )
    assert finished.status_code == 409


def test_legacy_rows_are_not_rewritten_on_read(client, app):
    _ada, ada, _csrf = _register(client, "Ada")
    _owen, owen, _csrf = _register(client, "Owen")
    app.config["PLATFORM_ADMIN_IDS"] = {_ada}
    with app.app_context():
        _route("legacy-ridge")
    _org(client, ada, "legacy-club", "Owen")
    created = _season(client, owen, "legacy-club", "legacy-season", "legacy-ridge")
    assert created.status_code == 201, created.get_json()
    with app.app_context():
        database = get_db()
        database.execute(
            """
            UPDATE organizations
            SET colors = '{"primary": "green", "extra": 1}'::jsonb,
                logo_ref = ?
            WHERE slug = 'legacy-club'
            """,
            (COVER,),
        )
        database.execute(
            """
            UPDATE expeditions
            SET accent_color = 'blue', image_ref = ?
            WHERE slug = 'legacy-season'
            """,
            (COVER,),
        )
        database.commit()
    seen = client.get("/api/v1/expeditions/legacy-season", headers=_auth(owen))
    brand = seen.get_json()["expedition"]["branding"]
    assert brand["primary_color"] == DEFAULT_PRIMARY
    assert brand["accent_color"] == DEFAULT_ACCENT
    assert brand["logo_ref"] == COVER
    assert brand["image_ref"] == COVER
    with app.app_context():
        database = get_db()
        colors = database.execute(
            "SELECT colors FROM organizations WHERE slug = 'legacy-club'"
        ).fetchone()["colors"]
        accent = database.execute(
            "SELECT accent_color FROM expeditions WHERE slug = 'legacy-season'"
        ).fetchone()["accent_color"]
        database.rollback()
    assert colors == {"primary": "green", "extra": 1}
    assert accent == "blue"

    cleared = client.patch(
        "/api/v1/organizations/legacy-club/branding",
        json={"logo_ref": None},
        headers=_auth(owen),
    )
    assert cleared.status_code == 200, cleared.get_json()
    assert cleared.get_json()["organization"]["logo_ref"] is None
    assert cleared.get_json()["organization"]["colors"] == {"primary": "green", "extra": 1}


def test_global_expedition_keeps_product_defaults(client, app):
    _ada, ada, _csrf = _register(client, "Ada")
    app.config["PLATFORM_ADMIN_IDS"] = {_ada}
    with app.app_context():
        database = get_db()
        database.execute(
            """
            UPDATE expeditions
            SET accent_color = '#112233', welcome_override = 'No', image_ref = ?
            WHERE slug = 'everest'
            """,
            (COVER,),
        )
        database.commit()
    seen = client.get("/api/v1/expeditions/everest", headers=_auth(ada))
    assert seen.status_code == 200, seen.get_json()
    brand = seen.get_json()["expedition"]["branding"]
    assert brand["primary_color"] == DEFAULT_PRIMARY
    assert brand["accent_color"] == DEFAULT_ACCENT
    assert brand["logo_ref"] is None
    assert brand["image_ref"] is None
    assert brand["welcome_message"] is None
    denied = client.patch(
        "/api/v1/expeditions/everest/branding",
        json={"accent_color": None},
        headers=_auth(ada),
    )
    assert denied.status_code == 403
    with app.app_context():
        database = get_db()
        row = database.execute(
            """
            SELECT accent_color, welcome_override, image_ref
            FROM expeditions WHERE slug = 'everest'
            """
        ).fetchone()
        database.rollback()
    assert row["accent_color"] == "#112233"
    assert row["welcome_override"] == "No"
    assert row["image_ref"] == COVER


def test_branding_preserves_progress_scene_and_snapshots(client, app):
    _ada, ada, _csrf = _register(client, "Ada")
    _owen, owen, _csrf = _register(client, "Owen")
    _sam, sam, _csrf = _register(client, "Sam")
    app.config["PLATFORM_ADMIN_IDS"] = {_ada}
    with app.app_context():
        route_id = _route("snapshot-ridge")
    _org(client, ada, "snapshot-club", "Owen")
    created = _season(client, owen, "snapshot-club", "snapshot-season", "snapshot-ridge")
    assert created.status_code == 201, created.get_json()
    with app.app_context():
        database = get_db()
        database.execute(
            "UPDATE expeditions SET image_ref = ? WHERE slug = 'snapshot-season'",
            (COVER,),
        )
        database.commit()
    joined = client.post("/api/v1/expeditions/snapshot-season/join", headers=_auth(sam))
    assert joined.status_code == 200, joined.get_json()
    preview = client.post(
        "/api/v1/activities/preview",
        json={"elevation_ft": "25", "happened_on": DAY},
        headers=_auth(sam),
    )
    assert preview.status_code == 200, preview.get_json()
    posted = client.post(
        "/api/v1/activities",
        json={
            "elevation_ft": "25",
            "happened_on": DAY,
            "confirmation_token": preview.get_json()["preview"]["confirmation_token"],
        },
        headers=_auth(sam),
    )
    assert posted.status_code == 201, posted.get_json()
    home = client.get("/api/v1/home", headers=_auth(sam)).get_json()
    card = next(row for row in home["expeditions"] if row["slug"] == "snapshot-season")
    assert card["organization"]["colors"] is None
    assert card["branding"]["image_ref"] == COVER
    assert card["branding"]["primary_color"] == DEFAULT_PRIMARY
    scene_path = _scene_path(home)
    assert scene_path != COVER
    assert home["journey"]["pending_arrivals"] == []
    progress, totals, views, credits = _journey_state(app, _sam)
    done = client.post("/api/v1/expeditions/snapshot-season/complete", headers=_auth(owen))
    assert done.status_code == 200, done.get_json()
    assert "branding" not in done.get_json()["entries"][0]
    snapshot = _result_rows(app, "snapshot-season")
    assert snapshot
    progress_after, totals_after, views_after, credits_after = _journey_state(app, _sam)
    recolored = client.patch(
        "/api/v1/organizations/snapshot-club/branding",
        json={"colors": {"primary": "#111111", "accent": "#222222"}, "welcome_message": "After close"},
        headers=_auth(owen),
    )
    assert recolored.status_code == 200, recolored.get_json()
    blocked = client.patch(
        "/api/v1/expeditions/snapshot-season/branding",
        json={"image_ref": None},
        headers=_auth(owen),
    )
    assert blocked.status_code == 409
    assert _result_rows(app, "snapshot-season") == snapshot
    later_progress, later_totals, later_views, later_credits = _journey_state(app, _sam)
    assert later_progress == progress_after
    assert later_totals == totals_after
    assert later_views == views_after
    assert later_credits == credits_after
    assert later_progress == progress
    assert later_totals == totals
    reread = client.get("/api/v1/expeditions/snapshot-season", headers=_auth(sam))
    body = reread.get_json()["expedition"]
    assert body["status"] == "completed"
    assert body["accent_color"] is None
    assert body["image_ref"] == COVER
    assert body["branding"]["primary_color"] == "#111111"
    assert body["branding"]["accent_color"] == "#222222"
    assert body["branding"]["image_ref"] == COVER
    assert body["branding"]["welcome_message"] == "After close"
    again = client.get("/api/v1/home", headers=_auth(sam)).get_json()
    assert _scene_path(again) == scene_path
    assert again["journey"]["pending_arrivals"] == []
    board = client.get(
        "/api/v1/expeditions/snapshot-season/leaderboard", headers=_auth(sam)
    ).get_json()
    assert board["mode"] == "final"
    assert "primary_color" not in board["entries"][0]
    with app.app_context():
        database = get_db()
        columns = {
            row["column_name"]
            for row in database.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'adventurer'
                  AND table_name IN ('expedition_result_entries', 'expedition_result_metrics')
                """
            ).fetchall()
        }
        database.rollback()
    assert "primary_color" not in columns
    assert "logo_ref" not in columns
    assert route_id


def test_organization_expedition_list_loads_the_brand_once(client, app, monkeypatch):
    _ada, ada, _csrf = _register(client, "Ada")
    _owen, owen, _csrf = _register(client, "Owen")
    app.config["PLATFORM_ADMIN_IDS"] = {_ada}
    with app.app_context():
        _route("once-alpha")
        _route("once-beta")
    _org(client, ada, "once-club", "Owen")
    assert _season(client, owen, "once-club", "once-alpha", "once-alpha").status_code == 201
    assert _season(client, owen, "once-club", "once-beta", "once-beta").status_code == 201
    import adventurer.expeditions as expeditions

    calls = {"n": 0}
    original = expeditions._organization_brand

    def wrapped(database, organization_id):
        calls["n"] += 1
        return original(database, organization_id)

    monkeypatch.setattr(expeditions, "_organization_brand", wrapped)
    listed = client.get("/api/v1/organizations/once-club/expeditions", headers=_auth(owen))
    assert listed.status_code == 200, listed.get_json()
    assert len(listed.get_json()["expeditions"]) == 2
    assert calls["n"] == 1


def test_home_branding_uses_the_same_snapshot(app, monkeypatch):
    from adventurer.services import create_adventurer

    with app.app_context():
        route_id = _route("home-brand")
        owner = create_adventurer("BrandOwner", "trailhead")
        member = create_adventurer("BrandMember", "trailhead")
        database = get_db()
        organization_id = database.execute(
            """
            INSERT INTO organizations (slug, name)
            VALUES ('home-brand', 'Home Brand')
            RETURNING id
            """
        ).fetchone()["id"]
        database.execute(
            """
            INSERT INTO organization_memberships (organization_id, user_id, role, status)
            VALUES (?, ?, 'owner', 'active')
            """,
            (organization_id, owner["id"]),
        )
        expedition_id = database.execute(
            """
            INSERT INTO expeditions (
                slug, name, route_id, visibility, kind, status, organization_id, enrollment
            )
            VALUES ('home-brand-season', 'Home Brand Season', ?, 'public',
                    'organization', 'active', ?, 'open')
            RETURNING id
            """,
            (route_id, organization_id),
        ).fetchone()["id"]
        database.commit()
    app.config["PLATFORM_ADMIN_IDS"] = set()
    from adventurer.expeditions import join_expedition

    with app.app_context():
        join_expedition(member["id"], "home-brand-season")
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
                and "from expedition_participations" in normalized
                and "organization.colors" in normalized
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
                    holder["result"] = compose_home(member["id"])
                except Exception as exc:
                    holder["error"] = exc
                finally:
                    get_db().close()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert reached.wait(timeout=10)
            main.execute(
                """
                UPDATE organizations
                SET colors = ?::jsonb
                WHERE slug = 'home-brand'
                """,
                (json.dumps({"accent": "#112233"}),),
            )
            main.commit()
        finally:
            allow.set()
            thread.join(timeout=20)
            monkeypatch.undo()
        assert "error" not in holder, holder.get("error")
        card = next(row for row in holder["result"]["expeditions"] if row["slug"] == "home-brand-season")
        assert card["organization"]["colors"] is None
        assert card["branding"]["accent_color"] == DEFAULT_ACCENT
        scene = holder["result"]["journey"]["scene"]
        fresh = compose_home(member["id"])
        again = next(row for row in fresh["expeditions"] if row["slug"] == "home-brand-season")
        assert again["organization"]["colors"] == {"accent": "#112233"}
        assert again["branding"]["accent_color"] == "#112233"
        assert fresh["journey"]["scene"] == scene
        get_db().close()


def _register(client, name, password="trailhead"):
    response = client.post(
        "/api/v1/auth/register",
        json={"name": name, "password": password},
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    client.delete_cookie("session")
    return body["you"]["id"], body["token"], body["csrf"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _route(slug: str) -> int:
    database = get_db()
    target = feet_to_meters(Decimal("1000"))
    route_id = database.execute(
        """
        INSERT INTO routes (slug, name, region, summary, status)
        VALUES (?, ?, 'Test', 'A test route', 'draft')
        RETURNING id
        """,
        (slug, slug),
    ).fetchone()["id"]
    database.execute(
        """
        INSERT INTO route_requirements (route_id, metric_code, target_amount)
        VALUES (?, 'elevation', ?)
        """,
        (route_id, target),
    )
    checkpoint_id = database.execute(
        """
        INSERT INTO route_checkpoints (route_id, slug, name, position)
        VALUES (?, 'summit', 'Summit', 2)
        RETURNING id
        """,
        (route_id,),
    ).fetchone()["id"]
    database.execute(
        """
        INSERT INTO checkpoint_requirements (
            checkpoint_id, route_id, metric_code, threshold_amount
        )
        VALUES (?, ?, 'elevation', ?)
        """,
        (checkpoint_id, route_id, target),
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


def _org(client, token, slug, owner):
    created = client.post(
        "/api/v1/organizations",
        json={"slug": slug, "name": slug, "owner_username": owner},
        headers=_auth(token),
    )
    assert created.status_code == 201, created.get_json()
    return created


def _season(client, token, organization, slug, route, **branding):
    earlier = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    body = {
        "slug": slug,
        "name": slug,
        "route_slug": route,
        "enrollment": "open",
        "starts_at": earlier,
        "ends_at": later,
    }
    body.update(branding)
    return client.post(
        f"/api/v1/organizations/{organization}/expeditions",
        json=body,
        headers=_auth(token),
    )


def _count(app, table: str) -> int:
    with app.app_context():
        row = get_db().execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        get_db().rollback()
        return row["count"]


def _scene_path(home: dict):
    image = ((home.get("journey") or {}).get("scene") or {}).get("image")
    if not image:
        return None
    return image.get("path")


def _journey_state(app, user_id: int):
    with app.app_context():
        database = get_db()
        journey = database.execute(
            """
            SELECT id, progress_version
            FROM route_journeys
            WHERE user_id = ? AND is_current
            ORDER BY id
            """,
            (user_id,),
        ).fetchone()
        totals = database.execute(
            """
            SELECT metric_code, credited_total
            FROM journey_metric_totals
            WHERE journey_id = ?
            ORDER BY metric_code
            """,
            (journey["id"],),
        ).fetchall()
        views = database.execute(
            "SELECT COUNT(*) AS count FROM journey_story_views WHERE journey_id = ?",
            (journey["id"],),
        ).fetchone()["count"]
        credits = database.execute(
            """
            SELECT metric_code, credited_amount
            FROM journey_credits
            WHERE journey_id = ?
            ORDER BY id
            """,
            (journey["id"],),
        ).fetchall()
        database.rollback()
    return (
        journey["progress_version"],
        [(row["metric_code"], row["credited_total"]) for row in totals],
        views,
        [(row["metric_code"], row["credited_amount"]) for row in credits],
    )


def _result_rows(app, slug: str):
    with app.app_context():
        database = get_db()
        rows = database.execute(
            """
            SELECT entry.user_id, entry.journey_id, entry.display_name,
                   entry.final_rank, entry.checkpoint_name,
                   metric.metric_code, metric.total_amount
            FROM expedition_result_entries AS entry
            JOIN expeditions AS expedition ON expedition.id = entry.expedition_id
            LEFT JOIN expedition_result_metrics AS metric
              ON metric.result_entry_id = entry.id
            WHERE expedition.slug = ?
            ORDER BY entry.user_id, metric.metric_code
            """,
            (slug,),
        ).fetchall()
        database.rollback()
    return [
        (
            row["user_id"],
            row["journey_id"],
            row["display_name"],
            row["final_rank"],
            row["checkpoint_name"],
            row["metric_code"],
            row["total_amount"],
        )
        for row in rows
    ]
