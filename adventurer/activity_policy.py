"""Expedition and journey activity eligibility.

The journey stores the effective policy. Expeditions supply it only while they
govern. Credit still comes from the one allocator. This module does not write
credit rows.
"""

from __future__ import annotations

import hmac
import hashlib
import time
from dataclasses import dataclass
from decimal import Decimal

from flask import current_app

from .allocation import SUPPORTED_TYPES, canonical_meters

PRESETS = {
    "on_foot": ("hike", "run", "stair_climb", "walk"),
    "cycling": ("cycle", "indoor_cycle"),
    "all": (),
}
_SORTED_PRESETS = {
    "on_foot": tuple(sorted(PRESETS["on_foot"])),
    "cycling": tuple(sorted(PRESETS["cycling"])),
}
TOKEN_TTL = 600


class PolicyError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **details) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


@dataclass(frozen=True)
class Policy:
    mode: str
    types: tuple[str, ...] = ()

    def canonical(self) -> str:
        if self.mode == "unrestricted":
            return "unrestricted"
        return "restricted:" + ",".join(self.types)


UNRESTRICTED = Policy("unrestricted")


def parse_policy(value) -> Policy:
    """Read one create-time policy. Omitted means unrestricted."""
    if value is None:
        return UNRESTRICTED
    if not isinstance(value, dict):
        raise PolicyError("invalid", "Send activity_policy as an object.")
    allowed = {"mode", "activity_types", "preset"}
    unknown = set(value) - allowed
    if unknown:
        raise PolicyError("invalid", "That activity policy field is not supported.")
    if "preset" in value and ("mode" in value or "activity_types" in value):
        raise PolicyError("invalid", "Send a preset or an activity list, not both.")
    if "preset" in value:
        preset = value["preset"]
        if preset == "all":
            return UNRESTRICTED
        if preset not in _SORTED_PRESETS:
            raise PolicyError("invalid", "That activity preset is not supported.")
        return Policy("restricted", _SORTED_PRESETS[preset])
    mode = value.get("mode", "unrestricted")
    if mode == "unrestricted":
        if value.get("activity_types"):
            raise PolicyError("invalid", "An unrestricted policy has no activity list.")
        return UNRESTRICTED
    if mode != "restricted":
        raise PolicyError("invalid", "Activity policy is unrestricted or restricted.")
    return _restricted(value.get("activity_types"))


def _restricted(raw) -> Policy:
    if not isinstance(raw, list) or not raw:
        raise PolicyError("invalid", "A restricted policy needs at least one activity type.")
    if any(not isinstance(item, str) for item in raw):
        raise PolicyError("invalid", "Name each activity type as text.")
    if len(set(raw)) != len(raw):
        raise PolicyError("invalid", "Name each activity type once.")
    unknown = [item for item in raw if item not in SUPPORTED_TYPES]
    if unknown:
        raise PolicyError("invalid", "That activity type is not supported.")
    return Policy("restricted", tuple(sorted(raw)))


def public_policy(policy: Policy) -> dict:
    if policy.mode == "unrestricted":
        return {"mode": "unrestricted"}
    return {"mode": "restricted", "activity_types": list(policy.types)}


def accepts(policy: Policy, activity_type: str) -> bool:
    if activity_type not in SUPPORTED_TYPES:
        return False
    if policy.mode == "unrestricted":
        return True
    return activity_type in policy.types


def permitted(policy: Policy) -> tuple[str, ...]:
    if policy.mode == "unrestricted":
        return tuple(sorted(SUPPORTED_TYPES))
    return policy.types


def compatible(receiver: Policy, journey: Policy) -> bool:
    """A receiver can share a journey when it accepts every permitted type."""
    if journey.mode == "unrestricted":
        return receiver.mode == "unrestricted"
    if receiver.mode == "unrestricted":
        return True
    return set(journey.types) <= set(receiver.types)


def narrows(current: Policy, proposed: Policy) -> bool:
    if proposed.mode == "unrestricted":
        return False
    if current.mode == "unrestricted":
        return True
    return set(proposed.types) < set(current.types)


def rejected_types(current: Policy, receiver: Policy) -> list[str]:
    if receiver.mode == "unrestricted":
        return []
    allowed = set(receiver.types)
    return [code for code in permitted(current) if code not in allowed]


def load_expedition_policy(database, expedition_id: int) -> Policy:
    row = database.execute(
        "SELECT activity_policy, kind FROM expeditions WHERE id = ?",
        (expedition_id,),
    ).fetchone()
    if row is None or row["activity_policy"] == "unrestricted":
        return UNRESTRICTED
    types = _type_rows(
        database,
        "SELECT activity_type FROM expedition_activity_types WHERE expedition_id = ? ORDER BY activity_type",
        expedition_id,
    )
    return Policy("restricted", types)


def load_journey_policy(database, journey_id: int) -> Policy:
    row = database.execute(
        "SELECT activity_policy FROM route_journeys WHERE id = ?",
        (journey_id,),
    ).fetchone()
    if row is None or row["activity_policy"] == "unrestricted":
        return UNRESTRICTED
    return Policy("restricted", _journey_types(database, journey_id))


def _journey_types(database, journey_id: int) -> tuple[str, ...]:
    return _type_rows(
        database,
        "SELECT activity_type FROM journey_activity_types WHERE journey_id = ? ORDER BY activity_type",
        journey_id,
    )


def _type_rows(database, sql: str, parent_id: int) -> tuple[str, ...]:
    return tuple(row["activity_type"] for row in database.execute(sql, (parent_id,)).fetchall())


def store_expedition_policy(database, expedition_id: int, policy: Policy) -> None:
    kind = database.execute(
        "SELECT kind FROM expeditions WHERE id = ?",
        (expedition_id,),
    ).fetchone()["kind"]
    if kind == "global" and policy.mode == "restricted":
        raise PolicyError("invalid", "Global Everest stays unrestricted.")
    if policy.mode == "unrestricted":
        database.execute(
            "UPDATE expeditions SET activity_policy_sealed = true WHERE id = ?",
            (expedition_id,),
        )
        return
    database.execute(
        """
        UPDATE expeditions
        SET activity_policy = 'restricted', activity_policy_sealed = false
        WHERE id = ?
        """,
        (expedition_id,),
    )
    for activity_type in policy.types:
        database.execute(
            """
            INSERT INTO expedition_activity_types (expedition_id, activity_type)
            VALUES (?, ?)
            """,
            (expedition_id, activity_type),
        )
    database.execute(
        "UPDATE expeditions SET activity_policy_sealed = true WHERE id = ?",
        (expedition_id,),
    )


def store_journey_policy(database, journey_id: int, policy: Policy, governor_id: int | None) -> None:
    database.execute(
        """
        UPDATE route_journeys
        SET activity_policy = ?, governing_expedition_id = ?
        WHERE id = ?
        """,
        (policy.mode, governor_id, journey_id),
    )
    database.execute(
        "DELETE FROM journey_activity_types WHERE journey_id = ?",
        (journey_id,),
    )
    for activity_type in policy.types:
        database.execute(
            """
            INSERT INTO journey_activity_types (journey_id, activity_type)
            VALUES (?, ?)
            """,
            (journey_id, activity_type),
        )
    if governor_id is not None:
        clear_choice(database, journey_id)


def copy_journey_policy(database, source_id: int, dest_id: int) -> None:
    policy = load_journey_policy(database, source_id)
    governor = database.execute(
        """
        SELECT governing_expedition_id
        FROM route_journeys
        WHERE id = ?
        """,
        (source_id,),
    ).fetchone()["governing_expedition_id"]
    kept = None
    if governor is not None:
        still = database.execute(
            """
            SELECT expedition.id
            FROM expeditions AS expedition
            JOIN expedition_participations AS participation
              ON participation.expedition_id = expedition.id
            WHERE expedition.id = ?
              AND expedition.status = 'active'
              AND participation.journey_id = ?
              AND participation.status = 'active'
            """,
            (governor, dest_id),
        ).fetchone()
        if still is not None:
            kept = governor
    store_journey_policy(database, dest_id, policy, kept if policy.mode == "restricted" else None)
    clear_choice(database, source_id)


def clear_choice(database, journey_id: int) -> None:
    database.execute(
        "DELETE FROM journey_policy_choices WHERE journey_id = ?",
        (journey_id,),
    )


def write_choice(database, journey_id: int, reason: str, former_id: int | None) -> None:
    database.execute(
        """
        INSERT INTO journey_policy_choices (journey_id, reason, former_expedition_id)
        VALUES (?, ?, ?)
        ON CONFLICT (journey_id) DO UPDATE
        SET reason = EXCLUDED.reason,
            former_expedition_id = EXCLUDED.former_expedition_id
        """,
        (journey_id, reason, former_id),
    )


def refresh_governor(database, journey_id: int, *, reason: str | None, former_id: int | None) -> None:
    """Set the lowest same-list governor, or keep the list and open a choice."""
    row = database.execute(
        """
        SELECT status, activity_policy, governing_expedition_id
        FROM route_journeys
        WHERE id = ?
        """,
        (journey_id,),
    ).fetchone()
    if row is None or row["status"] == "completed" or row["activity_policy"] != "restricted":
        return
    expected = _lowest_same_list(database, journey_id)
    if expected is not None:
        if row["governing_expedition_id"] != expected:
            database.execute(
                """
                UPDATE route_journeys
                SET governing_expedition_id = ?
                WHERE id = ?
                """,
                (expected, journey_id),
            )
        clear_choice(database, journey_id)
        return
    if row["governing_expedition_id"] is not None:
        database.execute(
            """
            UPDATE route_journeys
            SET governing_expedition_id = NULL
            WHERE id = ?
            """,
            (journey_id,),
        )
    if reason is not None:
        write_choice(database, journey_id, reason, former_id)


def release_expedition(database, expedition_id: int, reason: str) -> None:
    journeys = database.execute(
        """
        SELECT id, governing_expedition_id
        FROM route_journeys
        WHERE governing_expedition_id = ?
        UNION
        SELECT journey_id, NULL
        FROM expedition_participations
        WHERE expedition_id = ?
        """,
        (expedition_id, expedition_id),
    ).fetchall()
    seen: set[int] = set()
    for row in journeys:
        journey_id = row["id"]
        if journey_id in seen:
            continue
        seen.add(journey_id)
        current = database.execute(
            "SELECT governing_expedition_id, status FROM route_journeys WHERE id = ?",
            (journey_id,),
        ).fetchone()
        if current is None or current["status"] == "completed":
            continue
        was_governor = current["governing_expedition_id"] == expedition_id
        refresh_governor(
            database,
            journey_id,
            reason=reason if was_governor else None,
            former_id=expedition_id if was_governor else None,
        )


def _lowest_same_list(database, journey_id: int) -> int | None:
    row = database.execute(
        """
        SELECT expedition.id
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        JOIN route_journeys AS journey ON journey.id = participation.journey_id
        WHERE participation.journey_id = ?
          AND participation.user_id = journey.user_id
          AND participation.status = 'active'
          AND expedition.status = 'active'
          AND expedition.route_id = journey.route_id
          AND expedition.activity_policy = 'restricted'
          AND journey.activity_policy = 'restricted'
          AND NOT EXISTS (
              SELECT activity_type FROM expedition_activity_types WHERE expedition_id = expedition.id
              EXCEPT
              SELECT activity_type FROM journey_activity_types WHERE journey_id = journey.id
          )
          AND NOT EXISTS (
              SELECT activity_type FROM journey_activity_types WHERE journey_id = journey.id
              EXCEPT
              SELECT activity_type FROM expedition_activity_types WHERE expedition_id = expedition.id
          )
        ORDER BY expedition.id
        LIMIT 1
        """,
        (journey_id,),
    ).fetchone()
    if row is None:
        return None
    return row["id"]


def expansion_state(database, journey_id: int) -> dict:
    row = database.execute(
        """
        SELECT status, activity_policy, governing_expedition_id, user_id, route_id
        FROM route_journeys
        WHERE id = ?
        """,
        (journey_id,),
    ).fetchone()
    if row is None or row["status"] == "completed":
        return {"available": False, "reason": "completed", "blocked_by": []}
    if row["activity_policy"] == "unrestricted":
        return {"available": False, "reason": "already_unrestricted", "blocked_by": []}
    if row["governing_expedition_id"] is not None:
        return {"available": False, "reason": "governing_expedition", "blocked_by": []}
    blocked = _unrestricted_conflicts(database, row["user_id"], row["route_id"], journey_id)
    if blocked:
        return {"available": False, "reason": "incompatible_participation", "blocked_by": blocked}
    return {"available": True, "blocked_by": []}


def _unrestricted_conflicts(database, user_id: int, route_id: int, journey_id: int) -> list[str]:
    rows = database.execute(
        """
        SELECT expedition.slug
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ?
          AND participation.journey_id = ?
          AND participation.status = 'active'
          AND expedition.status = 'active'
          AND expedition.route_id = ?
          AND expedition.activity_policy = 'restricted'
        ORDER BY expedition.slug
        """,
        (user_id, journey_id, route_id),
    ).fetchall()
    return [row["slug"] for row in rows]


def active_expedition_ids(database, user_id: int, journey_id: int) -> list[int]:
    rows = database.execute(
        """
        SELECT expedition.id
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ?
          AND participation.journey_id = ?
          AND participation.status = 'active'
          AND expedition.status = 'active'
        ORDER BY expedition.id
        """,
        (user_id, journey_id),
    ).fetchall()
    return [row["id"] for row in rows]


def conflict_body(journey: Policy, rejected: list[str], expeditions: list[dict]) -> dict:
    return {
        "journey_policy": public_policy(journey),
        "rejected_activity_types": rejected,
        "conflicting_expeditions": expeditions,
    }


def expedition_brief(database, expedition_id: int) -> dict:
    row = database.execute(
        "SELECT slug, name FROM expeditions WHERE id = ?",
        (expedition_id,),
    ).fetchone()
    return {"slug": row["slug"], "name": row["name"]}


def issue_token(
    *,
    user_id: int,
    journey_id: int,
    operation: str,
    expedition_id: int | None,
    current: Policy,
    proposed: Policy,
    participation_ids: list[int],
    lifecycle: str,
    allocation: str,
    totals: str,
    progress_version: int,
) -> tuple[str, int]:
    expires = int(time.time()) + TOKEN_TTL
    payload = "|".join(
        [
            "p1",
            str(user_id),
            str(journey_id),
            operation,
            "-" if expedition_id is None else str(expedition_id),
            current.canonical(),
            proposed.canonical(),
            ",".join(str(item) for item in participation_ids),
            lifecycle,
            allocation,
            totals,
            str(progress_version),
            str(expires),
        ]
    )
    signature = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{signature}", expires


def token_matches(token: str | None, **expected) -> bool:
    if not token or not isinstance(token, str):
        return False
    payload, separator, signature = token.rpartition("|")
    if not separator or payload.count("|") != 12:
        return False
    digest = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, signature):
        return False
    parts = payload.split("|")
    if parts[0] != "p1":
        return False
    try:
        expires = int(parts[12])
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    expedition = "-" if expected["expedition_id"] is None else str(expected["expedition_id"])
    participations = ",".join(str(item) for item in expected["participation_ids"])
    return (
        parts[1] == str(expected["user_id"])
        and parts[2] == str(expected["journey_id"])
        and parts[3] == expected["operation"]
        and parts[4] == expedition
        and parts[5] == expected["current"].canonical()
        and parts[6] == expected["proposed"].canonical()
        and parts[7] == participations
        and parts[8] == expected["lifecycle"]
        and parts[9] == expected["allocation"]
        and parts[10] == expected["totals"]
        and parts[11] == str(expected["progress_version"])
    )


def totals_text(requirements, totals: dict[str, Decimal]) -> str:
    parts = []
    for code, _target in requirements:
        parts.append(f"{code}={canonical_meters(totals.get(code, Decimal('0')))}")
    return ",".join(parts)


def classify(journey: Policy, receiver: Policy) -> str:
    """share, adopt, or conflict. Adopt is a strict narrowing."""
    if compatible(receiver, journey) and not narrows(journey, receiver):
        return "share"
    if narrows(journey, receiver):
        return "adopt"
    return "conflict"


def conflicting_receivers(database, user_id: int, route_id: int, proposed: Policy, skip_id: int | None) -> list[dict]:
    rows = database.execute(
        """
        SELECT expedition.id
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ?
          AND participation.status = 'active'
          AND expedition.status = 'active'
          AND expedition.route_id = ?
        ORDER BY expedition.id
        """,
        (user_id, route_id),
    ).fetchall()
    conflicts = []
    for row in rows:
        if row["id"] == skip_id:
            continue
        if not compatible(load_expedition_policy(database, row["id"]), proposed):
            conflicts.append(expedition_brief(database, row["id"]))
    return conflicts


def policy_preview_body(
    database,
    *,
    operation: str,
    journey_id: int,
    route_id: int,
    current: Policy,
    proposed: Policy,
    proposal: dict,
    lifecycle: str,
    will_resume: bool,
    will_complete: bool,
) -> dict:
    from .journey_progress import _checkpoints, assess_checkpoints

    requirements = proposal["requirements"]
    allocations = []
    excluded_count = 0
    excluded_measurements: dict[str, Decimal] = {code: Decimal("0") for code, _target in requirements}
    for slot in proposal["slots"]:
        metrics = proposal["shares"].get(slot.activity_id, {})
        if slot.activity_id is None:
            continue
        eligible = accepts(proposed, slot.activity_type)
        measured = {
            "elevation": slot.elevation_m or Decimal("0"),
            "distance": slot.distance_m or Decimal("0"),
        }
        positive = any(measured.get(code, Decimal("0")) > 0 for code, _target in requirements)
        if not eligible and positive:
            excluded_count += 1
            for code, _target in requirements:
                excluded_measurements[code] += measured.get(code, Decimal("0"))
        for code, _target in requirements:
            allocations.append(
                {
                    "activity_id": slot.activity_id,
                    "metric": code,
                    "credited_m": str(metrics.get(code, Decimal("0"))),
                }
            )
    current_totals = proposal["journey"]["totals"]
    marks = _checkpoints(database, route_id)
    current_reached = [mark for mark in assess_checkpoints(marks, current_totals) if mark.reached]
    next_reached = [mark for mark in assess_checkpoints(marks, proposal["resulting"]) if mark.reached]
    return {
        "operation": operation,
        "current_policy": public_policy(current),
        "proposed_policy": public_policy(proposed),
        "metrics": [
            {
                "metric": code,
                "current_credited": str(current_totals.get(code, Decimal("0"))),
                "proposed_credited": str(proposal["resulting"].get(code, Decimal("0"))),
            }
            for code, _target in requirements
        ],
        "allocations": allocations,
        "excluded_activities": {
            "count": excluded_count,
            "measurements": [
                {"metric": code, "total_m": str(excluded_measurements[code])}
                for code, _target in requirements
            ],
        },
        "checkpoint": {
            "current_slug": None if not current_reached else current_reached[-1].slug,
            "resulting_slug": None if not next_reached else next_reached[-1].slug,
        },
        "compatible_expeditions": _compatible_slugs(database, proposal["journey"]["user_id"], route_id, proposed),
        "conflicting_expeditions": conflicting_receivers(
            database, proposal["journey"]["user_id"], route_id, proposed, None
        ),
        "lifecycle": None if lifecycle == "none" else lifecycle,
        "will_resume": will_resume,
        "will_complete": will_complete,
        "journey_id": journey_id,
    }


def _compatible_slugs(database, user_id: int, route_id: int, proposed: Policy) -> list[str]:
    rows = database.execute(
        """
        SELECT expedition.id, expedition.slug
        FROM expedition_participations AS participation
        JOIN expeditions AS expedition ON expedition.id = participation.expedition_id
        WHERE participation.user_id = ?
          AND participation.status = 'active'
          AND expedition.status = 'active'
          AND expedition.route_id = ?
        ORDER BY expedition.slug
        """,
        (user_id, route_id),
    ).fetchall()
    return [
        row["slug"]
        for row in rows
        if compatible(load_expedition_policy(database, row["id"]), proposed)
    ]


def assert_join(
    database,
    *,
    user_id: int,
    expedition_id: int | None,
    journey_id: int,
    route_id: int,
    prior_status: str,
    confirmation_token: str | None,
    operation: str,
    proposed: Policy | None = None,
) -> dict:
    """Require a matching policy confirmation before a narrowing or expansion write.

    Returns ``{"change": False}`` when the journey policy stays as it is.
    A mismatch raises and the caller rolls the transaction back.
    """
    from .activities import propose_policy_allocation

    current = load_journey_policy(database, journey_id)
    if proposed is None:
        proposed = load_expedition_policy(database, expedition_id)
    relation = "expand" if operation == "expand" else classify(current, proposed)
    if operation != "expand" and relation == "share":
        return {"change": False, "policy": current, "complete": False}
    if operation != "expand" and relation == "conflict":
        raise PolicyError(
            "activity_policy_conflict",
            "That expedition does not fit this journey's activity rules.",
            status=409,
            **conflict_body(
                current,
                rejected_types(current, proposed),
                [expedition_brief(database, expedition_id)] if expedition_id else [],
            ),
        )
    if operation == "expand":
        state = expansion_state(database, journey_id)
        if not state["available"]:
            if state["reason"] == "incompatible_participation":
                raise PolicyError(
                    "activity_policy_conflict",
                    "Another expedition still limits this journey.",
                    status=409,
                    **conflict_body(current, [], [{"slug": slug} for slug in state["blocked_by"]]),
                )
            raise PolicyError(
                "invalid_state",
                "This journey cannot change activity rules.",
                status=409,
            )
        proposed = UNRESTRICTED
    else:
        blocked = conflicting_receivers(database, user_id, route_id, proposed, expedition_id)
        if blocked:
            raise PolicyError(
                "activity_policy_conflict",
                "Another expedition does not accept that activity list.",
                status=409,
                **conflict_body(current, rejected_types(current, proposed), blocked),
            )
    proposal = propose_policy_allocation(database, journey_id, proposed)
    would_finish = proposal["would_finish"]
    if prior_status in ("paused", "abandoned") and would_finish:
        raise PolicyError(
            "resume_required",
            "Resume this journey before it can be completed.",
            status=409,
            journey_id=journey_id,
            journey_status=prior_status,
        )
    if prior_status == "completed":
        raise PolicyError(
            "invalid_state",
            "A completed journey cannot change activity rules.",
            status=409,
        )
    lifecycle = "none"
    will_resume = prior_status == "paused"
    will_complete = prior_status == "active" and would_finish
    if will_complete:
        lifecycle = "complete"
    elif will_resume:
        lifecycle = "resume"
    requirements = proposal["requirements"]
    participations = [
        item
        for item in active_expedition_ids(database, user_id, journey_id)
        if item != expedition_id
    ]
    from .allocation import policy_allocation_fingerprint

    binding = {
        "user_id": user_id,
        "journey_id": journey_id,
        "operation": operation,
        "expedition_id": None if operation in ("create", "expand", "type") else expedition_id,
        "current": current,
        "proposed": proposed,
        "participation_ids": participations,
        "lifecycle": lifecycle,
        "allocation": policy_allocation_fingerprint(
            proposal["slots"], proposal["shares"], requirements
        ),
        "totals": totals_text(requirements, proposal["resulting"]),
        "progress_version": proposal["journey"]["progress_version"],
    }
    if token_matches(confirmation_token, **binding):
        return {"change": True, "policy": proposed, "complete": will_complete}
    replacement, _expires = issue_token(**binding)
    code = "stale_preview" if confirmation_token else "confirmation_required"
    message = (
        "That activity-rule preview is out of date."
        if confirmation_token
        else "Confirm the activity-rule change before continuing."
    )
    raise PolicyError(
        code,
        message,
        status=409,
        policy_preview=policy_preview_body(
            database,
            operation=operation,
            journey_id=journey_id,
            route_id=route_id,
            current=current,
            proposed=proposed,
            proposal=proposal,
            lifecycle=lifecycle,
            will_resume=will_resume,
            will_complete=will_complete,
        ),
        confirmation_token=replacement,
    )


def _secret() -> bytes:
    secret = current_app.config.get("SECRET_KEY")
    if isinstance(secret, str) and secret:
        return secret.encode("utf-8")
    if isinstance(secret, bytes) and secret:
        return secret
    raise RuntimeError("SECRET_KEY is not configured.")
