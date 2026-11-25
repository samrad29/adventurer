"""Validate and import one route's curated narrative.

GitHub holds the package. PostgreSQL holds the rows the journal API reads.
The importer writes chapters, stories, discoveries, media, checkpoint
chapter assignments, and route recommendations. It does not write route requirements, checkpoint
thresholds, checkpoint positions, journeys, credits, standings, or
arrival acknowledgments.

    python -m adventurer.content_pipeline validate content/routes/<slug>
    python -m adventurer.content_pipeline validate content/routes/<slug> --database
    python -m adventurer.content_pipeline import content/routes/<slug> --dry-run
    python -m adventurer.content_pipeline import content/routes/<slug>

The database commands read TEST_DATABASE_URL and refuse every other target.
They do not read DATABASE_URL. A license string that matches the allowlist
is not proof that the license is authentic.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from adventurer.allocation import SUPPORTED_TYPES
from adventurer.db import DB
from adventurer.narrative import _asset_is_readable, _photo_relative
from adventurer.route_recommendations import recommendations_for_routes

FORMAT_VERSION = 1
SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
STATUSES = frozenset({"draft", "published", "retired"})
KINDS = frozenset({"cultural", "historical", "geographic", "culinary", "photographic"})
ROLES = frozenset({"route_hero", "checkpoint_scene", "journal", "discovery"})
RIGHTS = frozenset(
    {"public_domain", "cc0", "creative_commons", "commissioned", "undocumented"}
)
CC_LICENSES = frozenset(
    {
        "CC BY 2.0",
        "CC BY-SA 2.0",
        "CC BY 2.5",
        "CC BY-SA 2.5",
        "CC BY 3.0",
        "CC BY-SA 3.0",
        "CC BY 4.0",
        "CC BY-SA 4.0",
    }
)
ROOT_FILES = frozenset({"manifest.json", "chapters.json", "media.json", "checkpoints"})
CHAPTER_FIELDS = ("title", "summary", "position", "status")
STORY_FIELDS = ("arrival_title", "arrival_body", "journal_title", "journal_body", "status")
DISCOVERY_FIELDS = ("kind", "title", "body", "position", "status")
MEDIA_FIELDS = (
    "asset_path",
    "alt_text",
    "rights_basis",
    "author",
    "license",
    "license_url",
    "source_url",
    "permission_note",
    "status",
)
KIND_ORDER = {"chapter": 0, "assignment": 1, "story": 2, "discovery": 3, "media": 4, "recommendations": 5}
RIGHTS_NOTE = "matching text is not proof the license is authentic"


@dataclass(frozen=True)
class ContentIssue:
    file: str
    record: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}: {self.record}: {self.field}: {self.message}"


class ContentRejected(Exception):
    def __init__(self, issues: list[ContentIssue]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(issue) for issue in issues))


@dataclass(frozen=True)
class ChapterDoc:
    slug: str
    title: str
    summary: str | None
    position: int
    status: str


@dataclass(frozen=True)
class DiscoveryDoc:
    slug: str
    kind: str
    title: str
    body: str
    position: int
    status: str


@dataclass(frozen=True)
class CheckpointDoc:
    slug: str
    status: str
    chapter: str | None
    arrival_title: str
    arrival_body: str
    journal_title: str | None
    journal_body: str | None
    discoveries: tuple[DiscoveryDoc, ...]


@dataclass(frozen=True)
class MediaDoc:
    role: str
    checkpoint: str | None
    discovery: str | None
    path: str
    alt_text: str
    rights_basis: str
    author: str | None
    license: str | None
    license_url: str | None
    source_url: str | None
    permission_note: str | None
    position: int
    status: str

    def identity(self) -> tuple[str, str, str, int]:
        return (self.role, self.checkpoint or "", self.discovery or "", self.position)


@dataclass(frozen=True)
class Package:
    root: Path
    route_slug: str
    chapters: tuple[ChapterDoc, ...]
    checkpoints: tuple[CheckpointDoc, ...]
    media: tuple[MediaDoc, ...]
    recommended_activity_types: tuple[str, ...]


@dataclass
class Catalog:
    route_id: int
    checkpoints: dict[str, dict]
    chapters: dict[str, dict]
    stories: dict[int, dict]
    discoveries: dict[tuple[int, str], dict]
    media: list[dict]
    recommended_activity_types: tuple[str, ...]


@dataclass(frozen=True)
class Change:
    action: str
    kind: str
    identity: str
    fields: tuple[str, ...] = ()
    record_id: int | None = None
    checkpoint_id: int | None = None
    values: tuple = ()


def validate_content(root: Path, database: DB | None = None) -> list[ContentIssue]:
    """Read a package and return every problem. This writes nothing."""
    package, issues = _load_package(root)
    if package is None:
        return issues
    structural = _structural_issues(package)
    issues.extend(structural)
    if database is None:
        return issues
    database.rollback()
    try:
        catalog, existence = _read_catalog(database, package)
        issues.extend(existence)
        if catalog is not None and not structural:
            issues.extend(_catalog_issues(package, catalog))
        return issues
    finally:
        database.rollback()


def import_route(database: DB, root: Path, *, dry_run: bool = False) -> list[Change]:
    """Validate, then upsert one route in a single transaction.

    A dry run returns the plan and rolls back. Any failure rolls the
    transaction back, including a failure on the last row.
    """
    package, issues = _load_package(root)
    if package is None:
        raise ContentRejected(issues)
    issues.extend(_structural_issues(package))
    if issues:
        raise ContentRejected(issues)
    database.rollback()
    try:
        catalog, issues = _read_catalog(database, package, lock=not dry_run)
        if catalog is not None:
            issues.extend(_catalog_issues(package, catalog))
        if issues or catalog is None:
            raise ContentRejected(issues)
        plan = _plan(package, catalog)
        if dry_run:
            database.rollback()
            return plan
        _apply(database, catalog, plan)
        database.execute(
            """
            SET CONSTRAINTS
                adventurer.route_chapter_spans,
                adventurer.route_chapter_spans_chapters
            IMMEDIATE
            """
        )
        database.commit()
        return plan
    except ContentRejected:
        database.rollback()
        raise
    except Exception as exc:
        database.rollback()
        message = getattr(getattr(exc, "diag", None), "message_primary", None) or str(exc)
        if (
            "chapter checkpoints must form a contiguous span" in message
            or "chapter order must follow checkpoint position" in message
        ):
            raise ContentRejected(
                [
                    ContentIssue(
                        "chapters.json",
                        package.route_slug,
                        "chapter",
                        message,
                    )
                ]
            ) from exc
        raise


def format_plan(plan: list[Change]) -> str:
    if not plan:
        return "no editorial changes"
    lines = []
    for change in plan:
        suffix = f" ({', '.join(change.fields)})" if change.fields else ""
        lines.append(f"{change.action} {change.kind} {change.identity}{suffix}")
    return "\n".join(lines)


def test_database_url() -> str:
    """Isolated local database. DATABASE_URL is never read."""
    raw = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not raw:
        raise ContentRejected(
            [
                ContentIssue(
                    "TEST_DATABASE_URL",
                    "database",
                    "url",
                    "Set TEST_DATABASE_URL to the isolated database "
                    "adventurer_migration_test. This tool does not read DATABASE_URL.",
                )
            ]
        )
    if "supabase" in raw.lower() or "render.com" in raw.lower():
        raise ContentRejected(
            [
                ContentIssue(
                    "TEST_DATABASE_URL",
                    "database",
                    "url",
                    "Refusing a hosted database URL.",
                )
            ]
        )
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost"}:
        raise ContentRejected(
            [
                ContentIssue(
                    "TEST_DATABASE_URL",
                    "database",
                    "url",
                    f"Refusing database host {host!r}. Imports run only against localhost.",
                )
            ]
        )
    database = parsed.path.lstrip("/").split("/", 1)[0]
    if database != "adventurer_migration_test":
        raise ContentRejected(
            [
                ContentIssue(
                    "TEST_DATABASE_URL",
                    "database",
                    "url",
                    f"Refusing database {database!r}. Use adventurer_migration_test.",
                )
            ]
        )
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    if not raw.startswith("postgresql://"):
        raise ContentRejected(
            [
                ContentIssue(
                    "TEST_DATABASE_URL",
                    "database",
                    "url",
                    "TEST_DATABASE_URL must be a PostgreSQL URL.",
                )
            ]
        )
    return raw


def open_database() -> DB:
    import psycopg
    from psycopg.rows import dict_row

    connection = psycopg.connect(
        test_database_url(),
        row_factory=dict_row,
        prepare_threshold=None,
        connect_timeout=15,
        options="-c search_path=adventurer",
    )
    return DB(connection)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m adventurer.content_pipeline",
        description="Validate or import one route narrative package.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Check a package and write nothing.")
    validate.add_argument("path")
    validate.add_argument(
        "--database",
        action="store_true",
        help="Also require the route and checkpoints to exist.",
    )
    import_command = commands.add_parser("import", help="Upsert one route narrative.")
    import_command.add_argument("path")
    import_command.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and roll back.",
    )
    args = parser.parse_args(argv)
    database = None
    try:
        if args.command == "validate":
            if args.database:
                database = open_database()
            issues = validate_content(Path(args.path), database)
            if issues:
                for issue in issues:
                    print(issue, file=sys.stderr)
                return 1
            print("valid")
            return 0
        database = open_database()
        plan = import_route(database, Path(args.path), dry_run=args.dry_run)
        print(format_plan(plan))
        return 0
    except ContentRejected as rejected:
        for issue in rejected.issues:
            print(issue, file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()


def _load_package(root: Path) -> tuple[Package | None, list[ContentIssue]]:
    root = Path(root)
    if not root.is_dir():
        return None, [
            ContentIssue(root.name, "package", "path", "route directory does not exist")
        ]
    issues: list[ContentIssue] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.name not in ROOT_FILES:
            issues.append(
                ContentIssue(child.name, child.name, "path", "unexpected file in the route package")
            )
    manifest, manifest_issues = _read_json(root / "manifest.json")
    issues.extend(manifest_issues)
    route_slug = root.name
    if manifest is not None:
        issues.extend(_manifest_issues(manifest, route_slug))
        if isinstance(manifest, dict) and isinstance(manifest.get("route_slug"), str):
            route_slug = manifest["route_slug"]
    if manifest is None or not isinstance(manifest, dict):
        return None, issues
    recommendations = manifest.get("recommended_activity_types", [])

    chapters, chapter_issues = _load_chapters(root / "chapters.json")
    issues.extend(chapter_issues)
    checkpoints, checkpoint_issues = _load_checkpoints(root / "checkpoints")
    issues.extend(checkpoint_issues)
    media, media_issues = _load_media(root / "media.json")
    issues.extend(media_issues)
    if any(issue.file in {"chapters.json", "media.json"} or issue.file.startswith("checkpoints") for issue in issues):
        if not (root / "chapters.json").is_file() or not (root / "media.json").is_file():
            return None, issues
        if not (root / "checkpoints").is_dir():
            return None, issues
    return (
        Package(
            root=root,
            route_slug=route_slug,
            chapters=tuple(chapters),
            checkpoints=tuple(checkpoints),
            media=tuple(media),
            recommended_activity_types=tuple(recommendations) if isinstance(recommendations, list) else (),
        ),
        issues,
    )


def _manifest_issues(manifest: object, directory: str) -> list[ContentIssue]:
    if not isinstance(manifest, dict):
        return [ContentIssue("manifest.json", "manifest", "json", "manifest must be an object")]
    field = "recommended_activity_types"
    issues = _unknown_keys(manifest, {"format_version", "route_slug", field}, "manifest.json", "manifest")
    types = manifest.get(field, [])
    if not isinstance(types, list) or any(
        not isinstance(item, str) or item not in SUPPORTED_TYPES for item in types
    ):
        issues.append(ContentIssue("manifest.json", "manifest", field, "must be an array of supported activity types"))
    elif len(set(types)) != len(types):
        issues.append(ContentIssue("manifest.json", "manifest", field, "must not contain duplicate activity types"))
    version = manifest.get("format_version")
    if type(version) is not int or version != FORMAT_VERSION:
        issues.append(
            ContentIssue(
                "manifest.json",
                "manifest",
                "format_version",
                f"format_version must be {FORMAT_VERSION}",
            )
        )
    slug = manifest.get("route_slug")
    if not isinstance(slug, str) or SLUG.fullmatch(slug) is None:
        issues.append(
            ContentIssue(
                "manifest.json",
                "manifest",
                "route_slug",
                "route_slug must be a lowercase slug",
            )
        )
    elif slug != directory:
        issues.append(
            ContentIssue(
                "manifest.json",
                slug,
                "route_slug",
                f"route_slug must match the directory name {directory}",
            )
        )
    return issues


def _load_chapters(path: Path) -> tuple[list[ChapterDoc], list[ContentIssue]]:
    payload, issues = _read_json(path)
    if payload is None:
        return [], issues
    if not isinstance(payload, dict):
        return [], issues + [ContentIssue("chapters.json", "chapters", "json", "chapters.json must be an object")]
    issues.extend(_unknown_keys(payload, {"chapters"}, "chapters.json", "chapters"))
    rows = payload.get("chapters")
    if not isinstance(rows, list):
        return [], issues + [ContentIssue("chapters.json", "chapters", "chapters", "chapters must be an array")]
    chapters: list[ChapterDoc] = []
    for index, row in enumerate(rows):
        record = f"chapters[{index}]"
        if not isinstance(row, dict):
            issues.append(ContentIssue("chapters.json", record, "json", "chapter must be an object"))
            continue
        issues.extend(
            _unknown_keys(row, {"slug", "title", "summary", "position", "status"}, "chapters.json", record)
        )
        slug = _slug(row.get("slug"), issues, "chapters.json", record, "slug")
        title = _required_text(row.get("title"), issues, "chapters.json", record, "title")
        summary = _optional_text(row.get("summary"), issues, "chapters.json", record, "summary", present="summary" in row)
        position = _position(row.get("position"), issues, "chapters.json", record)
        status = _status(row.get("status"), issues, "chapters.json", record)
        if None in (slug, title, position, status):
            continue
        chapters.append(ChapterDoc(slug, title, summary, position, status))
    return chapters, issues


def _load_checkpoints(path: Path) -> tuple[list[CheckpointDoc], list[ContentIssue]]:
    issues: list[ContentIssue] = []
    if not path.is_dir():
        return [], [ContentIssue("checkpoints", "checkpoints", "path", "checkpoints directory is required")]
    docs: list[CheckpointDoc] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        relative = f"checkpoints/{child.name}"
        if not child.is_file() or child.suffix != ".md":
            issues.append(ContentIssue(relative, child.name, "path", "checkpoint files must be Markdown"))
            continue
        if SLUG.fullmatch(child.stem) is None:
            issues.append(ContentIssue(relative, child.stem, "slug", "checkpoint file name must be a lowercase slug"))
            continue
        document, document_issues = _parse_checkpoint(child, relative)
        issues.extend(document_issues)
        if document is not None:
            docs.append(document)
    return docs, issues


def _parse_checkpoint(path: Path, relative: str) -> tuple[CheckpointDoc | None, list[ContentIssue]]:
    issues: list[ContentIssue] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [ContentIssue(relative, path.stem, "file", str(exc))]
    split = _split_front_matter(text)
    if split is None:
        return None, [
            ContentIssue(
                relative,
                path.stem,
                "front_matter",
                "checkpoint file must start with a JSON object between --- lines",
            )
        ]
    raw_json, body = split
    try:
        payload = json.loads(raw_json, object_pairs_hook=_no_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        message = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        return None, [ContentIssue(relative, path.stem, "front_matter", message)]
    if not isinstance(payload, dict):
        return None, [ContentIssue(relative, path.stem, "front_matter", "front matter must be an object")]
    record = path.stem
    issues.extend(
        _unknown_keys(
            payload,
            {"status", "chapter", "arrival_title", "journal_title", "discoveries"},
            relative,
            record,
        )
    )
    if "chapter" not in payload:
        issues.append(
            ContentIssue(
                relative,
                record,
                "chapter",
                "chapter is required; use null to leave this checkpoint unassigned",
            )
        )
    status = _status(payload.get("status"), issues, relative, record)
    chapter = _chapter_ref(payload.get("chapter"), issues, relative, record) if "chapter" in payload else None
    arrival_title = _required_text(payload.get("arrival_title"), issues, relative, record, "arrival_title")
    journal_title = _optional_text(
        payload.get("journal_title"),
        issues,
        relative,
        record,
        "journal_title",
        present="journal_title" in payload,
    )
    sections, section_issues = _sections(body, relative, record)
    issues.extend(section_issues)
    arrival_body = sections.get("arrival")
    if arrival_body is None:
        issues.append(ContentIssue(relative, record, "arrival_body", "an Arrival section is required"))
    elif not arrival_body:
        issues.append(ContentIssue(relative, record, "arrival_body", "arrival body is required"))
    journal_body = sections.get("journal")
    if journal_title is None and journal_body is not None:
        issues.append(
            ContentIssue(relative, record, "journal_title", "journal_title is required when a Journal section is present")
        )
    if journal_title is not None and journal_body is None:
        issues.append(
            ContentIssue(relative, record, "journal_body", "a Journal section is required when journal_title is set")
        )
    elif journal_title is not None and not journal_body:
        issues.append(ContentIssue(relative, record, "journal_body", "journal body is required"))
    discoveries, discovery_issues = _discoveries(payload.get("discoveries"), sections, relative, record)
    issues.extend(discovery_issues)
    if None in (status, arrival_title) or not arrival_body:
        return None, issues
    return (
        CheckpointDoc(
            slug=record,
            status=status,
            chapter=chapter,
            arrival_title=arrival_title,
            arrival_body=arrival_body or "",
            journal_title=journal_title,
            journal_body=journal_body if journal_title is not None else None,
            discoveries=tuple(discoveries),
        ),
        issues,
    )


def _discoveries(
    payload: object,
    sections: dict[str, str],
    file: str,
    record: str,
) -> tuple[list[DiscoveryDoc], list[ContentIssue]]:
    issues: list[ContentIssue] = []
    if payload is None:
        payload = []
    if not isinstance(payload, list):
        return [], [ContentIssue(file, record, "discoveries", "discoveries must be an array")]
    found: list[DiscoveryDoc] = []
    attempted: set[str] = set()
    for index, row in enumerate(payload):
        name = f"{record}.discoveries[{index}]"
        if not isinstance(row, dict):
            issues.append(ContentIssue(file, name, "json", "discovery must be an object"))
            continue
        issues.extend(_unknown_keys(row, {"slug", "kind", "title", "position", "status"}, file, name))
        slug = _slug(row.get("slug"), issues, file, name, "slug")
        if slug is not None:
            attempted.add(slug)
        kind = row.get("kind")
        if kind not in KINDS:
            issues.append(
                ContentIssue(file, name, "kind", "kind must be cultural, historical, geographic, culinary, or photographic")
            )
            kind = None
        title = _required_text(row.get("title"), issues, file, name, "title")
        position = _position(row.get("position"), issues, file, name)
        status = _status(row.get("status"), issues, file, name)
        body = sections.get(f"discovery:{slug}") if slug else None
        if slug and body is None:
            issues.append(ContentIssue(file, name, "body", "a Discovery section is required"))
        elif slug and not body:
            issues.append(ContentIssue(file, name, "body", "discovery body is required"))
        if None in (slug, kind, title, position, status) or not body:
            continue
        found.append(DiscoveryDoc(slug, kind, title, body, position, status))
    for key in sections:
        if key.startswith("discovery:") and key.split(":", 1)[1] not in attempted:
            issues.append(
                ContentIssue(file, record, key, "Discovery section has no matching discoveries entry")
            )
    return found, issues


def _load_media(path: Path) -> tuple[list[MediaDoc], list[ContentIssue]]:
    payload, issues = _read_json(path)
    if payload is None:
        return [], issues
    if not isinstance(payload, dict):
        return [], issues + [ContentIssue("media.json", "media", "json", "media.json must be an object")]
    issues.extend(_unknown_keys(payload, {"media"}, "media.json", "media"))
    rows = payload.get("media")
    if not isinstance(rows, list):
        return [], issues + [ContentIssue("media.json", "media", "media", "media must be an array")]
    media: list[MediaDoc] = []
    allowed = {
        "role",
        "checkpoint",
        "discovery",
        "path",
        "alt_text",
        "rights_basis",
        "author",
        "license",
        "license_url",
        "source_url",
        "permission_note",
        "position",
        "status",
    }
    for index, row in enumerate(rows):
        record = f"media[{index}]"
        if not isinstance(row, dict):
            issues.append(ContentIssue("media.json", record, "json", "media record must be an object"))
            continue
        issues.extend(_unknown_keys(row, allowed, "media.json", record))
        role = row.get("role")
        if role not in ROLES:
            issues.append(
                ContentIssue(
                    "media.json",
                    record,
                    "role",
                    "role must be route_hero, checkpoint_scene, journal, or discovery",
                )
            )
            role = None
        checkpoint = _optional_slug(row.get("checkpoint"), issues, record, "checkpoint", present="checkpoint" in row)
        discovery = _optional_slug(row.get("discovery"), issues, record, "discovery", present="discovery" in row)
        asset = row.get("path")
        if not isinstance(asset, str) or not asset.strip():
            issues.append(ContentIssue("media.json", record, "path", "path is required"))
            asset = None
        alt_text = _required_text(row.get("alt_text"), issues, "media.json", record, "alt_text")
        basis = row.get("rights_basis")
        if basis not in RIGHTS:
            issues.append(
                ContentIssue(
                    "media.json",
                    record,
                    "rights_basis",
                    "rights_basis must be public_domain, cc0, creative_commons, commissioned, or undocumented",
                )
            )
            basis = None
        author = _optional_text(row.get("author"), issues, "media.json", record, "author", present="author" in row)
        license_name = _optional_text(
            row.get("license"), issues, "media.json", record, "license", present="license" in row
        )
        license_url = _optional_text(
            row.get("license_url"), issues, "media.json", record, "license_url", present="license_url" in row
        )
        source_url = _optional_text(
            row.get("source_url"), issues, "media.json", record, "source_url", present="source_url" in row
        )
        permission = _optional_text(
            row.get("permission_note"),
            issues,
            "media.json",
            record,
            "permission_note",
            present="permission_note" in row,
        )
        position = _position(row.get("position"), issues, "media.json", record)
        status = _status(row.get("status"), issues, "media.json", record)
        if role == "route_hero" and (checkpoint is not None or discovery is not None):
            issues.append(
                ContentIssue("media.json", record, "checkpoint", "a route hero has no checkpoint or discovery")
            )
        if role in {"checkpoint_scene", "journal", "discovery"} and checkpoint is None:
            issues.append(ContentIssue("media.json", record, "checkpoint", "checkpoint is required for this role"))
        if role == "discovery" and discovery is None:
            issues.append(ContentIssue("media.json", record, "discovery", "discovery is required for this role"))
        if role in {"checkpoint_scene", "journal"} and discovery is not None:
            issues.append(ContentIssue("media.json", record, "discovery", "this role has no discovery"))
        if None in (role, asset, alt_text, basis, position, status):
            continue
        media.append(
            MediaDoc(
                role=role,
                checkpoint=checkpoint,
                discovery=discovery,
                path=asset,
                alt_text=alt_text,
                rights_basis=basis,
                author=author,
                license=license_name,
                license_url=license_url,
                source_url=source_url,
                permission_note=permission,
                position=position,
                status=status,
            )
        )
    return media, issues


def _structural_issues(package: Package) -> list[ContentIssue]:
    issues: list[ContentIssue] = []
    chapter_slugs: set[str] = set()
    chapter_positions: set[int] = set()
    for chapter in package.chapters:
        record = chapter.slug
        if chapter.slug in chapter_slugs:
            issues.append(ContentIssue("chapters.json", record, "slug", "duplicate chapter slug"))
        if chapter.position in chapter_positions:
            issues.append(ContentIssue("chapters.json", record, "position", "duplicate chapter position"))
        chapter_slugs.add(chapter.slug)
        chapter_positions.add(chapter.position)
    checkpoint_slugs = {doc.slug for doc in package.checkpoints}
    for doc in package.checkpoints:
        relative = f"checkpoints/{doc.slug}.md"
        if doc.chapter is not None and doc.chapter not in chapter_slugs:
            issues.append(
                ContentIssue(relative, doc.slug, "chapter", "chapter is not listed in chapters.json")
            )
        discovery_slugs: set[str] = set()
        discovery_positions: set[int] = set()
        for discovery in doc.discoveries:
            if discovery.slug in discovery_slugs:
                issues.append(
                    ContentIssue(relative, f"{doc.slug}/{discovery.slug}", "slug", "duplicate discovery slug")
                )
            if discovery.position in discovery_positions:
                issues.append(
                    ContentIssue(
                        relative,
                        f"{doc.slug}/{discovery.slug}",
                        "position",
                        "duplicate discovery position",
                    )
                )
            discovery_slugs.add(discovery.slug)
            discovery_positions.add(discovery.position)
    identities: set[tuple[str, str, str, int]] = set()
    published_heroes = 0
    published_scenes: dict[str, int] = defaultdict(int)
    for index, item in enumerate(package.media):
        record = f"media[{index}]"
        if item.identity() in identities:
            issues.append(ContentIssue("media.json", record, "position", "duplicate media identity"))
        identities.add(item.identity())
        if item.checkpoint is not None and item.checkpoint not in checkpoint_slugs:
            issues.append(
                ContentIssue(
                    "media.json",
                    record,
                    "checkpoint",
                    "checkpoint file is not in this package",
                )
            )
        if item.discovery is not None:
            owner = next((doc for doc in package.checkpoints if doc.slug == item.checkpoint), None)
            declared = {discovery.slug for discovery in owner.discoveries} if owner else set()
            if item.discovery not in declared:
                issues.append(
                    ContentIssue("media.json", record, "discovery", "discovery is not declared on that checkpoint")
                )
        problem = _rights_problem(item)
        if problem is not None:
            issues.append(ContentIssue("media.json", record, "rights_basis", problem))
        path_problem = _path_problem(item.path)
        if path_problem is not None:
            issues.append(ContentIssue("media.json", record, "path", path_problem))
        if item.role == "route_hero" and item.status == "published":
            published_heroes += 1
        if item.role == "checkpoint_scene" and item.status == "published" and item.checkpoint:
            published_scenes[item.checkpoint] += 1
    if published_heroes > 1:
        issues.append(
            ContentIssue("media.json", "route_hero", "status", "only one hero may be published")
        )
    for checkpoint, count in published_scenes.items():
        if count > 1:
            issues.append(
                ContentIssue(
                    "media.json",
                    checkpoint,
                    "status",
                    "only one checkpoint scene may be published",
                )
            )
    return issues


def _rights_problem(item: MediaDoc) -> str | None:
    if item.status != "published":
        return None
    if item.rights_basis == "public_domain" and item.license == "Public domain" and item.source_url:
        return None
    if item.rights_basis == "cc0" and item.license == "CC0" and item.license_url and item.source_url:
        return None
    if (
        item.rights_basis == "creative_commons"
        and item.license in CC_LICENSES
        and item.author
        and item.license_url
        and item.source_url
    ):
        return None
    if item.rights_basis == "commissioned" and item.license and item.author and item.permission_note:
        return None
    return f"published media needs a complete commercial-use rights record; {RIGHTS_NOTE}"


def _path_problem(asset_path: str) -> str | None:
    if _photo_relative(asset_path) is None:
        return "path must be a curated file under /static/photos/"
    if not _asset_is_readable(asset_path):
        return "file is missing or is not inside the curated photos directory"
    return None


def _read_catalog(database: DB, package: Package, *, lock: bool = False) -> tuple[Catalog | None, list[ContentIssue]]:
    route_query = "SELECT id FROM routes WHERE slug = ?"
    if lock:
        route_query += " FOR NO KEY UPDATE"
    route = database.execute(
        route_query,
        (package.route_slug,),
    ).fetchone()
    if route is None:
        return None, [
            ContentIssue(
                "manifest.json",
                package.route_slug,
                "route_slug",
                "route does not exist",
            )
        ]
    checkpoints = {
        row["slug"]: row
        for row in database.execute(
            """
            SELECT checkpoint.id, checkpoint.slug, checkpoint.position,
                   checkpoint.chapter_id, chapter.slug AS chapter_slug
            FROM route_checkpoints AS checkpoint
            LEFT JOIN route_chapters AS chapter
              ON chapter.id = checkpoint.chapter_id
            WHERE checkpoint.route_id = ?
            ORDER BY checkpoint.position, checkpoint.id
            """,
            (route["id"],),
        ).fetchall()
    }
    chapters = {
        row["slug"]: row
        for row in database.execute(
            """
            SELECT id, slug, title, summary, position, status
            FROM route_chapters
            WHERE route_id = ?
            """,
            (route["id"],),
        ).fetchall()
    }
    stories = {
        row["checkpoint_id"]: row
        for row in database.execute(
            """
            SELECT id, checkpoint_id, arrival_title, arrival_body,
                   journal_title, journal_body, status
            FROM checkpoint_stories
            WHERE route_id = ?
            """,
            (route["id"],),
        ).fetchall()
    }
    discoveries = {
        (row["checkpoint_id"], row["slug"]): row
        for row in database.execute(
            """
            SELECT id, checkpoint_id, slug, kind, title, body, position, status
            FROM story_discoveries
            WHERE route_id = ?
            """,
            (route["id"],),
        ).fetchall()
    }
    media = list(
        database.execute(
            """
            SELECT media.id, media.checkpoint_id, media.discovery_id, media.role,
                   media.asset_path, media.alt_text, media.rights_basis, media.author,
                   media.license, media.license_url, media.source_url,
                   media.permission_note, media.position, media.status,
                   checkpoint.slug AS checkpoint_slug,
                   discovery.slug AS discovery_slug
            FROM route_media AS media
            LEFT JOIN route_checkpoints AS checkpoint
              ON checkpoint.id = media.checkpoint_id
            LEFT JOIN story_discoveries AS discovery
              ON discovery.id = media.discovery_id
            WHERE media.route_id = ?
            """,
            (route["id"],),
        ).fetchall()
    )
    recommendations = recommendations_for_routes(database, [route["id"]])
    return (
        Catalog(
            route["id"], checkpoints, chapters, stories, discoveries, media,
            tuple(recommendations.get(route["id"], [])),
        ),
        [],
    )


def _catalog_issues(package: Package, catalog: Catalog) -> list[ContentIssue]:
    issues: list[ContentIssue] = []
    for doc in package.checkpoints:
        if doc.slug not in catalog.checkpoints:
            issues.append(
                ContentIssue(
                    f"checkpoints/{doc.slug}.md",
                    doc.slug,
                    "slug",
                    "checkpoint is not on this route",
                )
            )
    if issues:
        return issues
    package_chapters = {chapter.slug for chapter in package.chapters}
    for chapter in package.chapters:
        for slug, row in catalog.chapters.items():
            if slug in package_chapters:
                continue
            if row["position"] == chapter.position:
                issues.append(
                    ContentIssue(
                        "chapters.json",
                        chapter.slug,
                        "position",
                        f"position is still used by chapter {slug}, which this package does not name",
                    )
                )
    for doc in package.checkpoints:
        checkpoint_id = catalog.checkpoints[doc.slug]["id"]
        named = {item.slug for item in doc.discoveries}
        held: dict[int, str] = {}
        for (owner_id, slug), row in catalog.discoveries.items():
            if owner_id == checkpoint_id and slug not in named:
                held[row["position"]] = slug
        for item in doc.discoveries:
            occupant = held.get(item.position)
            if occupant is not None:
                issues.append(
                    ContentIssue(
                        f"checkpoints/{doc.slug}.md",
                        f"{doc.slug}/{item.slug}",
                        "position",
                        f"position is still used by discovery {occupant}, which this file does not name",
                    )
                )
    issues.extend(_span_issues(package, catalog))
    issues.extend(_hero_conflicts(package, catalog))
    issues.extend(_media_position_issues(package, catalog))
    return issues


def _span_issues(package: Package, catalog: Catalog) -> list[ContentIssue]:
    positions = {slug: row["position"] for slug, row in catalog.checkpoints.items()}
    assignment = {slug: row["chapter_slug"] for slug, row in catalog.checkpoints.items()}
    for doc in package.checkpoints:
        assignment[doc.slug] = doc.chapter
    chapter_position = {slug: row["position"] for slug, row in catalog.chapters.items()}
    for chapter in package.chapters:
        chapter_position[chapter.slug] = chapter.position
    by_chapter: dict[str, list[int]] = defaultdict(list)
    for slug, chapter_slug in assignment.items():
        if chapter_slug is not None:
            by_chapter[chapter_slug].append(positions[slug])
    issues: list[ContentIssue] = []
    for chapter_slug, chapter_positions in by_chapter.items():
        low = min(chapter_positions)
        high = max(chapter_positions)
        for slug, position in positions.items():
            if low < position < high and assignment[slug] != chapter_slug:
                issues.append(
                    ContentIssue(
                        "chapters.json",
                        chapter_slug,
                        "chapter",
                        "chapter checkpoints must form a contiguous span",
                    )
                )
                break
    occupied = sorted(
        (chapter_position[slug], slug, min(values), max(values))
        for slug, values in by_chapter.items()
    )
    for index, earlier in enumerate(occupied):
        for later in occupied[index + 1 :]:
            if earlier[0] < later[0] and earlier[3] >= later[2]:
                issues.append(
                    ContentIssue(
                        "chapters.json",
                        later[1],
                        "position",
                        "chapter order must follow checkpoint position",
                    )
                )
    return issues


def _hero_conflicts(package: Package, catalog: Catalog) -> list[ContentIssue]:
    issues: list[ContentIssue] = []
    packaged = {item.identity(): item for item in package.media}
    seen: dict[tuple, dict] = {}
    for row in catalog.media:
        key = (
            row["role"],
            row["checkpoint_slug"] or "",
            row["discovery_slug"] or "",
            row["position"],
        )
        if key in seen and key in packaged:
            issues.append(
                ContentIssue(
                    "media.json",
                    _media_identity(*key),
                    "position",
                    "more than one database row has this media identity",
                )
            )
        seen.setdefault(key, row)
    heroes = []
    scenes: dict[str, list] = defaultdict(list)
    for key, row in seen.items():
        status = packaged[key].status if key in packaged else row["status"]
        if key[0] == "route_hero" and status == "published":
            heroes.append(key)
        if key[0] == "checkpoint_scene" and status == "published":
            scenes[key[1]].append(key)
    for item in package.media:
        if item.identity() in seen:
            continue
        if item.role == "route_hero" and item.status == "published":
            heroes.append(item.identity())
        if item.role == "checkpoint_scene" and item.status == "published" and item.checkpoint:
            scenes[item.checkpoint].append(item.identity())
    if len(heroes) > 1:
        issues.append(
            ContentIssue("media.json", "route_hero", "status", "only one hero may be published")
        )
    for checkpoint, keys in scenes.items():
        if len(keys) > 1:
            issues.append(
                ContentIssue(
                    "media.json",
                    checkpoint,
                    "status",
                    "only one checkpoint scene may be published",
                )
            )
    return issues


def _media_position_issues(package: Package, catalog: Catalog) -> list[ContentIssue]:
    """Reject a journal or discovery position the import would occupy twice.

    The projection is the post-import state: omitted rows stay as they are,
    a named row takes the package status, and a new identity is an insert.
    Retired rows do not occupy the partial unique indexes. This does not
    retire, delete, move, or overwrite a row the package left out.
    """
    packaged = {item.identity(): item for item in package.media}
    rows_by_identity: dict[tuple, list[dict]] = defaultdict(list)
    for row in catalog.media:
        key = (
            row["role"],
            row["checkpoint_slug"] or "",
            row["discovery_slug"] or "",
            int(row["position"]),
        )
        rows_by_identity[key].append(row)
    for rows in rows_by_identity.values():
        rows.sort(key=lambda row: row["id"])
    checkpoint_ids = {slug: row["id"] for slug, row in catalog.checkpoints.items()}
    checkpoint_slugs = {row["id"]: slug for slug, row in catalog.checkpoints.items()}
    discovery_ids = {
        (checkpoint_id, slug): row["id"]
        for (checkpoint_id, slug), row in catalog.discoveries.items()
    }
    discovery_names = {
        row["id"]: (checkpoint_slugs.get(checkpoint_id, ""), slug)
        for (checkpoint_id, slug), row in catalog.discoveries.items()
    }
    occupants: dict[tuple, list[tuple]] = defaultdict(list)

    def _add(slot: tuple, source: str, status: str, role: str, checkpoint: str, discovery: str, position: int) -> None:
        if status == "retired":
            return
        occupants[slot].append((source, status, role, checkpoint, discovery, position))

    for key, rows in rows_by_identity.items():
        item = packaged.get(key)
        for index, row in enumerate(rows):
            role = row["role"]
            if role not in {"journal", "discovery"}:
                continue
            status = item.status if item is not None and index == 0 else row["status"]
            position = int(row["position"])
            checkpoint = row["checkpoint_slug"] or checkpoint_slugs.get(row["checkpoint_id"], "")
            discovery = row["discovery_slug"] or ""
            if role == "discovery" and not discovery and row["discovery_id"] is not None:
                discovery = discovery_names.get(row["discovery_id"], ("", ""))[1]
            source = "named" if item is not None and index == 0 else "omitted"
            if role == "journal" and row["checkpoint_id"] is not None:
                _add(("journal", row["checkpoint_id"], position), source, status, role, checkpoint, "", position)
            if role == "discovery" and row["discovery_id"] is not None:
                _add(
                    ("discovery", row["discovery_id"], position),
                    source,
                    status,
                    role,
                    checkpoint,
                    discovery,
                    position,
                )
    for item in package.media:
        if item.identity() in rows_by_identity or item.status == "retired":
            continue
        if item.role not in {"journal", "discovery"} or item.checkpoint is None:
            continue
        checkpoint_id = checkpoint_ids.get(item.checkpoint)
        if checkpoint_id is None:
            continue
        if item.role == "journal":
            _add(
                ("journal", checkpoint_id, item.position),
                "incoming",
                item.status,
                item.role,
                item.checkpoint,
                "",
                item.position,
            )
            continue
        if item.discovery is None:
            continue
        discovery_id = discovery_ids.get((checkpoint_id, item.discovery))
        if discovery_id is None:
            continue
        _add(
            ("discovery", discovery_id, item.position),
            "incoming",
            item.status,
            item.role,
            item.checkpoint,
            item.discovery,
            item.position,
        )
    issues: list[ContentIssue] = []
    for slot, holders in occupants.items():
        if len(holders) < 2 or not any(holder[0] in {"named", "incoming"} for holder in holders):
            continue
        omitted = next((holder for holder in holders if holder[0] == "omitted"), None)
        subject = next((holder for holder in holders if holder[0] == "incoming"), None)
        if subject is None:
            subject = next(holder for holder in holders if holder[0] == "named")
        _source, status, role, checkpoint, discovery, position = omitted or holders[0]
        who = (
            f"an omitted {status} {role}"
            if omitted is not None
            else f"another non-retired {role}"
        )
        place = [f"route {package.route_slug}", f"checkpoint {checkpoint or subject[3]}", f"role {role}"]
        if role == "discovery":
            place.insert(2, f"discovery {discovery or subject[4]}")
        issues.append(
            ContentIssue(
                "media.json",
                _media_identity(subject[2], subject[3], subject[4], subject[5]),
                "position",
                f"position {position} is still used by {who} for {', '.join(place)}",
            )
        )
    return issues


def _plan(package: Package, catalog: Catalog) -> list[Change]:
    changes: list[Change] = []
    if package.recommended_activity_types != catalog.recommended_activity_types:
        changes.append(Change(
            "update", "recommendations", package.route_slug,
            fields=("recommended_activity_types",), values=package.recommended_activity_types,
        ))
    for chapter in package.chapters:
        existing = catalog.chapters.get(chapter.slug)
        values = (chapter.slug, chapter.title, chapter.summary, chapter.position, chapter.status)
        if existing is None:
            changes.append(Change("insert", "chapter", chapter.slug, values=values))
            continue
        current = (
            existing["slug"],
            existing["title"],
            existing["summary"],
            existing["position"],
            existing["status"],
        )
        fields = _diff(CHAPTER_FIELDS, current[1:], values[1:])
        changes.append(
            Change(
                _action(fields, existing["status"], chapter.status),
                "chapter",
                chapter.slug,
                fields,
                record_id=existing["id"],
                values=values,
            )
        )
    for doc in package.checkpoints:
        current = catalog.checkpoints[doc.slug]
        action = "unchanged" if current["chapter_slug"] == doc.chapter else "update"
        fields = ("chapter",) if action == "update" else ()
        changes.append(
            Change(
                action,
                "assignment",
                doc.slug,
                fields,
                checkpoint_id=current["id"],
                values=(doc.chapter,),
            )
        )
        story = catalog.stories.get(current["id"])
        story_values = (
            doc.arrival_title,
            doc.arrival_body,
            doc.journal_title,
            doc.journal_body,
            doc.status,
        )
        if story is None:
            changes.append(
                Change(
                    "insert",
                    "story",
                    doc.slug,
                    checkpoint_id=current["id"],
                    values=story_values,
                )
            )
        else:
            current_story = tuple(story[name] for name in STORY_FIELDS)
            fields = _diff(STORY_FIELDS, current_story, story_values)
            changes.append(
                Change(
                    _action(fields, story["status"], doc.status),
                    "story",
                    doc.slug,
                    fields,
                    record_id=story["id"],
                    checkpoint_id=current["id"],
                    values=story_values,
                )
            )
        for discovery in doc.discoveries:
            existing = catalog.discoveries.get((current["id"], discovery.slug))
            values = (
                discovery.slug,
                discovery.kind,
                discovery.title,
                discovery.body,
                discovery.position,
                discovery.status,
            )
            identity = f"{doc.slug}/{discovery.slug}"
            if existing is None:
                changes.append(
                    Change(
                        "insert",
                        "discovery",
                        identity,
                        checkpoint_id=current["id"],
                        values=values,
                    )
                )
                continue
            current_values = (
                existing["slug"],
                existing["kind"],
                existing["title"],
                existing["body"],
                existing["position"],
                existing["status"],
            )
            fields = _diff(DISCOVERY_FIELDS, current_values[1:], values[1:])
            changes.append(
                Change(
                    _action(fields, existing["status"], discovery.status),
                    "discovery",
                    identity,
                    fields,
                    record_id=existing["id"],
                    checkpoint_id=current["id"],
                    values=values,
                )
            )
    media_index: dict[tuple, dict] = {}
    for row in catalog.media:
        key = (
            row["role"],
            row["checkpoint_slug"] or "",
            row["discovery_slug"] or "",
            row["position"],
        )
        media_index.setdefault(key, row)
    for item in package.media:
        values = (
            item.path,
            item.alt_text,
            item.rights_basis,
            item.author,
            item.license,
            item.license_url,
            item.source_url,
            item.permission_note,
            item.status,
        )
        identity = _media_identity(*item.identity())
        existing = media_index.get(item.identity())
        checkpoint_id = (
            catalog.checkpoints[item.checkpoint]["id"] if item.checkpoint is not None else None
        )
        if existing is None:
            changes.append(
                Change(
                    "insert",
                    "media",
                    identity,
                    checkpoint_id=checkpoint_id,
                    values=(item.discovery, item.position, item.role) + values,
                )
            )
            continue
        current_values = tuple(existing[name] for name in MEDIA_FIELDS)
        fields = _diff(MEDIA_FIELDS, current_values, values)
        changes.append(
            Change(
                _action(fields, existing["status"], item.status),
                "media",
                identity,
                fields,
                record_id=existing["id"],
                checkpoint_id=checkpoint_id,
                values=(item.discovery, item.position, item.role) + values,
            )
        )
    changes.sort(key=lambda change: (KIND_ORDER[change.kind], change.identity, change.action))
    return changes


def _apply(database: DB, catalog: Catalog, plan: list[Change]) -> None:
    for change in plan:
        if change.kind != "recommendations":
            continue
        database.execute(
            "DELETE FROM route_activity_recommendations WHERE route_id = ?", (catalog.route_id,)
        )
        for position, activity_type in enumerate(change.values):
            database.execute(
                """
                INSERT INTO route_activity_recommendations (route_id, activity_type, position)
                VALUES (?, ?, ?)
                """,
                (catalog.route_id, activity_type, position),
            )
    chapter_moves = [
        (change.record_id, change.values[3])
        for change in plan
        if change.kind == "chapter"
        and change.action in {"update", "retire"}
        and "position" in change.fields
        and change.record_id is not None
    ]
    _park_chapters(database, catalog.route_id, chapter_moves)
    chapter_ids = {slug: row["id"] for slug, row in catalog.chapters.items()}
    for change in plan:
        if change.kind != "chapter" or change.action == "unchanged":
            continue
        slug, title, summary, position, status = change.values
        if change.action == "insert":
            inserted = database.execute(
                """
                INSERT INTO route_chapters (route_id, slug, title, summary, position, status)
                VALUES (?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (catalog.route_id, slug, title, summary, position, status),
            ).fetchone()
            chapter_ids[slug] = inserted["id"]
            continue
        database.execute(
            """
            UPDATE route_chapters
            SET title = ?, summary = ?, position = ?, status = ?
            WHERE id = ? AND route_id = ?
            """,
            (title, summary, position, status, change.record_id, catalog.route_id),
        )
    for change in plan:
        if change.kind != "assignment" or change.action != "update":
            continue
        chapter_slug = change.values[0]
        chapter_id = None if chapter_slug is None else chapter_ids[chapter_slug]
        database.execute(
            """
            UPDATE route_checkpoints
            SET chapter_id = ?
            WHERE id = ? AND route_id = ?
            """,
            (chapter_id, change.checkpoint_id, catalog.route_id),
        )
    for change in plan:
        if change.kind != "story" or change.action == "unchanged":
            continue
        title, body, journal_title, journal_body, status = change.values
        if change.action == "insert":
            database.execute(
                """
                INSERT INTO checkpoint_stories (
                    checkpoint_id, route_id, arrival_title, arrival_body,
                    journal_title, journal_body, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change.checkpoint_id,
                    catalog.route_id,
                    title,
                    body,
                    journal_title,
                    journal_body,
                    status,
                ),
            )
            continue
        database.execute(
            """
            UPDATE checkpoint_stories
            SET arrival_title = ?, arrival_body = ?, journal_title = ?,
                journal_body = ?, status = ?
            WHERE id = ? AND route_id = ?
            """,
            (title, body, journal_title, journal_body, status, change.record_id, catalog.route_id),
        )
    discovery_moves: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for change in plan:
        if (
            change.kind == "discovery"
            and change.action in {"update", "retire"}
            and "position" in change.fields
            and change.record_id is not None
            and change.checkpoint_id is not None
        ):
            discovery_moves[change.checkpoint_id].append((change.record_id, change.values[4]))
    for checkpoint_id, moves in discovery_moves.items():
        _park_discoveries(database, checkpoint_id, moves)
    for change in plan:
        if change.kind != "discovery" or change.action == "unchanged":
            continue
        slug, kind, title, body, position, status = change.values
        if change.action == "insert":
            database.execute(
                """
                INSERT INTO story_discoveries (
                    checkpoint_id, route_id, slug, kind, title, body, position, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (change.checkpoint_id, catalog.route_id, slug, kind, title, body, position, status),
            )
            continue
        database.execute(
            """
            UPDATE story_discoveries
            SET kind = ?, title = ?, body = ?, position = ?, status = ?
            WHERE id = ? AND route_id = ?
            """,
            (kind, title, body, position, status, change.record_id, catalog.route_id),
        )
    discovery_ids = {
        (checkpoint_id, slug): row["id"]
        for (checkpoint_id, slug), row in catalog.discoveries.items()
    }
    inserted_discoveries = database.execute(
        """
        SELECT id, checkpoint_id, slug
        FROM story_discoveries
        WHERE route_id = ?
        """,
        (catalog.route_id,),
    ).fetchall()
    for row in inserted_discoveries:
        discovery_ids[(row["checkpoint_id"], row["slug"])] = row["id"]
    for change in plan:
        if change.kind != "media" or change.action == "unchanged":
            continue
        discovery_slug, position, role, path, alt_text, basis, author, license_name, license_url, source_url, permission, status = change.values
        discovery_id = None
        if discovery_slug is not None:
            discovery_id = discovery_ids[(change.checkpoint_id, discovery_slug)]
        if change.action == "insert":
            database.execute(
                """
                INSERT INTO route_media (
                    route_id, checkpoint_id, discovery_id, role, asset_path, alt_text,
                    rights_basis, author, license, license_url, source_url,
                    permission_note, position, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    catalog.route_id,
                    change.checkpoint_id,
                    discovery_id,
                    role,
                    path,
                    alt_text,
                    basis,
                    author,
                    license_name,
                    license_url,
                    source_url,
                    permission,
                    position,
                    status,
                ),
            )
            continue
        database.execute(
            """
            UPDATE route_media
            SET asset_path = ?, alt_text = ?, rights_basis = ?, author = ?,
                license = ?, license_url = ?, source_url = ?, permission_note = ?,
                status = ?
            WHERE id = ? AND route_id = ?
            """,
            (
                path,
                alt_text,
                basis,
                author,
                license_name,
                license_url,
                source_url,
                permission,
                status,
                change.record_id,
                catalog.route_id,
            ),
        )


def _park_chapters(database: DB, route_id: int, moves: list[tuple[int, int]]) -> None:
    if not moves:
        return
    current = database.execute(
        "SELECT COALESCE(MAX(position), -1) AS max_position FROM route_chapters WHERE route_id = ?",
        (route_id,),
    ).fetchone()
    base = max(current["max_position"], max(final for _, final in moves)) + 1
    for offset, (record_id, _final) in enumerate(moves):
        database.execute(
            "UPDATE route_chapters SET position = ? WHERE id = ? AND route_id = ?",
            (base + offset, record_id, route_id),
        )


def _park_discoveries(database: DB, checkpoint_id: int, moves: list[tuple[int, int]]) -> None:
    if not moves:
        return
    current = database.execute(
        """
        SELECT COALESCE(MAX(position), -1) AS max_position
        FROM story_discoveries
        WHERE checkpoint_id = ?
        """,
        (checkpoint_id,),
    ).fetchone()
    base = max(current["max_position"], max(final for _, final in moves)) + 1
    for offset, (record_id, _final) in enumerate(moves):
        database.execute(
            "UPDATE story_discoveries SET position = ? WHERE id = ? AND checkpoint_id = ?",
            (base + offset, record_id, checkpoint_id),
        )


def _diff(names: tuple[str, ...], current: tuple, new: tuple) -> tuple[str, ...]:
    return tuple(name for name, before, after in zip(names, current, new) if before != after)


def _action(fields: tuple[str, ...], old_status: str, new_status: str) -> str:
    if not fields:
        return "unchanged"
    if "status" in fields and new_status == "retired" and old_status != "retired":
        return "retire"
    return "update"


def _media_identity(role: str, checkpoint: str, discovery: str, position: int) -> str:
    if role == "route_hero":
        return f"route_hero {position}"
    if role == "discovery":
        return f"discovery {checkpoint}/{discovery} {position}"
    return f"{role} {checkpoint} {position}"


def _sections(body: str, file: str, record: str) -> tuple[dict[str, str], list[ContentIssue]]:
    issues: list[ContentIssue] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    prelude: list[str] = []
    for line in body.splitlines():
        heading = _heading(line)
        if heading is None:
            if current is None:
                prelude.append(line)
            else:
                sections[current].append(line)
            continue
        if heading == "invalid":
            issues.append(ContentIssue(file, record, "body", f"unsupported heading {line.strip()}"))
            current = "invalid"
            sections.setdefault(current, [])
            continue
        if heading in sections:
            issues.append(ContentIssue(file, record, heading, "duplicate Markdown section"))
        current = heading
        sections[current] = []
    if any(line.strip() for line in prelude):
        issues.append(ContentIssue(file, record, "body", "prose must sit under an Arrival, Journal, or Discovery heading"))
    rendered = {
        key: "\n".join(lines).strip()
        for key, lines in sections.items()
        if key != "invalid"
    }
    return rendered, issues


def _heading(line: str) -> str | None:
    text = line.strip()
    if text == "# Arrival":
        return "arrival"
    if text == "# Journal":
        return "journal"
    prefix = "# Discovery "
    if text.startswith(prefix):
        slug = text[len(prefix) :]
        if SLUG.fullmatch(slug) is None:
            return "invalid"
        return "discovery:" + slug
    if text.startswith("#"):
        return "invalid"
    return None


def _split_front_matter(text: str) -> tuple[str, str] | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    return None


def _read_json(path: Path) -> tuple[object | None, list[ContentIssue]]:
    name = path.name
    if not path.is_file():
        return None, [ContentIssue(name, name, "path", "required file is missing")]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [ContentIssue(name, name, "file", str(exc))]
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys), []
    except json.JSONDecodeError as exc:
        return None, [ContentIssue(name, name, "json", exc.msg)]
    except ValueError as exc:
        return None, [ContentIssue(name, name, "json", str(exc))]


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key}")
        result[key] = value
    return result


def _unknown_keys(payload: dict, allowed: set[str], file: str, record: str) -> list[ContentIssue]:
    return [
        ContentIssue(file, record, key, "unknown field")
        for key in payload
        if key not in allowed
    ]


def _slug(value: object, issues: list[ContentIssue], file: str, record: str, field: str) -> str | None:
    if not isinstance(value, str) or SLUG.fullmatch(value) is None:
        issues.append(ContentIssue(file, record, field, "must be a lowercase slug"))
        return None
    return value


def _optional_slug(
    value: object,
    issues: list[ContentIssue],
    record: str,
    field: str,
    *,
    present: bool,
) -> str | None:
    if not present or value is None:
        return None
    return _slug(value, issues, "media.json", record, field)


def _required_text(value: object, issues: list[ContentIssue], file: str, record: str, field: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(ContentIssue(file, record, field, "must be non-blank text"))
        return None
    return value.strip()


def _optional_text(
    value: object,
    issues: list[ContentIssue],
    file: str,
    record: str,
    field: str,
    *,
    present: bool,
) -> str | None:
    if not present or value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        issues.append(ContentIssue(file, record, field, "must be non-blank text when present"))
        return None
    return value.strip()


def _position(value: object, issues: list[ContentIssue], file: str, record: str) -> int | None:
    if type(value) is not int or value < 0:
        issues.append(ContentIssue(file, record, "position", "position must be a whole number starting at zero"))
        return None
    return value


def _status(value: object, issues: list[ContentIssue], file: str, record: str) -> str | None:
    if value not in STATUSES:
        issues.append(ContentIssue(file, record, "status", "status must be draft, published, or retired"))
        return None
    return str(value)


def _chapter_ref(value: object, issues: list[ContentIssue], file: str, record: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or SLUG.fullmatch(value) is None:
        issues.append(ContentIssue(file, record, "chapter", "chapter must be a slug or null"))
        return None
    return value


if __name__ == "__main__":
    raise SystemExit(main())
