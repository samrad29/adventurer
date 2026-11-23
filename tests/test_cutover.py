"""Route checkpoints still match the curated Everest story.

Per-expedition credit behavior from the old cutover is gone. Journey
credits are covered in test_domain_foundation.py.
"""

from __future__ import annotations

from decimal import Decimal

from adventurer.challenges import get_challenge
from adventurer.db import get_db
from adventurer.measures import feet_to_meters


def test_static_checkpoints_match_the_database(app):
    everest = get_challenge("everest")
    with app.app_context():
        rows = get_db().execute(
            """
            SELECT checkpoint.slug, checkpoint.name, checkpoint.position,
                   requirement.threshold_amount
            FROM route_checkpoints AS checkpoint
            JOIN routes AS route ON route.id = checkpoint.route_id
            JOIN checkpoint_requirements AS requirement
                ON requirement.checkpoint_id = checkpoint.id
               AND requirement.metric_code = 'elevation'
            WHERE route.slug = 'everest'
            ORDER BY checkpoint.position
            """
        ).fetchall()
        target = get_db().execute(
            """
            SELECT requirement.target_amount
            FROM route_requirements AS requirement
            JOIN routes AS route ON route.id = requirement.route_id
            WHERE route.slug = 'everest' AND requirement.metric_code = 'elevation'
            """
        ).fetchone()["target_amount"]
    assert [row["slug"] for row in rows] == [stop["slug"] for stop in everest["stops"]]
    for index, (row, stop) in enumerate(zip(rows, everest["stops"])):
        assert row["position"] == index
        assert row["name"] == stop["name"]
        assert row["threshold_amount"] == feet_to_meters(Decimal(str(stop["elevation_ft"])))
    assert rows[-1]["threshold_amount"] == target
    assert rows[-1]["threshold_amount"] == feet_to_meters(Decimal(str(everest["summit_ft"])))
