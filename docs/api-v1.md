# API v1

The mobile client calls these routes. The server-rendered HTML pages have been removed. Domain rules live in `docs/architecture.md` and in `adventurer/journeys.py`, `adventurer/activities.py`, and `adventurer/expeditions.py`. Home presentation lives in `docs/dynamic-home.md`. Handlers in `adventurer/api.py` only authenticate, validate the JSON body, and return what those services already decided. Endpoints and fields marked proposed are not implemented.

## Authentication

Two credentials identify the same user. They are not interchangeable.

- Cookie `session`, set by `POST /api/v1/auth/register` and `POST /api/v1/auth/login`. The cookie is HttpOnly, SameSite=Lax, and a thirty-day permanent session. Every state-changing cookie request must send the CSRF token from `GET /api/v1/session` or from the `csrf` field of the register or login response, in the form field `csrf` or the header `X-CSRF-Token`. A missing token is a JSON `csrf` error.
- Bearer token, returned once as `token` from register and login. The database stores only the SHA-256 hash in `api_tokens`. Send `Authorization: Bearer <token>`. A valid bearer request skips CSRF. The token lasts thirty days.

If an `Authorization` header is present and the token is missing, unknown, revoked, expired, or belongs to an account being deleted, the response is 401. The cookie is not consulted. `POST /api/v1/auth/logout` with a bearer token revokes that token and leaves the cookie alone. The same path with only a cookie clears the browser session. `POST /api/v1/auth/revoke` revokes every token for the user and does not clear the cookie.

Login, registration, and repeated bad bearer presentations share a process-local limit of five failures in fifteen minutes for that client address and name. An invalid invite code is a failed registration for that same key and is counted once. The response is 429 `rate_limited` and does not include the expected code. A successful login or registration clears that key. Expired timestamps are discarded, and a key with nothing left is removed, so unused names do not stay in the process dictionary. The count is not in the database and resets when the process restarts.

`GET /api/v1/session` returns `you` and, for a cookie caller, `csrf`. A bearer caller receives `csrf: null` and does not receive a new cookie session.

Registration still honors `INVITE_CODE` when that setting is non-empty. It creates no journey and no expedition participation.

Account deletion (`POST /api/v1/account/deletion`) is not implemented. The deletion batches described in the domain architecture do not have a service yet. A deleting account is rejected as unauthenticated once `deletion_state` is set.

## Response and errors

Success is a JSON object. Resource bodies from the services are returned directly. `Decimal` values are decimal strings. Timestamps are ISO-8601. Home progress fields that the home contract names in feet are JSON numbers.

Errors are:

```json
{"error": "Human readable message.", "code": "restart_required", "details": {}}
```

`details` is omitted when empty. Stable codes include `unauthenticated`, `csrf`, `forbidden`, `not_found`, `invalid`, `invalid_state`, `conflict`, `restart_required`, `confirmation_required`, `resume_required`, `slot_occupied`, `ownership_transfer_required`, and `rate_limited`. HTTP status comes from the service: 401, 400, 403, 404, 409, 422, or 429. A private expedition that the caller cannot see is 404 with the same message as a missing expedition. Database errors are not returned. Another person's journey is 403, which is the journey service's existing rule.

The acting user is always the authenticated account. A user id in a body is not accepted. Ownership transfer and organization membership take a username, and the service checks that the person is allowed to receive that role.

## Journeys

| Method | Path | Body |
| --- | --- | --- |
| GET | `/api/v1/journeys` | |
| GET | `/api/v1/journeys/active` | |
| GET | `/api/v1/journeys/<id>` | |
| POST | `/api/v1/journeys/start` | `route_slug` |
| POST | `/api/v1/journeys/switch` | `route_slug` |
| POST | `/api/v1/journeys/resume` | `route_slug` |
| POST | `/api/v1/journeys/abandon` | `route_slug` |
| POST | `/api/v1/journeys/restart` | `route_slug` |

GET does not start, switch, resume, abandon, or restart. Switch and resume return 409 `restart_required` when the current attempt is completed or abandoned. The details include `route_slug`, the journey `status`, and `reset_expedition_slugs`. Restart is the only call that opens the next attempt. It returns `journey`, `paused_journey`, and `reset_expedition_slugs`.

```json
POST /api/v1/journeys/switch
{"route_slug": "everest"}

409
{
  "error": "This route needs an explicit restart.",
  "code": "restart_required",
  "details": {
    "route_slug": "everest",
    "status": "completed",
    "reset_expedition_slugs": ["sam-everest"]
  }
}
```

## Activities

The fields in this section are the implemented contract. The target metric contract below replaces elevation-only credit. Elevation is feet, sent as an integer or a string, not a JSON float. `happened_on` is `YYYY-MM-DD`. Optional fields are `distance_km`, `note`, `activity_type`, `duration_s`, `started_at`, and `ended_at`. `distance_km` does not credit a journey.

| Method | Path |
| --- | --- |
| GET | `/api/v1/activities` |
| GET | `/api/v1/activities/<id>` |
| POST | `/api/v1/activities/preview` |
| POST | `/api/v1/activities` |
| POST | `/api/v1/activities/<id>/preview` |
| PATCH | `/api/v1/activities/<id>` |
| DELETE | `/api/v1/activities/<id>` |

Summit confirmation:

1. Preview the create or the elevation edit.
2. When `will_complete` is true, the preview includes `confirmation_token` and `confirmation_expires_at`.
3. Submit that token as `confirmation_token` with the same save.
4. The service locks the current journey and checks the token against the credit it just calculated. It does not trust the preview numbers.
5. A missing, stale, or expired token returns 409 `confirmation_required`, a fresh `preview` in `details`, and writes nothing.

An elevation edit that would finish an active journey uses that same token. The expedition feed then records `summit_reached` for active participations on that route. A completed journey refuses elevation edits and deletes of activities that credit it, with 409 `invalid_state`.

```json
POST /api/v1/activities/preview
{"elevation_ft": "100", "happened_on": "2026-01-02"}

{
  "preview": {
    "will_complete": true,
    "confirmation_token": "<hmac>",
    "credited_elevation_m": "30.48000"
  }
}
```

## Expeditions, invitations, and organizations

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/v1/expeditions/<slug>` | `mode` is `live` or `final` |
| POST | `/api/v1/expeditions` | private create: `slug`, `name`, `route_slug`, optional `description` and `confirm_new_attempt` |
| PATCH | `/api/v1/expeditions/<slug>` | private name and description |
| POST | `/api/v1/expeditions/<slug>/archive` | private close |
| POST | `/api/v1/expeditions/<slug>/join` | optional `confirm_new_attempt`; 409 `restart_required` or `slot_occupied` |
| POST | `/api/v1/expeditions/<slug>/leave` | ordinary leave; keeps the participation |
| POST | `/api/v1/expeditions/<slug>/owner` | `username` of an active participant |
| POST | `/api/v1/expeditions/<slug>/extend` | `ends_at`, timezone required |
| POST | `/api/v1/expeditions/<slug>/complete` | organization close |
| POST | `/api/v1/expeditions/<slug>/privacy-removal` | body `{"confirm": true}` only |
| GET | `/api/v1/expeditions/<slug>/roster` | |
| GET | `/api/v1/expeditions/<slug>/leaderboard` | live sums or final result rows |
| GET | `/api/v1/expeditions/<slug>/updates` | |
| GET | `/api/v1/expeditions/<slug>/invitations` | tokens are null |
| POST | `/api/v1/expeditions/<slug>/invitations` | `{"username": "Rita"}` or `{"kind": "link"}`; the raw token is returned once |
| GET | `/api/v1/invitations` | username invites addressed to the caller |
| POST | `/api/v1/invitations/accept` | `{"token": "..."}` for a share link |
| POST | `/api/v1/invitations/<id>/accept` | username invite; optional `confirm_new_attempt` |
| POST | `/api/v1/invitations/<id>/decline` | username invites only |
| POST | `/api/v1/invitations/<id>/revoke` | owner or organization manager |
| GET | `/api/v1/organizations` | the caller's active memberships |
| POST | `/api/v1/organizations` | platform admin: `slug`, `name`, `owner_username` |
| GET | `/api/v1/organizations/<slug>` | |
| GET | `/api/v1/organizations/<slug>/members` | owner, admin, or platform admin |
| POST | `/api/v1/organizations/<slug>/members` | `username`, optional `role` |
| PATCH | `/api/v1/organizations/<slug>/members` | `username`, `role` |
| DELETE | `/api/v1/organizations/<slug>/members` | `username` |
| GET | `/api/v1/organizations/<slug>/expeditions` | |
| POST | `/api/v1/organizations/<slug>/expeditions` | `slug`, `name`, `route_slug`, `enrollment` (`open` or `invitation`), schedule timestamps |
| POST | `/api/v1/admin/organizations/<slug>/owner` | platform admin, `username` |
| POST | `/api/v1/admin/organizations/<slug>/suspend` | platform admin; closes active expeditions, then suspends |

Ordinary leave withdraws the participation and keeps journey progress on the board when the person is still a participant later. A finisher who leaves keeps read access and a place in the final standings. Someone who leaves without finishing loses read access on a private expedition. Privacy removal is a different endpoint. It deletes that person's participation, feed, and result row, and a later rejoin does not put the removed total back on the board. The personal journey is not deleted.

```json
POST /api/v1/expeditions/owen-kili/privacy-removal
{"confirm": true}
```

`confirm` must be boolean true. Omitting it is 422 and writes nothing.

A second active organization expedition returns 409 `slot_occupied`. An enrollment window that has closed returns 409 `invalid_state`. Platform-admin routes check `PLATFORM_ADMIN_IDS` on the server. A request cannot grant that role.

## Routes

`GET /api/v1/routes` and `GET /api/v1/routes/<slug>` list published routes. The detail adds checkpoints and, when a session or bearer is present, the caller's journeys. Its `expeditions` list is the set someone can still join without an invitation:

- an active global expedition, such as Everest
- an active organization expedition with open enrollment, an active organization, and an enrollment window that is open now

Private expeditions, invitation-only expeditions, completed or archived expeditions, expeditions whose enrollment window has not opened or has already closed, and expeditions belonging to a suspended organization are left out. The same list is returned to an anonymous caller because the route catalog is public. Roster, board, and feed stay on the authenticated expedition routes. These route reads do not require a login. Expedition, journey, activity, home, and organization routes do.

Another person's activity or journey returns 403 `forbidden`, which is the service rule. A private expedition the caller cannot see returns 404.

## Home

`GET /api/v1/home` is aggregation only. It does not resume, restart, accept an invitation, or complete a due expedition. Expedition cards keep their raw private overrides or raw organization columns and add the resolved `branding` object from the branding section below. The journey `scene` does not use it. One home response stays one repeatable-read, read-only transaction. `docs/architecture.md` says lazy completion runs before a home read. This API does not do that, because a home GET is not allowed to change domain state. Due organization expeditions still complete on the next join or expedition read. The implemented home payload does not include journal bodies, discovery bodies, or arrival bodies. The active journey includes `pending_arrivals` for reached stories this attempt has not acknowledged. Incoming invitations include `branding` for the expedition being offered.

`journey` is the active journey, or null. A paused, completed, or abandoned attempt is never placed there. `focus` is `{journey_id, status: "active"}` when a journey is active, the full presentation of one finished attempt when nothing is active or paused, and null when the state is `ready` or `resume`. `state` is `ready`, `active`, `resume`, `completed`, or `abandoned`.

`next_actions` lists every eligible journey action in this order: `continue_journey`, `resume_journey` by route slug, `start_new_attempt` by route slug, `choose_route`. Invitation `accept_invitation` and `decline_invitation` stay on `incoming_invitations` and are not copied into `next_actions`. A share link has no decline action.

The home scene uses every required metric and the checkpoint thresholds, then the fallback: the reached checkpoint's file, then the nearest lower checkpoint by position, then `photos/<route>/hero` or `photos/<route>`, then null. It never uses an unmet checkpoint's file. Expedition cards use `displayed_metrics` and the board rule, including `display_credits_after`. That filtered total is not fed into the scene. Until a curated file exists, `image` is null rather than a borrowed checkpoint.

```json
{
  "you": {"id": 1, "name": "Sam"},
  "state": "active",
  "next_actions": [{"type": "continue_journey", "journey_id": 10, "route_slug": "everest"}],
  "journey": {"id": 10, "status": "active", "pending_arrivals": [], "scene": {"role": "route_hero", "checkpoint_slug": null}},
  "focus": {"journey_id": 10, "status": "active"},
  "paused_journeys": [],
  "expeditions": [],
  "incoming_invitations": []
}
```

## Target metric contract

Home, route catalog, and journey reads use the metric objects below. Activity writes accept `elevation_ft`, `distance_mi`, or both. An omitted measurement is not stored as zero. Authentication, CSRF, status codes, and error bodies do not change. Do not send `total_ft`, `remaining_ft`, `percent`, or `summit_ft`. Those fields are not returned.

The target shape gives every required metric the same object. Amounts are decimal strings in the display unit named by `unit`. `ft` is the international foot. `mi` is the statute mile. Storage and confirmation stay in meters. There is no route-level percent. A metric the route does not require is omitted.

`distance_km` on an activity is optional metadata in the current API and does not credit a journey. The target activity field is `distance_mi`. The implementation replaces `distance_km`. It does not accept both.

Journey and home progress:

```json
"metrics": [
  {"metric": "elevation", "unit": "ft", "target": "29032", "credited": "400", "remaining": "28632", "complete": false}
]
```

Route requirements and checkpoint thresholds use `target` or `threshold` in that same unit. They do not include `credited`.

Create and edit preview, then submit `confirmation_token`, when the proposed credits would satisfy every remaining requirement. The token is bound to the user, the journey, `progress_version`, and every proposed credit in storage units. A stale token is 409 `confirmation_required`, a fresh preview, and no write. An elevation edit, a distance edit, or an edit of both uses that path when it would complete the active journey. Delete does not send a confirmation token. It removes the activity and its credits, increments `progress_version` when a credited total changes, and cannot finish the route. A completed journey still refuses that delete.

Elevation-only route:

```json
{
  "slug": "everest",
  "name": "Everest",
  "region": "Khumbu, Nepal",
  "requirements": [
    {"metric": "elevation", "unit": "ft", "target": "29032"}
  ],
  "checkpoints": [
    {
      "slug": "lukla",
      "name": "Lukla",
      "position": 3,
      "requirements": [
        {"metric": "elevation", "unit": "ft", "threshold": "9383"}
      ]
    }
  ]
}
```

Distance-only route:

```json
{
  "slug": "towpath",
  "name": "Towpath",
  "region": "Canal",
  "requirements": [
    {"metric": "distance", "unit": "mi", "target": "12"}
  ],
  "checkpoints": [
    {
      "slug": "lock",
      "name": "Lock",
      "position": 1,
      "requirements": [
        {"metric": "distance", "unit": "mi", "threshold": "4"}
      ]
    }
  ]
}
```

Combined route:

```json
{
  "slug": "ridge-loop",
  "name": "Ridge Loop",
  "region": "Foothills",
  "requirements": [
    {"metric": "distance", "unit": "mi", "target": "2"},
    {"metric": "elevation", "unit": "ft", "target": "500"}
  ],
  "checkpoints": [
    {
      "slug": "gate",
      "name": "Gate",
      "position": 1,
      "requirements": [
        {"metric": "distance", "unit": "mi", "threshold": "1"},
        {"metric": "elevation", "unit": "ft", "threshold": "200"}
      ]
    },
    {
      "slug": "finish",
      "name": "Finish",
      "position": 2,
      "requirements": [
        {"metric": "distance", "unit": "mi", "threshold": "2"},
        {"metric": "elevation", "unit": "ft", "threshold": "500"}
      ]
    }
  ]
}
```

Requirement arrays are ordered by metric code. A checkpoint that lists both metrics unlocks only when both thresholds are met.

Preview that completes a combined route. The journey still needs 500 ft and 2 miles. The activity records 900 ft and 3 miles. The journey receives 500 ft and 2 miles. The activity keeps 900 and 3.

```json
POST /api/v1/activities/preview
{"elevation_ft": "900", "distance_mi": "3", "happened_on": "2026-01-02"}

{
  "preview": {
    "will_complete": true,
    "confirmation_token": "<hmac>",
    "confirmation_expires_at": 1768000000,
    "metrics": [
      {"metric": "distance", "unit": "mi", "current": "0", "remaining": "2", "proposed": "2", "resulting": "2"},
      {"metric": "elevation", "unit": "ft", "current": "0", "remaining": "500", "proposed": "500", "resulting": "500"}
    ]
  }
}
```

Home for that combined route at 500 ft and 1 mile. Elevation is complete and distance is not, so the journey stays active. The gate is reached because both of its thresholds are met. The finish checkpoint is next because its 2 mile threshold is not. `focus` does not repeat the metrics.

```json
{
  "you": {"id": 1, "name": "Sam"},
  "state": "active",
  "next_actions": [{"type": "continue_journey", "journey_id": 10, "route_slug": "ridge-loop"}],
  "journey": {
    "id": 10,
    "status": "active",
    "metrics": [
      {"metric": "distance", "unit": "mi", "target": "2", "credited": "1", "remaining": "1", "complete": false},
      {"metric": "elevation", "unit": "ft", "target": "500", "credited": "500", "remaining": "0", "complete": true}
    ],
    "checkpoint": {
      "current": {"slug": "gate", "name": "Gate", "position": 1},
      "next": {"slug": "finish", "name": "Finish", "position": 2}
    },
    "scene": {"role": "checkpoint", "checkpoint_slug": "gate"}
  },
  "focus": {"journey_id": 10, "status": "active"},
  "paused_journeys": [],
  "expeditions": [],
  "incoming_invitations": []
}
```

Roster and update payloads use the same metric objects for actual credited progress. `activity_logged` amounts are the credits applied, not the activity's full measurements. One create that crosses two checkpoints appends two `checkpoint_reached` rows. An ordinary edit appends neither `activity_logged` nor `checkpoint_reached`.

A live multi-metric leaderboard distinguishes an actual finish, a projected finish, and insufficient history. `live_rank` is not the snapshot's `final_rank`. Sam's elevation target is already met, so the projected date is only the distance estimate. Ada has finished both metrics and is not given a projected date.

```json
{
  "mode": "live",
  "entries": [
    {
      "display_name": "Ada",
      "rank_basis": "actual_completion",
      "live_rank": 1,
      "completed_at": "2026-06-01T15:00:00+00:00",
      "projected_completion_at": null,
      "metrics": [
        {"metric": "distance", "unit": "mi", "credited": "2", "complete": true},
        {"metric": "elevation", "unit": "ft", "credited": "500", "complete": true}
      ]
    },
    {
      "display_name": "Sam",
      "rank_basis": "projected_completion",
      "live_rank": 2,
      "completed_at": null,
      "projected_completion_at": "2026-06-20T00:00:00+00:00",
      "metrics": [
        {"metric": "distance", "unit": "mi", "credited": "1", "complete": false},
        {"metric": "elevation", "unit": "ft", "credited": "500", "complete": true}
      ]
    },
    {
      "display_name": "Lee",
      "rank_basis": "insufficient_data",
      "live_rank": null,
      "completed_at": null,
      "projected_completion_at": null,
      "metrics": [
        {"metric": "distance", "unit": "mi", "credited": "0.2", "complete": false},
        {"metric": "elevation", "unit": "ft", "credited": "40", "complete": false}
      ]
    }
  ]
}
```

A final leaderboard returns the snapshotted amounts and `final_rank`. It does not recompute them and it has no projected date. On a multi-metric route, finishers are ordered by completion time. Participants who did not finish keep their metric totals and a null `final_rank`. A single-metric board, live or final, still orders completed participants first and then ranks unfinished participants by credited amount, then earlier last credit, then display name. It does not use a forecast.

An activity may send `elevation_ft`, `distance_mi`, or both. Omit a metric that was not measured. Do not send zero to mean "unknown." Zero is a measured zero and does not by itself earn credit. A negative is rejected. An update may change either metric or both. Omitted progress fields stay as they are. The preview lists, for each required metric, the current total, the remaining amount, the proposed credit, and the resulting total. The token covers that whole result. A distance-only body is valid. An elevation-only body is valid.

## Narrative contract

Published chapters, the field journal, arrival acknowledgments, activity `arrivals`, and home `pending_arrivals` are implemented. Content seeding and scene selection from `route_media` are not. `docs/architecture.md` section 18 is the domain rule. `docs/dynamic-home.md` remains the scene rule. Amounts stay out of these payloads. Story text does not award credit.

A checkpoint is unlocked only when the caller's own journey totals meet every one of its thresholds. That rule is the same for an elevation-only route, a distance-only route, and a combined route. Unreached arrivals, journal bodies, and discoveries are not returned. Another person's journey is 403 `forbidden`. An unreached checkpoint, a draft or retired story, and an unknown slug are 404 `not_found` with the same message. Anonymous callers can see published chapter structure on the route. They cannot see arrival text, journal text, or discoveries.

`GET /api/v1/routes` and `GET /api/v1/routes/<slug>` gain a `chapters` array on the published route. The array contains only published chapters. Each item is `slug`, `title`, `position`, and `checkpoints` as slugs in checkpoint position order. No `arrival_body`, `journal_body`, discovery, or image. The existing `requirements` and `checkpoints` arrays stay. A draft or retired chapter is omitted.

`GET /api/v1/journeys/<id>/journal` is the Field Journal timeline for a journey the caller owns. It works for an active, paused, completed, or abandoned attempt. Restart does not copy acknowledgments onto the new attempt. The old attempt keeps its own timeline. The list contains only reached checkpoints that have a published story, in position order. It includes the short arrival and whether a journal entry and discoveries exist. It does not include `journal_body` or discovery bodies. A reached checkpoint whose story is draft or retired is omitted.

A reached checkpoint with a published story and no published chapter stays in the timeline. A draft or retired chapter counts as no chapter. Consecutive checkpoints in that state form one chapter object whose `slug`, `title`, and `position` are null, placed where those checkpoints fall in route order. The story detail returns `chapter_slug: null` for the same checkpoint.

```json
{
  "journey_id": 10,
  "status": "active",
  "chapters": [
    {
      "slug": "approach",
      "title": "The approach",
      "position": 0,
      "entries": [
        {
          "checkpoint_slug": "gate",
          "checkpoint_name": "Gate",
          "position": 1,
          "arrival_title": "The gate",
          "arrival_body": "A short reveal.",
          "has_journal": true,
          "discovery_count": 2,
          "acknowledged": false
        }
      ]
    }
  ]
}
```

`GET /api/v1/journeys/<id>/journal/<checkpoint_slug>` returns the unlocked published story. `journal` is null when the checkpoint has no journal pair. `discoveries` are published rows in position order. Each discovery carries its own published images. The story `media` array is the published journal images only. The checkpoint scene image is not repeated here. Home already selected it. Every returned media row has `alt_text`, `rights_basis`, `license`, and the other attribution section 18.4 required for that basis. `author`, `license_url`, and `source_url` are omitted when that basis does not require them and the stored value is null. `permission_note` is not returned. `path` is a site-relative URL. The client resolves it against the backend origin. It is not a remote URL. The server returns a path only when the file is inside the curated `/static/photos/` directory. A missing file, a path outside that directory, a malformed or remote path, or a symlink that leaves the directory is omitted rather than returned with empty rights.

```json
{
  "journey_id": 10,
  "checkpoint_slug": "gate",
  "chapter_slug": "approach",
  "arrival_title": "The gate",
  "arrival_body": "A short reveal.",
  "journal": {"title": "Below the ridge", "body": "The longer entry."},
  "discoveries": [
    {
      "slug": "stone",
      "kind": "historical",
      "title": "The old gate",
      "body": "A longer note.",
      "position": 0,
      "media": []
    }
  ],
  "media": [
    {
      "role": "journal",
      "path": "/static/photos/gate-journal.jpg",
      "alt_text": "The gate from the path",
      "rights_basis": "creative_commons",
      "author": "Ada Lovelace",
      "license": "CC BY 4.0",
      "license_url": "https://creativecommons.org/licenses/by/4.0",
      "source_url": "https://commons.wikimedia.org/wiki/File:Example.jpg",
      "position": 0
    }
  ],
  "acknowledged": false
}
```

`POST /api/v1/journeys/<id>/journal/<checkpoint_slug>/acknowledgment` records that this attempt has seen the arrival. The row is unique to this journey id and checkpoint. A restarted attempt is a different journey and does not see this row. Cookie callers send the CSRF token. The body is empty. The response is the existing `acknowledged_at` when the row is already there. A second call does not change `progress_version`, metrics, journey status, or the expedition feed. A checkpoint this journey has not reached, or a story that is not published, is 404 and writes nothing.

```json
{"journey_id": 10, "checkpoint_slug": "gate", "acknowledged_at": "2026-06-01T15:04:00+00:00"}
```

`POST /api/v1/activities` and `PATCH /api/v1/activities/<id>` add `arrivals` for checkpoints that this write newly unlocked. A create uses the active journey. An update uses the journey that activity already credits. That journey may be paused or abandoned when the edit does not complete it. The write that completes an active journey includes the final arrival, and `journey.status` in that response is completed. One activity that crosses two checkpoints returns both, in position order, for whichever metrics that route requires. The objects are the short reveal only. `journal_body` and discoveries are not included. The client loads those from the journal detail route. `journey.id` in the same response is the attempt. When the activity credits nothing, `journey` is null and `arrivals` is empty. No second journey identifier is added. An edit that unlocks nothing returns `"arrivals": []`. A 409 `resume_required` or `confirmation_required` rejection writes nothing and returns no arrival list. Preview does not return arrivals. `DELETE /api/v1/activities/<id>` keeps its `activity_id` and journey summary and returns `"arrivals": []`. None of these writes an acknowledgment. This list does not create `expedition_updates`. The feed rule in `docs/architecture.md` section 16.4 stays as it is.

```json
{
  "activity": {"id": 40},
  "journey": {"id": 10, "status": "active"},
  "arrivals": [
    {
      "checkpoint_slug": "gate",
      "chapter_slug": "approach",
      "arrival_title": "The gate",
      "arrival_body": "A short reveal.",
      "acknowledged": false
    }
  ]
}
```

`GET /api/v1/home` adds `pending_arrivals` on the active journey: `checkpoint_slug`, `chapter_slug`, and `arrival_title` for reached checkpoints that this attempt has not acknowledged. It does not add `arrival_body`, `journal_body`, discoveries, or extra images. Paused, abandoned, and completed attempts do not put those titles on the home scene. Their journals stay on the journey routes. The scene image comes from one published `route_media` row. `image.path` is the stored site-relative `asset_path`, such as `/static/photos/lukla.jpg`. `image.alt` is that row's alt text. `image.credit` carries `author`, `license`, `license_url`, `source_url`, and `rights_basis`. `permission_note` is not returned. A route with no readable published hero or checkpoint scene has `image` null. A file on disk without a published row is not used.

## Branding

`docs/architecture.md` section 19 is the rule. Resolution, validation, and the branding edits below are implemented. Logo and cover upload is not. The mobile client does not paint these fields yet.

A reader that is allowed to see the surface receives one resolved object. The server applies The Adventurer defaults (`#18354C`, `#5095B6`, null logo, null cover, null welcome), then the organization when the expedition kind is organization, then that expedition's accent, cover, and welcome. The client does not merge those layers. `image_ref` is the cover. It is not a route scene. If that resolved file fails to load, the client uses a product placeholder or omits the image.

```json
"branding": {
  "primary_color": "#18354C",
  "accent_color": "#5095B6",
  "logo_ref": null,
  "image_ref": null,
  "welcome_message": null
}
```

`primary_color` and `accent_color` are always `#RRGGBB` in uppercase. `logo_ref`, `image_ref`, and `welcome_message` are null when the chain did not supply them. A stored empty welcome is null here. An invalid stored color is skipped and the parent color is used.

Management responses keep the raw fields beside that object.

- Organization list and `GET /api/v1/organizations/<slug>` keep `logo_ref`, `image_ref`, `colors`, and `welcome_message`. `colors` is the stored object, `{"primary": "#18354C", "accent": "#5095B6"}`, or null. `welcome_message` stays the stored string, including `""`. These routes stay member or platform-admin reads. `branding` on them is the organization layer with no expedition override.
- An expedition profile keeps `accent_color`, `welcome_override`, `image_ref`, and `organization_id`, and adds `branding` resolved for that expedition.
- A home expedition card adds `branding`. The implemented private `accent_color`, `welcome_override`, and `image_ref`, and the implemented `organization` object, stay raw. The card paint uses `branding`. The journey `scene` does not.
- `GET /api/v1/invitations`, an expedition's invitation list, and `incoming_invitations` on home add `branding` for an invitation the caller can already see.
- Each expedition already listed on `GET /api/v1/routes/<slug>` adds `branding`. That object is the open-enrollment brand. The entry does not add the organization, its membership, or a roster. `GET /api/v1/routes` stays an unbranded route catalog.

Global Everest resolves to the product colors and null logo, cover, and welcome, even if a legacy override is stored. A private expedition has no organization layer. An organization expedition inherits the organization and then its own `accent_color`, `welcome_override`, and `image_ref`. There is no expedition primary-color field. A completed or archived expedition still resolves live. Close does not copy branding into the result snapshot.

`PATCH /api/v1/organizations/<slug>/branding` edits the organization. The caller is an active owner, an active admin, or a platform administrator. A member receives 403 `You cannot edit this organization.` A caller who cannot see the organization receives 404. An owner or admin of a suspended organization receives 409 `This organization is suspended.` A platform administrator may still edit it. A missing key is left unchanged. `null` clears that key.

```json
{
  "colors": {"primary": "#112233", "accent": "#445566"},
  "welcome_message": "A season on the ridge.",
  "logo_ref": null,
  "image_ref": null
}
```

`colors` is that object or `null`. Supported keys are `primary` and `accent`. A supplied non-null token must be a six-digit hex color and is stored in uppercase. A supplied null token is the same as omitting that token, and the resolved color inherits its parent default. The object replaces the previous complete `colors` object. It is not a token-by-token merge. This request:

```json
{"colors": {"primary": "#233E34", "accent": null}}
```

is stored as `{"primary": "#233E34"}`. The resolved accent is the product default `#5095B6`. `{}` stores neither organization color. `null` clears the complete color configuration. Omitting the top-level `colors` field leaves the stored object unchanged. `welcome_message` is text of at most 280 characters, or `null`. Blank text is stored as `""` because the column is not null. `logo_ref` and `image_ref` accept only `null`. A string is 400 `Set a logo through the branding upload.` or `Set a cover through the branding upload.` Unknown keys, including `theme`, are 400 `That branding field is not supported.`

`PATCH /api/v1/expeditions/<slug>/branding` edits expedition overrides. An active organization owner or admin may edit that organization's expedition. The private expedition's owner may edit that private expedition. A platform administrator may edit either. A participant or ordinary member receives 403 `You cannot manage this expedition.` A caller who cannot see the expedition receives 404. A completed or archived expedition receives 409 `This expedition is not open.` Global Everest receives 403 `This expedition keeps the product brand.` for every caller.

```json
{
  "accent_color": "#AABBCC",
  "welcome_override": "This season only.",
  "image_ref": null
}
```

`accent_color` is `#RRGGBB` or `null`. `welcome_override` is text of at most 280 characters, or `null`. Blank text is stored as `null` and inherits. `image_ref` accepts only `null`. There is no expedition logo and no expedition primary. Unknown keys are rejected.

`POST /api/v1/organizations/<slug>/expeditions` uses the same accent and welcome rules when those keys are present. An `image_ref` string is rejected. Omitted branding keys stay null.

Colors are stored and returned in uppercase. Three-digit hex, eight-digit hex, alpha, `rgb()`, `hsl()`, named colors, and non-string tokens are 400 `Use a color like #18354C.` A welcome that is not text is 400 `Send the welcome as text.` A welcome over 280 characters is 400 `Keep the welcome under 280 characters.` A colors value that is not an object or null is 400 `Send colors as an object or null.` A colors object with another key is 400 `Colors accept only primary and accent.`

A read of a legacy bad token inherits the parent color and does not update the row. Logo and cover bytes are not accepted on these routes. The path layout is section 19.7 and the bucket is not created. Organization creation stays platform-admin only. Joining an expedition does not make the participant an organization manager.

## What the Expo client needs first

1. Call register or login and store the bearer token. Do not send it as a query parameter or a cookie.
2. Send `Authorization: Bearer` on later requests. Native clients do not need CSRF. A browser client that uses the cookie must send `X-CSRF-Token`.
3. Treat 409 `restart_required` and `confirmation_required` as instructions to show a confirmation step, then call restart or resubmit with the new token. Do not retry a stale summit token.
4. Use `GET /api/v1/home` for the first screen. The implemented payload reports elevation in feet. The target payload in the metric section reports one object per required metric, in feet or miles, and does not add those objects together. Activity elevation is feet. The target distance field is miles.
5. Account deletion, anonymous public expedition profiles, and push or email delivery are not available yet. The HTML activity and expedition pages still call the legacy platform module and are not this API. Achievement and trophy-case routes are not in this contract. The domain is `docs/architecture.md` section 17. Chapter, journal, acknowledgment, activity arrival, and home pending-arrival routes are implemented. Content seeding and scene selection from `route_media` are not.
