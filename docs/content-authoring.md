# Route content authoring

GitHub is the editorial source of truth. PostgreSQL is the runtime copy the journal API reads. Adding or updating a route's narrative does not require an application change or a database migration.

The importer writes curated rows only. It does not change route metric requirements, checkpoint thresholds, checkpoint positions, journey progress, activity credits, expedition standings, or arrival acknowledgments.

This is not a CMS and not a branching narrative. A route is one linear sequence of checkpoints. A package fills in the story for checkpoints that already exist. Asset lifecycle, optimization, and delivery are `docs/media-infrastructure.md`.

`content/routes/sample-ridge/` is a fictional example. It is not a launch route, it is not imported at startup, and its photograph metadata does not describe the real file it points at. That path exists only so the validator can see a file inside `adventurer/static/photos/`.

## Directory

One directory per route:

```text
content/routes/<route-slug>/
  manifest.json
  chapters.json
  media.json
  checkpoints/
    <checkpoint-slug>.md
```

The directory name is the route slug. `manifest.json`, `chapters.json`, `media.json`, and the `checkpoints/` directory are required. Checkpoint prose is Markdown. Everything else is JSON, UTF-8, with no duplicate keys. The pipeline rejects any other file in that directory, including a stray note, so a typo cannot hide.

Create the route's geography, metric requirements, and checkpoint thresholds separately. The package identifies those checkpoints by the slugs they already have. It does not create them.

## Commands

From the repository root, with the virtualenv's Python:

```text
python -m adventurer.content_pipeline validate content/routes/<route-slug>
python -m adventurer.content_pipeline validate content/routes/<route-slug> --database
python -m adventurer.content_pipeline import content/routes/<route-slug> --dry-run
python -m adventurer.content_pipeline import content/routes/<route-slug>
```

`validate` reads the files and writes nothing. Without `--database` it does not connect, so it cannot check that the route exists or that chapter assignments form a contiguous span. Those checks need checkpoint positions, and the package does not copy geography.

`--database`, `--dry-run`, and `import` read `TEST_DATABASE_URL`. They refuse a missing URL, a hosted host, and any database other than `adventurer_migration_test` on `localhost` or `127.0.0.1`. They do not read `DATABASE_URL`.

Validation prints one problem per line:

```text
chapters.json: approach: position: duplicate chapter position
```

The fields are the file, the record, the field, and the reason. A non-zero exit means the package was not applied.

A dry run prints the plan and rolls back:

```text
insert chapter approach
update story gate (arrival_body)
unchanged discovery gate/old-post
retire story camp (status)
```

`insert` is a new row. `update` changes an existing row and keeps its id. `unchanged` is listed so a rerun shows that the package was read. `retire` is an existing row whose status becomes `retired`. An empty plan prints `no editorial changes`.

Import validates the whole package, then writes one route in one transaction. If any row fails, the transaction rolls back. A second import of the same package changes nothing and does not bump `updated_at`.

## manifest.json

```json
{
  "format_version": 1,
  "route_slug": "sample-ridge"
}
```

`format_version` must be the integer `1`. `route_slug` must match the directory name and the slug of an existing route. The importer looks the route up. It does not insert a route.

`docs/architecture.md` section 20 proposes an optional `recommended_activity_types` array on this file. The current importer rejects unknown manifest fields, so a package that includes the array fails validation. Do not add it until the importer accepts it. When that lands, the array is editorial display order only. Omitting it means the route has no recommendation. It does not restrict which activities credit a journey, and the importer still does not write journey progress.

## chapters.json

```json
{
  "chapters": [
    {
      "slug": "approach",
      "title": "The cedar approach",
      "summary": "Optional. Omit the key, or set it to null, for no summary.",
      "position": 0,
      "status": "published"
    }
  ]
}
```

| JSON field | Column |
| --- | --- |
| `slug` | `route_chapters.slug` |
| `title` | `route_chapters.title` |
| `summary` | `route_chapters.summary` |
| `position` | `route_chapters.position` |
| `status` | `route_chapters.status` |

Slugs are lowercase letters, digits, and single hyphens, and they start with a letter: `approach`, `blue-camp`. Titles and summaries are trimmed. A blank string is rejected. Positions are whole numbers starting at zero and are unique within the package. Status is `draft`, `published`, or `retired`.

Chapter order is the `position` field, not the order of the array. A chapter with no checkpoints is allowed. The span rule ignores it.

## Checkpoint Markdown

The file name is the checkpoint slug: `checkpoints/gate.md` updates the checkpoint whose slug is `gate`. The file begins with a JSON object between `---` lines. The closing fence is a line by itself.

```markdown
---
{
  "status": "published",
  "chapter": "approach",
  "arrival_title": "The cedar gate",
  "journal_title": "Below the ridge",
  "discoveries": [
    {
      "slug": "old-post",
      "kind": "historical",
      "title": "The old post",
      "position": 0,
      "status": "published"
    }
  ]
}
---

# Arrival

A short reveal.

# Journal

The longer entry.

# Discovery old-post

A longer note.
```

`chapter` is required. A JSON `null` leaves that checkpoint unassigned. A slug must be listed in `chapters.json`. Omitting the key is an error, so a checkpoint file cannot drop its chapter by accident.

`journal_title` is optional. Omit it, or set it to null, when the checkpoint has no Field Journal. A title without a Journal section, or a Journal section without a title, is an error. The database stores the pair together or not at all.

Discoveries are optional. The heading `# Discovery <slug>` must match a `discoveries` entry, and every entry needs that heading. Kind is `cultural`, `historical`, `geographic`, `culinary`, or `photographic`.

Headings are a full line: `# Arrival`, `# Journal`, or `# Discovery <slug>`. Any other heading is rejected. Prose before the first heading is rejected.

| Package | Column |
| --- | --- |
| file name | lookup of `route_checkpoints.slug` |
| `chapter` | `route_checkpoints.chapter_id` |
| `status` | `checkpoint_stories.status` |
| `arrival_title` | `checkpoint_stories.arrival_title` |
| `# Arrival` body | `checkpoint_stories.arrival_body` |
| `journal_title` | `checkpoint_stories.journal_title` |
| `# Journal` body | `checkpoint_stories.journal_body` |
| discovery `slug` | `story_discoveries.slug` |
| discovery `kind` | `story_discoveries.kind` |
| discovery `title` | `story_discoveries.title` |
| `# Discovery <slug>` body | `story_discoveries.body` |
| discovery `position` | `story_discoveries.position` |
| discovery `status` | `story_discoveries.status` |

Arrival title and body are required for every checkpoint file. Bodies are trimmed. Internal paragraphs stay. The importer does not change `route_checkpoints.position`, the checkpoint name, or any threshold.

## media.json

```json
{
  "media": [
    {
      "role": "journal",
      "checkpoint": "gate",
      "path": "/static/photos/lukla.jpg",
      "alt_text": "Stand-in photograph beside the fictional cedar gate.",
      "rights_basis": "creative_commons",
      "author": "Sample Author",
      "license": "CC BY 4.0",
      "license_url": "https://creativecommons.org/licenses/by/4.0/",
      "source_url": "https://example.com/sample-ridge/gate",
      "position": 0,
      "status": "published"
    }
  ]
}
```

| JSON field | Column |
| --- | --- |
| `role` | `route_media.role` |
| `checkpoint` | `route_media.checkpoint_id` |
| `discovery` | `route_media.discovery_id` |
| `path` | `route_media.asset_path` |
| `alt_text` | `route_media.alt_text` |
| `rights_basis` | `route_media.rights_basis` |
| `author` | `route_media.author` |
| `license` | `route_media.license` |
| `license_url` | `route_media.license_url` |
| `source_url` | `route_media.source_url` |
| `permission_note` | `route_media.permission_note` |
| `position` | `route_media.position` |
| `status` | `route_media.status` |

`permission_note` is stored for a commissioned photograph. The journal API does not select it and does not return it.

Roles:

- `route_hero` has no checkpoint and no discovery.
- `checkpoint_scene` and `journal` name a checkpoint and no discovery.
- `discovery` names a checkpoint and a discovery declared in that checkpoint's file.

The checkpoint file has to be in the package. Media is not attached to a story the package does not mention.

There is one published hero per route and one published scene per checkpoint. Draft and retired copies can sit beside them.

A media record's identity is its role, checkpoint slug, discovery slug, and position. The same identity updates the existing row and keeps its id. Two records with that identity are an error. Position is part of the identity, so changing position does not move the old row. To replace a photograph, retire the old position and add the new position. A retired row and a new row still need different positions in the package.

Journal photographs on one checkpoint share a position namespace. Discovery photographs share a position namespace for that discovery. Draft and published rows occupy the position. Retired rows do not. `route_hero` and `checkpoint_scene` are outside these namespaces; a route still has one published hero, and a checkpoint still has one published scene.

Validation builds the position map that would exist after the import. Omitted rows stay in that map with their current status and position. Named rows take the package status. A new identity is an insert. If the map would contain two non-retired journal rows for one checkpoint and position, or two non-retired discovery rows for one discovery and position, the package is rejected before any write. The message names the route, the checkpoint, the discovery when the photograph belongs to one, the role, and the position. Retire the omitted row by naming it, or choose a position that is free. A dry run reports the same error and writes nothing. The importer does not retire, delete, move, or overwrite the omitted row to clear the position.

A retired row still owns its identity. Setting that same role, checkpoint, discovery, and position back to `draft` or `published` updates the row. That reuses the position the index allows. Different checkpoints do not share journal positions. Different discoveries do not share discovery positions. A journal image, a checkpoint scene, and a discovery image may all use position 0.

Every path must be an existing file under `/static/photos/`, for every status. The rules are the same ones the journal uses: one site-relative path, safe segments, and a file that resolves inside that directory. Do not add a remote URL, a path outside that directory, or a new download.

After import, the home scene reads published `route_hero` and `checkpoint_scene` rows from `route_media`. The filename is not a selector. A published journal or discovery image is not the home scene, even when it points at the same file. Retiring the published scene and hero removes that photograph from the scene. The file can remain on disk. `adventurer/photo_credits.json` is archival attribution for a later reviewed import. The importer does not publish a photograph because that file has an entry.

## Publication

Status `published` is visible to the journal and, for chapters, to the route catalog. `draft` and `retired` are hidden. A retired chapter still occupies its position and still counts in the span rule when checkpoints are assigned to it. The catalog only lists published chapters. A story on a draft or retired chapter stays in the timeline as an ungrouped entry once the checkpoint is reached, which is the existing journal rule.

Chapter assignments have to match checkpoint order when a database check runs:

- Checkpoints in one chapter form a contiguous span. An unassigned checkpoint, or a checkpoint from another chapter, cannot sit between them.
- A later chapter cannot contain an earlier checkpoint.
- Empty chapters are ignored.
- Checkpoints the package does not mention keep their current chapter.

The importer sets the two deferred constraints `route_chapter_spans` and `route_chapter_spans_chapters` by name before commit. It does not use `SET CONSTRAINTS ALL`.

## Updates, omissions, and retirement

Rows are matched by stable editorial identity:

- A chapter matches `(route, slug)` and keeps its id.
- A story matches the checkpoint and keeps its id. There is one story per checkpoint.
- A discovery matches `(checkpoint, slug)` and keeps its id.
- Media matches `(role, checkpoint, discovery, position)` and keeps its id.

The same identity is updated in place. Ids stay so arrival acknowledgments, which point at the checkpoint, and discovery photographs, which point at the discovery id, remain attached. A rerun that changes nothing does not write and does not change `updated_at`.

Swapping two chapter positions, or two discovery positions, is done inside the import transaction by parking the rows on unused positions and then writing the final positions. Positions start at zero, so the park positions are above the current maximum.

Absence is not deletion. A chapter, checkpoint file, discovery, or media record that the package does not name is left as it is, including its status and position. A partial package cannot wipe the route. Because an omitted row keeps its position, a new chapter or discovery cannot take that position until the omitted row is named and moved or the new row chooses a free position. A non-retired journal or discovery photograph works the same way. Naming that row and setting `status` to `retired` releases its position in the index. Leaving it out of the package does not.

A checkpoint file that is present is the statement for that checkpoint's story and its chapter assignment. Naming the file and setting `chapter` to null unassigns that checkpoint. Naming the file and leaving a discovery out does not delete that discovery.

Retirement is explicit. Set `status` to `retired` on the record you want to withdraw. The row stays, its id stays, and the API hides it. Do not delete the JSON object to retire it. Inserting a brand-new row that is already `retired` is an insert, not a retirement of something that was published.

A failed import rolls the whole route back. Journey rows, credits, and `journey_story_views` are not part of the write, so they stay even when the editorial transaction commits.

## Photography licensing

Published media must satisfy the same commercial-use categories as section 18.4 of `docs/architecture.md`. Draft and retired rows may be incomplete. A published row must match one of these:

- `public_domain`: license text exactly `Public domain`, plus `source_url`.
- `cc0`: license text exactly `CC0`, plus `license_url` and `source_url`.
- `creative_commons`: license text exactly one of `CC BY 2.0`, `CC BY-SA 2.0`, `CC BY 2.5`, `CC BY-SA 2.5`, `CC BY 3.0`, `CC BY-SA 3.0`, `CC BY 4.0`, `CC BY-SA 4.0`, plus `author`, `license_url`, and `source_url`.
- `commissioned`: `license`, `author`, and `permission_note`.

`undocumented`, a noncommercial license, a no-derivatives license, and a missing field keep the row unpublished. The validator rejects the package rather than storing a published row that the database would refuse.

A string that matches this list does not prove the license is authentic. Someone still has to confirm the rights. The validator only checks that the metadata is complete and shaped like the publication rule.

Checklist before publishing a photograph:

- The file is already in `adventurer/static/photos/` and the path is `/static/photos/<file>`.
- Alt text describes the picture for a reader who cannot see it.
- The rights category, license, author, and URLs are filled in for that category.
- A person has checked the actual license, not only the JSON.
- `status` stays `draft` until that check is done.
- A published `route_hero` or `checkpoint_scene` is eligible for the home scene. A journal or discovery image is not the home scene.

## What a later author copies

1. Confirm the route and its checkpoint slugs already exist. Do not invent thresholds in the package.
2. Copy `content/routes/sample-ridge/` to `content/routes/<the-route-slug>/` and replace the fictional prose.
3. Point every photograph at a file that is already curated, and fill in rights from the real license.
4. Run `validate`, then `validate --database`.
5. Run `import --dry-run` and read the inserts, updates, unchanged rows, and retirements.
6. Import against the isolated database named in `AGENTS.md`.
7. Leave a checkpoint out of the package when you do not intend to touch it. Set `status` to `retired` when you do intend to withdraw it.

Launch routes, including Everest, are not seeded by this pipeline. `adventurer/challenges.py` remains the running Everest prose until a later, reviewed import replaces it.
