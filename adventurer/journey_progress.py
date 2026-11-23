"""Read journey progress from route requirements and credit totals.

This module does not assign activities, write credits, or change journey
status. Completion is the storage comparison of every required metric with
its target. Checkpoint reach uses that same capped total. A read takes no
locks and leaves the transaction to its caller, or rolls a read-only
transaction back when it owns one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .db import get_db
from .metrics import MetricProgress, assess_metric


class ProgressError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CheckpointMark:
    id: int
    slug: str
    name: str
    position: int
    reached: bool
    thresholds: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True)
class JourneyProgress:
    journey_id: int
    user_id: int
    route_id: int
    status: str
    progress_version: int
    metrics: tuple[MetricProgress, ...]
    requirements_met: bool
    highest_checkpoint: CheckpointMark | None
    next_checkpoint: CheckpointMark | None
    final_checkpoint_reached: bool
    checkpoints: tuple[CheckpointMark, ...]


def journey_progress(actor_id: int, journey_id: int) -> JourneyProgress:
    """Return one person's journey progress. The read commits nothing."""
    database = get_db()
    try:
        _require_actor(database, actor_id)
        progress = read_journey_progress(database, journey_id)
        if progress.user_id != actor_id:
            raise ProgressError("forbidden", "You cannot open another person's journey.")
        return progress
    except ProgressError:
        database.rollback()
        raise
    finally:
        database.rollback()


def read_journey_progress(database, journey_id: int) -> JourneyProgress:
    """Load and assess one journey. Selects only. Does not lock or commit."""
    if not isinstance(journey_id, int) or isinstance(journey_id, bool) or journey_id <= 0:
        raise ProgressError("not_found", "That journey does not exist.")
    journey = database.execute(
        """
        SELECT id, user_id, route_id, status, progress_version
        FROM route_journeys
        WHERE id = ?
        """,
        (journey_id,),
    ).fetchone()
    if journey is None:
        raise ProgressError("not_found", "That journey does not exist.")
    requirements = database.execute(
        """
        SELECT requirement.metric_code, requirement.target_amount,
               catalog.storage_unit, catalog.display_unit
        FROM route_requirements AS requirement
        JOIN progress_metrics AS catalog ON catalog.code = requirement.metric_code
        WHERE requirement.route_id = ?
        ORDER BY requirement.metric_code
        """,
        (journey["route_id"],),
    ).fetchall()
    totals = {
        row["metric_code"]: row["credited_total"]
        for row in database.execute(
            """
            SELECT metric_code, credited_total
            FROM journey_metric_totals
            WHERE journey_id = ?
            """,
            (journey_id,),
        ).fetchall()
    }
    metrics = []
    for row in requirements:
        _require_catalog(row)
        metrics.append(
            assess_metric(
                row["metric_code"],
                row["target_amount"],
                totals.get(row["metric_code"], Decimal("0")),
            )
        )
    metrics = tuple(metrics)
    credited = {metric.code: metric.credited_m for metric in metrics}
    checkpoints = _checkpoints(database, journey["route_id"])
    marks = assess_checkpoints(checkpoints, credited)
    final = _final(marks)
    return JourneyProgress(
        journey_id=journey["id"],
        user_id=journey["user_id"],
        route_id=journey["route_id"],
        status=journey["status"],
        progress_version=journey["progress_version"],
        metrics=metrics,
        requirements_met=bool(metrics) and all(metric.complete for metric in metrics),
        highest_checkpoint=_first(marks, reached=True, reverse=True),
        next_checkpoint=_first(marks, reached=False, reverse=False),
        final_checkpoint_reached=final is not None and final.reached,
        checkpoints=marks,
    )


def assess_checkpoints(
    checkpoints: tuple[CheckpointMark, ...] | list[CheckpointMark],
    credited_m: dict[str, Decimal],
) -> tuple[CheckpointMark, ...]:
    """Mark each checkpoint from its own thresholds. Order is position, then id.

    A checkpoint is reached only when every one of its thresholds is met by
    the capped stored total. One satisfied metric does not unlock the rest.
    """
    ordered = tuple(sorted(checkpoints, key=lambda mark: (mark.position, mark.id)))
    marked = []
    for checkpoint in ordered:
        reached = bool(checkpoint.thresholds) and all(
            credited_m.get(metric, Decimal("0")) >= amount
            for metric, amount in checkpoint.thresholds
        )
        marked.append(
            CheckpointMark(
                id=checkpoint.id,
                slug=checkpoint.slug,
                name=checkpoint.name,
                position=checkpoint.position,
                reached=reached,
                thresholds=tuple(checkpoint.thresholds),
            )
        )
    return tuple(marked)


def _checkpoints(database, route_id: int) -> tuple[CheckpointMark, ...]:
    rows = database.execute(
        """
        SELECT id, slug, name, position
        FROM route_checkpoints
        WHERE route_id = ?
        ORDER BY position, id
        """,
        (route_id,),
    ).fetchall()
    thresholds: dict[int, list[tuple[str, Decimal]]] = {row["id"]: [] for row in rows}
    for row in database.execute(
        """
        SELECT checkpoint_id, metric_code, threshold_amount
        FROM checkpoint_requirements
        WHERE route_id = ?
        ORDER BY checkpoint_id, metric_code
        """,
        (route_id,),
    ).fetchall():
        thresholds[row["checkpoint_id"]].append(
            (row["metric_code"], row["threshold_amount"])
        )
    return tuple(
        CheckpointMark(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            position=row["position"],
            reached=False,
            thresholds=tuple(thresholds[row["id"]]),
        )
        for row in rows
    )


def _require_catalog(row: dict) -> None:
    if row["storage_unit"] != "m":
        raise ProgressError("invalid_state", "A progress metric must be stored in meters.")
    expected = "ft" if row["metric_code"] == "elevation" else "mi"
    if row["metric_code"] not in ("elevation", "distance") or row["display_unit"] != expected:
        raise ProgressError("invalid_state", "That progress metric is not elevation or distance.")


def _final(marks: tuple[CheckpointMark, ...]) -> CheckpointMark | None:
    if not marks:
        return None
    return max(marks, key=lambda mark: (mark.position, mark.id))


def _first(
    marks: tuple[CheckpointMark, ...],
    *,
    reached: bool,
    reverse: bool,
) -> CheckpointMark | None:
    choices = [mark for mark in marks if mark.reached is reached]
    if not choices:
        return None
    return (max if reverse else min)(choices, key=lambda mark: (mark.position, mark.id))


def _require_actor(database, actor_id: int) -> None:
    if not isinstance(actor_id, int) or isinstance(actor_id, bool) or actor_id <= 0:
        raise ProgressError("not_found", "That account does not exist.")
    row = database.execute(
        "SELECT id, deletion_state FROM users WHERE id = ?",
        (actor_id,),
    ).fetchone()
    if row is None:
        raise ProgressError("not_found", "That account does not exist.")
    if row["deletion_state"] == "deleting":
        raise ProgressError("forbidden", "This account is being deleted.")
