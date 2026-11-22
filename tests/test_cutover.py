"""Route checkpoints still match the curated Everest story.

Per-expedition credit behavior from the old cutover is gone. Journey
credits are covered in test_domain_foundation.py.
"""

from __future__ import annotations

from decimal import Decimal

from adventurer.challenges import get_challenge
from adventurer.db import get_db
from adventurer.measures import feet_to_meters
from adventurer.services import load_route


def test_static_checkpoints_match_the_database(app):
    everest = get_challenge("everest")
    with app.app_context():
        rows = get_db().execute(
            """
            SELECT checkpoint.slug, checkpoint.name, checkpoint.position, checkpoint.elevation_m,
                   route.target_elevation_m
            FROM route_checkpoints AS checkpoint
            JOIN routes AS route ON route.id = checkpoint.route_id
            WHERE route.slug = 'everest'
            ORDER BY checkpoint.position
            """
        ).fetchall()
        route = load_route("everest")
    assert [row["slug"] for row in rows] == [stop["slug"] for stop in everest["stops"]]
    assert [stop["slug"] for stop in route["stops"]] == [stop["slug"] for stop in everest["stops"]]
    for index, (row, stop) in enumerate(zip(rows, everest["stops"])):
        assert row["position"] == index
        assert row["name"] == stop["name"]
        assert row["elevation_m"] == feet_to_meters(Decimal(str(stop["elevation_ft"])))
        assert route["stops"][index]["elevation_ft"] == stop["elevation_ft"]
    assert rows[-1]["elevation_m"] == rows[-1]["target_elevation_m"]
    assert route["summit_ft"] == everest["summit_ft"]
