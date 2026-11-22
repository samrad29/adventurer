# Phase 3: routes and expeditions

A route is the curated adventure. An expedition is one group of people doing that route. The executable change is `migrations/sql/0005_routes_and_expeditions.sql`.

This revision was applied only to the isolated database `adventurer_migration_test` on `127.0.0.1:54329`. It was not applied to hosted Supabase, and nothing was deployed to Render.

## What the migration does

It keeps the existing Everest expedition row, including its id and slug `everest`.

1. Create `routes` and `route_checkpoints`.
2. Insert the published Everest route from the expedition's target elevation, with region `Khumbu, Nepal` and the summary from `adventurer/challenges.py`.
3. Copy all 16 checkpoints onto that route, preserving slug, name, position, and elevation.
4. Point the existing expedition at the route. Its name becomes `Global Everest Challenge`, visibility `public`, status `active`, and owner `NULL`.
5. Drop `expeditions.target_elevation_m` and `expedition_checkpoints`.
6. Retarget `assert_phase1_legacy_match()` so checkpoint checks join `route_checkpoints` through the expedition's route.
7. Create `expedition_invitations`. Only the SHA-256 hex digest of a share token is stored.

Participations, activities, credits, verification events, journal notes, and dates are not rewritten. `competition_seasons` still points at expeditions and stays unused.

New expedition columns:

| Column | Rule |
|---|---|
| `route_id` | Required. `ON DELETE RESTRICT`, so a route that still has an expedition cannot be deleted. |
| `visibility` | `public` or `private`. User-created expeditions are private in this release. |
| `status` | `active` or `archived`. Archived expeditions stay readable under the same visibility rules and reject new joins and credits. |
| `owner_user_id` | Null for the app-managed Global Everest Challenge. `ON DELETE RESTRICT`. |
| `starts_at`, `ends_at` | Optional. When both are set, `ends_at > starts_at`. The window applies to new credits only. |

Invitation rules that a normal unique constraint cannot express, because PostgreSQL allows many nulls:

- One pending username invitation per `(expedition_id, invited_user_id)`.
- One pending share-link invitation per expedition (`invited_user_id` is null).

Deleting a route cascades to its checkpoints. Deleting an expedition cascades to its invitations. Deleting a user cascades that user's invitations. The person who sent an invitation cannot be deleted while the row remains (`ON DELETE RESTRICT`).

## Authorization

The Flask database role bypasses row level security. Private access is enforced in `adventurer/access.py` and every read in `adventurer/platform.py`.

A missing expedition and a private expedition the viewer cannot read both return 404 with the same body. Withdrawn members lose private access unless they are the owner. The owner can still manage the expedition after leaving the leaderboard. Nobody can edit or archive an expedition whose owner is null.

## Downgrade

`migrations/sql/0005_routes_and_expeditions_down.sql` reverses the split only while the database is still the transformed Everest expedition: one route, slug `everest`, and one expedition, slug `everest`, public, active, with no owner and no invitations. User accounts and their credits may exist. Their ids stay. That is the path `tests/test_phase1_migration.py` uses when it downgrades to `0001_legacy_baseline`.

If any other route, expedition, or invitation exists, the downgrade raises and changes nothing. There is no safe automatic reverse once people have created expeditions. Restore that database from a backup taken before `0005_routes_and_expeditions`.

## API

Mutating requests use the existing CSRF token from `GET /api/v1/session`. Errors are `{"error": "..."}`.

`GET /api/v1/routes` returns published routes:

```json
{"routes": [{"slug": "everest", "name": "Everest", "region": "Khumbu, Nepal", "summary": "Start at sea level...", "summit_ft": 29032}]}
```

`GET /api/v1/routes/everest` adds ordered checkpoints and the public expeditions on that route. Draft and retired routes are 404.

`GET /api/v1/expeditions` lists public active expeditions only.

`GET /api/v1/expeditions/everest` returns the expedition, its route, checkpoints, the viewer's participation, and progress. A private slug the viewer cannot read is 404.

`POST /api/v1/expeditions` creates a private expedition and an active participation for the caller. `visibility` other than `private` is 400.

```json
{"route_slug": "everest", "name": "Friday Hills", "description": "Our group", "visibility": "private"}
```

`PATCH /api/v1/expeditions/<slug>` accepts `name`, `description`, and `owner_username`. `POST /api/v1/expeditions/<slug>/archive` archives it. Both require the owner.

`POST /api/v1/expeditions/<slug>/participation` joins or rejoins. `DELETE` withdraws and keeps credits.

`POST /api/v1/expeditions/<slug>/invitations` with `{"username": "Alex"}` creates or returns the pending username invitation. `{"kind": "link"}` returns a raw token once:

```json
{"id": 4, "expires_at": "2026-12-21T18:00:00+00:00", "token": "<shown once>", "kind": "link"}
```

`GET /api/v1/expeditions/<slug>/invitations` does not include raw tokens. `DELETE /api/v1/expeditions/<slug>/invitations/<id>` revokes a pending invitation.

`POST /api/v1/invitations/<token>/accept` joins the expedition. A share link stays pending so more than one person can use it. A username invitation becomes `accepted`. Decline is only for the invited account.

`GET /api/v1/expeditions/<slug>/leaderboard` lists active participants, including people with zero credited elevation, below anyone who has logged.

`GET /api/v1/activities` lists the signed-in user's outings and the expeditions that received a full-elevation credit. `POST` and `PATCH` use the same elevation, distance, date, and note rules as the Everest log. Changing elevation updates every credit for that outing in the same transaction. `DELETE /api/v1/activities/<id>` deletes the outing, its credits, and its verification history.

`POST /api/v1/activities/<id>/credits` with `{"expedition_slug": "everest"}` adds a full credit. A requested amount is ignored. `DELETE /api/v1/activities/<id>/credits/<expedition_slug>` removes that credit only.

`POST /api/v1/challenges/everest/logs` and `DELETE /api/v1/challenges/everest/logs/<id>` keep their previous shapes. Delete still removes only the Global Everest Challenge credit.

## Pages

`/routes` lists published routes. `/routes/<slug>` shows the route and its public expeditions. `/everest` remains the Global Everest Challenge. `/expeditions/new` creates a private expedition. `/expeditions/<slug>/manage` is the owner page. `/i/<token>` stores the token in the session and `/i` accepts it after login. `/activities` is the personal library.

## Deferred

User-created public expeditions, user-created routes, checkpoint editing, email delivery, a friend graph, chat, competition seasons, prize eligibility, and provider imports are not in this release.
