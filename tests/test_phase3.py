"""Route catalog and account creation that still match the domain architecture.

Expedition credits, automatic Everest enrollment, and the old leaderboard
are not part of this suite. Those rules now live on route journeys.
"""

from __future__ import annotations

from adventurer.db import get_db
from adventurer.services import create_adventurer


def test_published_routes_hide_draft_and_retired(app):
    with app.app_context():
        get_db().execute(
            """
            INSERT INTO routes (slug, name, region, summary, target_elevation_m, status)
            VALUES
                ('draft-ridge', 'Draft Ridge', 'Test', 'Hidden', 1000, 'draft'),
                ('old-ridge', 'Old Ridge', 'Test', 'Retired', 1000, 'retired')
            """
        )
        get_db().commit()
    client = app.test_client()
    listed = client.get("/api/v1/routes").get_json()["routes"]
    slugs = [row["slug"] for row in listed]
    assert "everest" in slugs
    assert "draft-ridge" not in slugs
    assert "old-ridge" not in slugs
    assert client.get("/api/v1/routes/draft-ridge").status_code == 404
    assert client.get("/api/v1/routes/old-ridge").status_code == 404
    detail = client.get("/api/v1/routes/everest").get_json()
    assert detail["name"] == "Everest"
    assert detail["region"] == "Khumbu, Nepal"
    assert detail["summit_ft"] == 29032
    assert len(detail["checkpoints"]) == 16
    assert [stop["slug"] for stop in detail["checkpoints"]][:2] == ["timms-hill", "four-thousand"]
    assert detail["checkpoints"][-1]["slug"] == "summit"
    assert detail["expeditions"][0]["slug"] == "everest"
    page = client.get("/routes/everest")
    assert page.status_code == 200
    assert "Global Everest Challenge" in page.get_data(as_text=True)
    assert "Timms Hill" in page.get_data(as_text=True)


def test_account_creation_does_not_join_an_expedition_or_start_a_journey(app):
    with app.app_context():
        created = create_adventurer("Rae", "trailhead")
        counts = get_db().execute(
            """
            SELECT
                (SELECT COUNT(*) FROM expedition_participations WHERE user_id = ?) AS participations,
                (SELECT COUNT(*) FROM route_journeys WHERE user_id = ?) AS journeys
            """,
            (created["id"], created["id"]),
        ).fetchone()
    assert counts["participations"] == 0
    assert counts["journeys"] == 0
