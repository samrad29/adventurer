"""Read the editorial activity types for a set of routes."""


def recommendations_for_routes(database, route_ids: list[int]) -> dict[int, list[str]]:
    if not route_ids:
        return {}
    rows = database.execute(
        """
        SELECT route_id, activity_type
        FROM route_activity_recommendations
        WHERE route_id = ANY(?)
        ORDER BY route_id, position
        """,
        (route_ids,),
    ).fetchall()
    grouped: dict[int, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["route_id"], []).append(row["activity_type"])
    return grouped
