# API v1

The mobile client should call these routes and ignore the HTML pages. Domain rules live in `adventurer/journeys.py`, `adventurer/activities.py`, and `adventurer/expeditions.py`. Handlers in `adventurer/api.py` only authenticate, validate the JSON body, and return what those services already decided.

## Authentication

Two credentials identify the same user. They are not interchangeable.

- Cookie `session`, set by `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, and the existing HTML join and login forms. The cookie is HttpOnly, SameSite=Lax, and a thirty-day permanent session. Every state-changing cookie request must send the CSRF token from `GET /api/v1/session` or from the `csrf` field of the register or login response, in the form field `csrf` or the header `X-CSRF-Token`.
- Bearer token, returned once as `token` from register and login. The database stores only the SHA-256 hash in `api_tokens`. Send `Authorization: Bearer <token>`. A valid bearer request skips CSRF. The token lasts thirty days.

If an `Authorization` header is present and the token is missing, unknown, revoked, expired, or belongs to an account being deleted, the response is 401. The cookie is not consulted. `POST /api/v1/auth/logout` with a bearer token revokes that token and leaves the cookie alone. The same path with only a cookie clears the browser session. `POST /api/v1/auth/revoke` revokes every token for the user and does not clear the cookie.

Login, registration, and repeated bad bearer presentations share a process-local limit of five failures in fifteen minutes for that client address and name. The response is 429 `rate_limited`. The count is not in the database and resets when the process restarts.

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

Elevation is feet, sent as an integer or a string, not a JSON float. `happened_on` is `YYYY-MM-DD`. Optional fields are `distance_km`, `note`, `activity_type`, `duration_s`, `started_at`, and `ended_at`.

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

`GET /api/v1/routes` and `GET /api/v1/routes/<slug>` list published routes. The detail adds checkpoints, the caller's journeys when a session or bearer is present, and the global or open-enrollment expeditions on that route. Private and invitation-only expeditions are not included. These reads do not require a login. Expedition, journey, activity, home, and organization routes do.

Another person's activity or journey returns 403 `forbidden`, which is the service rule. A private expedition the caller cannot see returns 404.

## Home

`GET /api/v1/home` is aggregation only. It does not resume, restart, accept an invitation, or complete a due expedition. Phase 4A says lazy completion runs before a home read. This API does not do that, because a home GET is not allowed to change domain state. Due organization expeditions still complete on the next join or expedition read.

`journey` is the active journey, or null. A paused, completed, or abandoned attempt is never placed there. `focus` is `{journey_id, status: "active"}` when a journey is active, the full presentation of one finished attempt when nothing is active or paused, and null when the state is `ready` or `resume`. `state` is `ready`, `active`, `resume`, `completed`, or `abandoned`.

`next_actions` lists every eligible journey action in this order: `continue_journey`, `resume_journey` by route slug, `start_new_attempt` by route slug, `choose_route`. Invitation `accept_invitation` and `decline_invitation` stay on `incoming_invitations` and are not copied into `next_actions`. A share link has no decline action.

The scene uses the capped personal journey total in meters. It shows the reached checkpoint's curated file, then the nearest lower checkpoint file, then `photos/<route>/hero` or `photos/<route>`, then null. It never uses the next checkpoint's file. Expedition `displayed_total_ft` follows the board rule, including `display_credits_after`, and is not the scene input. Until a curated file exists, `image` is null rather than a borrowed checkpoint.

```json
{
  "you": {"id": 1, "name": "Sam"},
  "state": "active",
  "next_actions": [{"type": "continue_journey", "journey_id": 10, "route_slug": "everest"}],
  "journey": {"id": 10, "status": "active", "scene": {"role": "route_hero", "checkpoint_slug": null}},
  "focus": {"journey_id": 10, "status": "active"},
  "paused_journeys": [],
  "expeditions": [],
  "incoming_invitations": []
}
```

## What the Expo client needs first

1. Call register or login and store the bearer token. Do not send it as a query parameter or a cookie.
2. Send `Authorization: Bearer` on later requests. Native clients do not need CSRF. A browser client that uses the cookie must send `X-CSRF-Token`.
3. Treat 409 `restart_required` and `confirmation_required` as instructions to show a confirmation step, then call restart or resubmit with the new token. Do not retry a stale summit token.
4. Use `GET /api/v1/home` for the first screen. Journey totals on that payload are feet. Activity elevations submitted to the API are also feet.
5. Account deletion, anonymous public expedition profiles, and push or email delivery are not available yet. The HTML activity and expedition pages still call the legacy platform module and are not this API.
