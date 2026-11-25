"""Read private awards from their snapshots and acknowledge automatic reveals."""

from datetime import datetime

from flask import current_app
from itsdangerous import BadData, URLSafeSerializer

from .db import get_db


class TrophyCaseError(Exception):
    def __init__(self, code, message, status):
        super().__init__(message)
        self.code = code
        self.status = status


def _missing():
    return TrophyCaseError("not_found", "That award does not exist.", 404)


def _collection_key(row):
    if row["edition_id"] is None and row["award_snapshot"]["scope"] == "route":
        return row["route_group_key"]
    return None


def award_summary(row, *, reveal_pending):
    snapshot = row["award_snapshot"]
    presentation = snapshot.get("presentation") or {}
    collection = _collection_key(row)
    edition = None
    if row["edition_id"] is not None:
        edition = {
            "slug": snapshot["edition_slug"], "size": row["edition_size"],
            "sequence_number": row["sequence_number"],
        }
    return {
        "id": row["id"], "definition_slug": snapshot["definition_slug"],
        "name": snapshot["name"], "description": snapshot["description"], "scope": snapshot["scope"],
        "issued_at": row["issued_at"], "reveal_pending": reveal_pending,
        "completion_number": row["completion_number"],
        "completed_at": row["event_at"] if row["completion_number"] is not None else None,
        "presentation": {
            key: value for key, value in presentation.items()
            if key in ("badge", "icon", "asset_ref", "accent_color", "variant")
            and isinstance(value, str) and len(value) <= 256
        },
        "edition": edition, "collection_key": collection,
        "section": "limited_editions" if edition else "route_collections" if collection else "individual",
    }


_SELECT = """
    SELECT award.*, reveal.acknowledged_at,
           (reveal.earned_achievement_id IS NOT NULL AND reveal.acknowledged_at IS NULL) AS pending
    FROM earned_achievements AS award
    LEFT JOIN achievement_reveals AS reveal ON reveal.earned_achievement_id = award.id
"""


def _summary(row):
    return award_summary(row, reveal_pending=row["pending"])


def home_summary(database, user_id):
    totals = database.execute(
        """
        SELECT count(*) AS earned_count,
               count(reveal.earned_achievement_id) FILTER (WHERE reveal.acknowledged_at IS NULL)
                   AS pending_reveal_count
        FROM earned_achievements AS award
        LEFT JOIN achievement_reveals AS reveal ON reveal.earned_achievement_id = award.id
        WHERE award.user_id = ?
        """,
        (user_id,),
    ).fetchone()
    rows = database.execute(
        _SELECT + """
        WHERE award.user_id = ? AND reveal.earned_achievement_id IS NOT NULL
          AND reveal.acknowledged_at IS NULL
        ORDER BY award.issued_at DESC, award.id DESC LIMIT 3
        """,
        (user_id,),
    ).fetchall()
    return {**totals, "pending_awards": [_summary(row) for row in rows]}


def list_awards(user_id, *, limit, cursor, collection_key, pending_only):
    signer = URLSafeSerializer(current_app.secret_key, salt="trophy-case-cursor-v1")
    filters = [user_id, collection_key, pending_only]
    before = (None, None)
    if cursor:
        try:
            owner_filters, timestamp, award_id = signer.loads(cursor)
            if owner_filters != filters:
                raise ValueError
            before = (datetime.fromisoformat(timestamp), award_id)
        except (BadData, TypeError, ValueError):
            raise TrophyCaseError("invalid", "Invalid trophy case cursor.", 400) from None
    database = get_db()
    try:
        database.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        rows = database.execute(
            _SELECT + """
            WHERE award.user_id = ?
              AND (NOT ? OR (reveal.earned_achievement_id IS NOT NULL AND reveal.acknowledged_at IS NULL))
              AND (?::bigint IS NULL OR
                  (award.route_group_key = ? AND award.edition_id IS NULL
                   AND award.award_snapshot ->> 'scope' = 'route'))
              AND (?::timestamptz IS NULL OR (award.issued_at, award.id) < (?, ?))
            ORDER BY award.issued_at DESC, award.id DESC LIMIT ?
            """,
            (user_id, pending_only, collection_key, collection_key, before[0], *before, limit + 1),
        ).fetchall()
        page = rows[:limit]
        keys = sorted({key for row in page if (key := _collection_key(row)) is not None})
        collections = database.execute(
            """
            WITH grouped AS (
                SELECT route_group_key, count(*) AS award_count
                FROM earned_achievements
                WHERE user_id = ? AND route_group_key = ANY(?) AND edition_id IS NULL
                  AND award_snapshot ->> 'scope' = 'route'
                GROUP BY route_group_key
            )
            SELECT grouped.route_group_key AS collection_key, grouped.award_count,
                   latest.context_snapshot -> 'route' ->> 'name' AS title,
                   (SELECT count(*) FROM route_completions
                    WHERE user_id = ? AND route_key = grouped.route_group_key) AS completion_count
            FROM grouped
            CROSS JOIN LATERAL (
                SELECT context_snapshot, issued_at, id FROM earned_achievements
                WHERE user_id = ? AND route_group_key = grouped.route_group_key AND edition_id IS NULL
                  AND award_snapshot ->> 'scope' = 'route'
                ORDER BY issued_at DESC, id DESC LIMIT 1
            ) AS latest
            ORDER BY latest.issued_at DESC, latest.id DESC
            """,
            (user_id, keys, user_id, user_id),
        ).fetchall() if keys else []
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = signer.dumps([filters, last["issued_at"].isoformat(), last["id"]])
        return {"awards": [_summary(row) for row in page],
                "route_collections": collections, "next_cursor": next_cursor}
    finally:
        database.rollback()


def award_detail(user_id, award_id):
    database = get_db()
    try:
        row = database.execute(
            _SELECT + " WHERE award.user_id = ? AND award.id = ?", (user_id, award_id),
        ).fetchone()
        if row is None:
            raise _missing()
        fields = {"route": ("slug", "name"), "expedition": ("slug", "name"),
                  "activity": ("activity_type", "activity_date"), "organization": ("slug", "name")}
        context = {
            kind: {key: value for key, value in row["context_snapshot"][kind].items() if key in allowed}
            for kind, allowed in fields.items() if isinstance(row["context_snapshot"].get(kind), dict)
        }
        return {**_summary(row), "context": context, "presentation": row["award_snapshot"].get("presentation"),
                "reveal_acknowledged_at": row["acknowledged_at"]}
    finally:
        database.rollback()


def acknowledge_reveal(user_id, award_id):
    database = get_db()
    try:
        user = database.execute(
            "SELECT 1 FROM users WHERE id = ? AND deletion_state IS NULL FOR UPDATE", (user_id,)
        ).fetchone()
        if user is None:
            raise _missing()
        row = database.execute(
            """
            SELECT reveal.acknowledged_at
            FROM achievement_reveals AS reveal JOIN earned_achievements AS award
              ON award.id = reveal.earned_achievement_id
            WHERE award.user_id = ? AND award.id = ?
            """,
            (user_id, award_id),
        ).fetchone()
        if row is None:
            raise _missing()
        if row["acknowledged_at"] is None:
            row = database.execute(
                """
                UPDATE achievement_reveals SET acknowledged_at = clock_timestamp()
                WHERE earned_achievement_id = ? RETURNING acknowledged_at
                """,
                (award_id,),
            ).fetchone()
        database.commit()
        return row
    except Exception:
        database.rollback()
        raise
