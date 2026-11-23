"""Curated chapters, field journal, and arrival acknowledgments.

Reach comes from the journey's capped personal totals. This module does not
award credits, complete a journey, or write expedition feed rows. Reads take
no locks. An acknowledgment locks the acting user, then that journey.

A journal read is one repeatable-read, read-only transaction, so progress,
stories, and acknowledgments come from the same snapshot. That setting ends
with the transaction. Later work on the connection stays read committed.

Arrival lists for an activity write, and pending titles for home, are selects
on the caller's transaction. They do not acknowledge a story or take a lock.
"""

from __future__ import annotations

import re
from pathlib import Path

from .db import get_db
from .journey_progress import ProgressError, read_journey_progress

_PHOTOS_ROOT = Path(__file__).resolve().parent / "static" / "photos"
_PHOTO_PREFIX = "/static/photos/"
_PHOTO_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_STORY_MISSING = "That story does not exist."
_JOURNEY_MISSING = "That journey does not exist."
_FORBIDDEN = "You cannot open another person's journey."


class NarrativeError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def arrival_reveals(database, journey_id: int, checkpoints) -> list[dict]:
    """Short published reveals for checkpoints this write newly reached.

    ``checkpoints`` stays in route position order. A checkpoint with no
    published story is left out. The acknowledgment flag is the row that
    already exists. This does not insert one.
    """
    return _arrival_rows(database, journey_id, [mark.id for mark in checkpoints], pending=False)


def pending_arrivals(database, journey_id: int, checkpoints) -> list[dict]:
    """Unacknowledged published titles among checkpoints already reached.

    Position order. Titles only. This does not write.
    """
    reached = [mark.id for mark in checkpoints if mark.reached]
    return _arrival_rows(database, journey_id, reached, pending=True)


def _arrival_rows(database, journey_id: int, checkpoint_ids: list[int], *, pending: bool) -> list[dict]:
    if not checkpoint_ids:
        return []
    stories = database.execute(
        """
        SELECT story.checkpoint_id, story.arrival_title, story.arrival_body,
               checkpoint.slug AS checkpoint_slug,
               chapter.slug AS chapter_slug
        FROM checkpoint_stories AS story
        JOIN route_checkpoints AS checkpoint
          ON checkpoint.id = story.checkpoint_id
         AND checkpoint.route_id = story.route_id
        LEFT JOIN route_chapters AS chapter
          ON chapter.id = checkpoint.chapter_id
         AND chapter.status = 'published'
        WHERE story.checkpoint_id = ANY(?)
          AND story.status = 'published'
        """,
        (checkpoint_ids,),
    ).fetchall()
    by_id = {row["checkpoint_id"]: row for row in stories}
    seen = {
        row["checkpoint_id"]
        for row in database.execute(
            """
            SELECT checkpoint_id
            FROM journey_story_views
            WHERE journey_id = ? AND checkpoint_id = ANY(?)
            """,
            (journey_id, checkpoint_ids),
        ).fetchall()
    }
    rows = []
    for checkpoint_id in checkpoint_ids:
        story = by_id.get(checkpoint_id)
        if story is None:
            continue
        acknowledged = checkpoint_id in seen
        if pending:
            if acknowledged:
                continue
            rows.append(
                {
                    "checkpoint_slug": story["checkpoint_slug"],
                    "chapter_slug": story["chapter_slug"],
                    "arrival_title": story["arrival_title"],
                }
            )
            continue
        rows.append(
            {
                "checkpoint_slug": story["checkpoint_slug"],
                "chapter_slug": story["chapter_slug"],
                "arrival_title": story["arrival_title"],
                "arrival_body": story["arrival_body"],
                "acknowledged": acknowledged,
            }
        )
    return rows


def chapters_for_routes(database, route_ids: list[int]) -> dict[int, list[dict]]:
    """Published chapters and their checkpoint slugs. Selects only."""
    if not route_ids:
        return {}
    rows = database.execute(
        """
        SELECT chapter.route_id, chapter.id, chapter.slug, chapter.title,
               chapter.position, checkpoint.slug AS checkpoint_slug
        FROM route_chapters AS chapter
        LEFT JOIN route_checkpoints AS checkpoint
          ON checkpoint.chapter_id = chapter.id
         AND checkpoint.route_id = chapter.route_id
        WHERE chapter.route_id = ANY(?)
          AND chapter.status = 'published'
        ORDER BY chapter.route_id, chapter.position, chapter.id,
                 checkpoint.position, checkpoint.id
        """,
        (route_ids,),
    ).fetchall()
    grouped: dict[int, list[dict]] = {}
    index: dict[int, dict] = {}
    for row in rows:
        chapter = index.get(row["id"])
        if chapter is None:
            chapter = {
                "slug": row["slug"],
                "title": row["title"],
                "position": row["position"],
                "checkpoints": [],
            }
            index[row["id"]] = chapter
            grouped.setdefault(row["route_id"], []).append(chapter)
        if row["checkpoint_slug"] is not None:
            chapter["checkpoints"].append(row["checkpoint_slug"])
    return grouped


def journal_timeline(actor_id: int, journey_id: int) -> dict:
    """Reached published stories for one journey. The read commits nothing."""
    database = get_db()
    try:
        _begin_consistent_read(database)
        _require_actor(database, actor_id, lock=False)
        progress = _own_progress(database, actor_id, journey_id)
        return _timeline(database, progress)
    except NarrativeError:
        raise
    finally:
        database.rollback()


def journal_story(actor_id: int, journey_id: int, checkpoint_slug: str) -> dict:
    """One unlocked published story. The read commits nothing."""
    database = get_db()
    try:
        _begin_consistent_read(database)
        _require_actor(database, actor_id, lock=False)
        progress = _own_progress(database, actor_id, journey_id)
        checkpoint, story = _open_story(database, progress, checkpoint_slug)
        return _story_payload(database, progress.journey_id, checkpoint, story)
    except NarrativeError:
        raise
    finally:
        database.rollback()


def acknowledge_arrival(actor_id: int, journey_id: int, checkpoint_slug: str) -> dict:
    """Record that this attempt has seen the arrival. A second call keeps the first time."""
    database = get_db()
    try:
        _require_actor(database, actor_id, lock=True)
        journey = _lock_own_journey(database, actor_id, journey_id)
        progress = _own_progress(database, actor_id, journey_id)
        checkpoint, _story = _open_story(database, progress, checkpoint_slug)
        existing = _view(database, journey["id"], checkpoint["id"])
        if existing is not None:
            acknowledged_at = existing["acknowledged_at"]
            database.rollback()
            return _ack_payload(journey["id"], checkpoint["slug"], acknowledged_at)
        inserted = database.execute(
            """
            INSERT INTO journey_story_views (
                journey_id, user_id, route_id, checkpoint_id
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT (journey_id, checkpoint_id) DO NOTHING
            RETURNING acknowledged_at
            """,
            (journey["id"], journey["user_id"], journey["route_id"], checkpoint["id"]),
        ).fetchone()
        if inserted is None:
            existing = _view(database, journey["id"], checkpoint["id"])
            database.rollback()
            if existing is None:
                raise NarrativeError("conflict", "The acknowledgment changed during this update.", status=409)
            return _ack_payload(journey["id"], checkpoint["slug"], existing["acknowledged_at"])
        acknowledged_at = inserted["acknowledged_at"]
        database.commit()
        return _ack_payload(journey["id"], checkpoint["slug"], acknowledged_at)
    except NarrativeError:
        database.rollback()
        raise
    except Exception:
        database.rollback()
        raise


def _timeline(database, progress) -> dict:
    reached = {mark.id for mark in progress.checkpoints if mark.reached}
    stories = database.execute(
        """
        SELECT story.checkpoint_id, story.arrival_title, story.arrival_body,
               (story.journal_title IS NOT NULL) AS has_journal,
               checkpoint.slug AS checkpoint_slug, checkpoint.name AS checkpoint_name,
               checkpoint.position,
               chapter.slug AS chapter_slug, chapter.title AS chapter_title,
               chapter.position AS chapter_position
        FROM checkpoint_stories AS story
        JOIN route_checkpoints AS checkpoint
          ON checkpoint.id = story.checkpoint_id
         AND checkpoint.route_id = story.route_id
        LEFT JOIN route_chapters AS chapter
          ON chapter.id = checkpoint.chapter_id
         AND chapter.status = 'published'
        WHERE story.route_id = ?
          AND story.status = 'published'
        ORDER BY checkpoint.position, checkpoint.id
        """,
        (progress.route_id,),
    ).fetchall()
    counts = {
        row["checkpoint_id"]: row["discovery_count"]
        for row in database.execute(
            """
            SELECT checkpoint_id, COUNT(*) AS discovery_count
            FROM story_discoveries
            WHERE route_id = ? AND status = 'published'
            GROUP BY checkpoint_id
            """,
            (progress.route_id,),
        ).fetchall()
    }
    seen = {
        row["checkpoint_id"]
        for row in database.execute(
            """
            SELECT checkpoint_id
            FROM journey_story_views
            WHERE journey_id = ?
            """,
            (progress.journey_id,),
        ).fetchall()
    }
    chapters: list[dict] = []
    for row in stories:
        if row["checkpoint_id"] not in reached:
            continue
        entry = {
            "checkpoint_slug": row["checkpoint_slug"],
            "checkpoint_name": row["checkpoint_name"],
            "position": row["position"],
            "arrival_title": row["arrival_title"],
            "arrival_body": row["arrival_body"],
            "has_journal": bool(row["has_journal"]),
            "discovery_count": counts.get(row["checkpoint_id"], 0),
            "acknowledged": row["checkpoint_id"] in seen,
        }
        key = None if row["chapter_slug"] is None else row["chapter_slug"]
        if chapters and chapters[-1]["_key"] == key:
            chapters[-1]["entries"].append(entry)
            continue
        chapters.append(
            {
                "_key": key,
                "slug": row["chapter_slug"],
                "title": row["chapter_title"],
                "position": row["chapter_position"],
                "entries": [entry],
            }
        )
    for chapter in chapters:
        chapter.pop("_key")
    return {
        "journey_id": progress.journey_id,
        "status": progress.status,
        "chapters": chapters,
    }


def _story_payload(database, journey_id: int, checkpoint: dict, story: dict) -> dict:
    discoveries = database.execute(
        """
        SELECT id, slug, kind, title, body, position
        FROM story_discoveries
        WHERE checkpoint_id = ? AND status = 'published'
        ORDER BY position, id
        """,
        (checkpoint["id"],),
    ).fetchall()
    media_rows = database.execute(
        """
        SELECT discovery_id, role, asset_path, alt_text, rights_basis,
               author, license, license_url, source_url, position
        FROM route_media
        WHERE checkpoint_id = ?
          AND status = 'published'
          AND role IN ('journal', 'discovery')
        ORDER BY position, id
        """,
        (checkpoint["id"],),
    ).fetchall()
    by_discovery: dict[int, list[dict]] = {}
    journal_media = []
    for row in media_rows:
        payload = _media_payload(row)
        if payload is None:
            continue
        if row["role"] == "discovery" and row["discovery_id"] is not None:
            by_discovery.setdefault(row["discovery_id"], []).append(payload)
        elif row["role"] == "journal":
            journal_media.append(payload)
    acknowledged = _view(database, journey_id, checkpoint["id"]) is not None
    journal = None
    if story["journal_title"] is not None:
        journal = {"title": story["journal_title"], "body": story["journal_body"]}
    return {
        "journey_id": journey_id,
        "checkpoint_slug": checkpoint["slug"],
        "chapter_slug": checkpoint["chapter_slug"],
        "arrival_title": story["arrival_title"],
        "arrival_body": story["arrival_body"],
        "journal": journal,
        "discoveries": [
            {
                "slug": row["slug"],
                "kind": row["kind"],
                "title": row["title"],
                "body": row["body"],
                "position": row["position"],
                "media": by_discovery.get(row["id"], []),
            }
            for row in discoveries
        ],
        "media": journal_media,
        "acknowledged": acknowledged,
    }


def _begin_consistent_read(database) -> None:
    """One snapshot for every select in this read. Does not change the session default."""
    database.rollback()
    database.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")


def _media_payload(row: dict) -> dict | None:
    if not _asset_is_readable(row["asset_path"]):
        return None
    payload = {
        "role": row["role"],
        "path": row["asset_path"],
        "alt_text": row["alt_text"],
        "rights_basis": row["rights_basis"],
        "license": row["license"],
        "position": row["position"],
    }
    for key in ("author", "license_url", "source_url"):
        if row[key] is not None:
            payload[key] = row[key]
    return payload


def _asset_is_readable(asset_path: str) -> bool:
    """True when the path is a real file inside the curated photos directory.

    Returned paths are site-relative. The client resolves them against the
    backend origin. A remote URL, a path outside /static/photos/, a missing
    file, a symlink that leaves that directory, or a symlink loop is omitted.
    """
    relative = _photo_relative(asset_path)
    if relative is None:
        return False
    try:
        root = _PHOTOS_ROOT.resolve()
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            return False
        return candidate.is_file()
    except (OSError, RuntimeError):
        return False


def _photo_relative(asset_path: str) -> str | None:
    if not isinstance(asset_path, str) or asset_path != asset_path.strip():
        return None
    if not asset_path.startswith(_PHOTO_PREFIX):
        return None
    relative = asset_path[len(_PHOTO_PREFIX) :]
    segments = relative.split("/")
    if not segments or any(_PHOTO_SEGMENT.fullmatch(segment) is None for segment in segments):
        return None
    return relative


def _open_story(database, progress, checkpoint_slug: str) -> tuple[dict, dict]:
    if not isinstance(checkpoint_slug, str) or not checkpoint_slug.strip():
        raise NarrativeError("not_found", _STORY_MISSING, status=404)
    checkpoint = database.execute(
        """
        SELECT checkpoint.id, checkpoint.slug, chapter.slug AS chapter_slug
        FROM route_checkpoints AS checkpoint
        LEFT JOIN route_chapters AS chapter
          ON chapter.id = checkpoint.chapter_id
         AND chapter.status = 'published'
        WHERE checkpoint.route_id = ? AND checkpoint.slug = ?
        """,
        (progress.route_id, checkpoint_slug),
    ).fetchone()
    if checkpoint is None or not any(
        mark.id == checkpoint["id"] and mark.reached for mark in progress.checkpoints
    ):
        raise NarrativeError("not_found", _STORY_MISSING, status=404)
    story = database.execute(
        """
        SELECT arrival_title, arrival_body, journal_title, journal_body
        FROM checkpoint_stories
        WHERE checkpoint_id = ? AND route_id = ? AND status = 'published'
        """,
        (checkpoint["id"], progress.route_id),
    ).fetchone()
    if story is None:
        raise NarrativeError("not_found", _STORY_MISSING, status=404)
    return checkpoint, story


def _own_progress(database, actor_id: int, journey_id: int):
    try:
        progress = read_journey_progress(database, journey_id)
    except ProgressError as exc:
        raise _from_progress(exc) from exc
    if progress.user_id != actor_id:
        raise NarrativeError("forbidden", _FORBIDDEN, status=403)
    return progress


def _lock_own_journey(database, actor_id: int, journey_id: int) -> dict:
    if not isinstance(journey_id, int) or isinstance(journey_id, bool) or journey_id <= 0:
        raise NarrativeError("not_found", _JOURNEY_MISSING, status=404)
    journey = database.execute(
        """
        SELECT id, user_id, route_id, status, progress_version
        FROM route_journeys
        WHERE id = ? AND user_id = ?
        FOR UPDATE
        """,
        (journey_id, actor_id),
    ).fetchone()
    if journey is not None:
        return journey
    other = database.execute(
        "SELECT id FROM route_journeys WHERE id = ?",
        (journey_id,),
    ).fetchone()
    if other is None:
        raise NarrativeError("not_found", _JOURNEY_MISSING, status=404)
    raise NarrativeError("forbidden", _FORBIDDEN, status=403)


def _view(database, journey_id: int, checkpoint_id: int):
    return database.execute(
        """
        SELECT acknowledged_at
        FROM journey_story_views
        WHERE journey_id = ? AND checkpoint_id = ?
        """,
        (journey_id, checkpoint_id),
    ).fetchone()


def _ack_payload(journey_id: int, checkpoint_slug: str, acknowledged_at) -> dict:
    return {
        "journey_id": journey_id,
        "checkpoint_slug": checkpoint_slug,
        "acknowledged_at": acknowledged_at,
    }


def _require_actor(database, actor_id: int, *, lock: bool) -> None:
    if not isinstance(actor_id, int) or isinstance(actor_id, bool) or actor_id <= 0:
        raise NarrativeError("not_found", "That account does not exist.", status=404)
    locking = " FOR UPDATE" if lock else ""
    row = database.execute(
        f"SELECT id, deletion_state FROM users WHERE id = ?{locking}",
        (actor_id,),
    ).fetchone()
    if row is None:
        raise NarrativeError("not_found", "That account does not exist.", status=404)
    if row["deletion_state"] == "deleting":
        raise NarrativeError("forbidden", "This account is being deleted.", status=403)


def _from_progress(exc: ProgressError) -> NarrativeError:
    status = {"not_found": 404, "forbidden": 403}.get(exc.code, 409)
    return NarrativeError(exc.code, str(exc), status=status)
