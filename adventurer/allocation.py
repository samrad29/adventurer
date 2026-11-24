"""One allocation of an unfinished journey's assigned measurements.

The credit rows are the result of this walk. A new activity that sorts last
can be inserted by the same walk; there is no second allocator. The preflight
reads unfinished journeys and writes nothing. A non-empty report means the
deterministic writer must not be enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from adventurer.metrics import STORAGE_QUANTUM

SUPPORTED_TYPES = frozenset(
    {
        "run",
        "hike",
        "walk",
        "stair_climb",
        "other",
        "unspecified",
        "cycle",
        "indoor_cycle",
    }
)


class AllocationCutoverError(RuntimeError):
    """Unfinished journeys still differ from the allocation formula."""

    def __init__(self, report: list[dict]) -> None:
        self.report = report
        super().__init__(
            f"{len(report)} unfinished journey metrics differ from the allocation"
        )


@dataclass
class MeasuredAssignment:
    activity_id: int | None
    assignment_id: int | None
    created_at: datetime
    sort_last: bool
    activity_type: str
    elevation_m: Decimal | None
    distance_m: Decimal | None

    def measurement(self, metric: str) -> Decimal | None:
        if metric == "elevation":
            return self.elevation_m
        if metric == "distance":
            return self.distance_m
        return None


def canonical_meters(value: Decimal) -> str:
    return format(Decimal(value).quantize(STORAGE_QUANTUM), "f")


def ordered_assignments(assignments: list[MeasuredAssignment]) -> list[MeasuredAssignment]:
    return sorted(
        assignments,
        key=lambda item: (
            1 if item.sort_last else 0,
            item.created_at,
            item.activity_id if item.activity_id is not None else 0,
        ),
    )


def _eligible(activity_type: str) -> bool:
    return activity_type in SUPPORTED_TYPES


def allocate(
    assignments: list[MeasuredAssignment],
    requirements: list[tuple[str, Decimal]],
) -> dict[int | None, dict[str, Decimal]]:
    """Positive credits keyed by activity id. A not-yet-saved activity uses None."""
    shares: dict[int | None, dict[str, Decimal]] = {}
    for metric, target in requirements:
        remaining = target
        for assignment in ordered_assignments(assignments):
            if not _eligible(assignment.activity_type) or remaining <= 0:
                continue
            measurement = assignment.measurement(metric)
            if measurement is None or measurement <= 0:
                continue
            credit = measurement if measurement < remaining else remaining
            if credit <= 0:
                continue
            shares.setdefault(assignment.activity_id, {})[metric] = credit
            remaining -= credit
    return shares


def totals_for(
    shares: dict[int | None, dict[str, Decimal]],
    requirements: list[tuple[str, Decimal]],
) -> dict[str, Decimal]:
    totals = {metric: Decimal("0") for metric, _target in requirements}
    for metrics in shares.values():
        for metric, amount in metrics.items():
            if metric in totals:
                totals[metric] += amount
    return totals


def allocation_fingerprint(
    assignments: list[MeasuredAssignment],
    shares: dict[int | None, dict[str, Decimal]],
) -> str:
    parts: list[str] = []
    for assignment in ordered_assignments(assignments):
        key = "new" if assignment.activity_id is None else str(assignment.activity_id)
        metrics = shares.get(assignment.activity_id, {})
        for metric in sorted(metrics):
            amount = metrics[metric]
            if amount > 0:
                parts.append(f"{key}:{metric}={canonical_meters(amount)}")
    return ",".join(parts)


def measurement_token(elevation_m: Decimal | None, distance_m: Decimal | None) -> str:
    elevation = "" if elevation_m is None else canonical_meters(elevation_m)
    distance = "" if distance_m is None else canonical_meters(distance_m)
    return f"elevation={elevation},distance={distance}"


def preflight_report(database) -> list[dict]:
    """Read-only differences between stored credits and the allocation formula."""
    journeys = database.execute(
        """
        SELECT id, status, route_id
        FROM route_journeys
        WHERE status IN ('active', 'paused', 'abandoned')
        ORDER BY id
        """
    ).fetchall()
    report: list[dict] = []
    for journey in journeys:
        requirements = [
            (row["metric_code"], row["target_amount"])
            for row in database.execute(
                """
                SELECT metric_code, target_amount
                FROM route_requirements
                WHERE route_id = ?
                ORDER BY metric_code
                """,
                (journey["route_id"],),
            ).fetchall()
        ]
        assignments = [
            MeasuredAssignment(
                activity_id=row["activity_id"],
                assignment_id=row["assignment_id"],
                created_at=row["created_at"],
                sort_last=False,
                activity_type=row["activity_type"],
                elevation_m=row["elevation_gain_m"],
                distance_m=row["distance_m"],
            )
            for row in database.execute(
                """
                SELECT assignment.id AS assignment_id,
                       assignment.created_at,
                       activity.id AS activity_id,
                       activity.activity_type,
                       activity.elevation_gain_m,
                       activity.distance_m
                FROM activity_journey_assignments AS assignment
                JOIN activities AS activity ON activity.id = assignment.activity_id
                WHERE assignment.journey_id = ?
                ORDER BY assignment.created_at, activity.id
                """,
                (journey["id"],),
            ).fetchall()
        ]
        shares = allocate(assignments, requirements)
        expected_totals = totals_for(shares, requirements)
        stored_credits: dict[int, dict[str, Decimal]] = {}
        for row in database.execute(
            """
            SELECT assignment_id, metric_code, credited_amount
            FROM journey_credits
            WHERE journey_id = ?
            """,
            (journey["id"],),
        ).fetchall():
            stored_credits.setdefault(row["assignment_id"], {})[row["metric_code"]] = row[
                "credited_amount"
            ]
        stored_totals = {
            row["metric_code"]: row["credited_total"]
            for row in database.execute(
                """
                SELECT metric_code, credited_total
                FROM journey_metric_totals
                WHERE journey_id = ?
                """,
                (journey["id"],),
            ).fetchall()
        }
        known_assignments = {item.assignment_id for item in assignments}
        for metric, _target in requirements:
            expected_total = expected_totals.get(metric, Decimal("0"))
            stored_total = stored_totals.get(metric, Decimal("0"))
            mismatches = []
            for assignment in assignments:
                expected = shares.get(assignment.activity_id, {}).get(metric, Decimal("0"))
                stored = stored_credits.get(assignment.assignment_id, {}).get(
                    metric, Decimal("0")
                )
                if stored != expected:
                    mismatches.append(
                        {
                            "assignment_id": assignment.assignment_id,
                            "activity_id": assignment.activity_id,
                            "stored": stored,
                            "expected": expected,
                        }
                    )
            if stored_total != expected_total or mismatches:
                report.append(
                    {
                        "journey_id": journey["id"],
                        "status": journey["status"],
                        "metric": metric,
                        "stored_total": stored_total,
                        "expected_total": expected_total,
                        "assignments": mismatches,
                    }
                )
        unused = set(stored_credits) - known_assignments
        if unused:
            report.append(
                {
                    "journey_id": journey["id"],
                    "status": journey["status"],
                    "metric": None,
                    "stored_total": None,
                    "expected_total": None,
                    "assignments": [
                        {"assignment_id": assignment_id, "activity_id": None}
                        for assignment_id in sorted(unused)
                    ],
                }
            )
    return report


def require_allocation_cutover(database) -> list[dict]:
    """Refuse a cutover while any unfinished journey disagrees with the formula."""
    report = preflight_report(database)
    if report:
        raise AllocationCutoverError(report)
    return report
