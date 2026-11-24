The Adventurer — dynamic home backend

Supporting presentation specification. docs/architecture.md is the domain source of truth for routes, journeys, expeditions, credits, authorization, and the narrative model. This document governs the home read model and the scene fallback. It does not own lifecycle rules, and it does not design the screen. Narrative bodies are specified in docs/architecture.md section 18 and docs/api-v1.md. They are not part of this payload. The active journey names unacknowledged arrivals by title.


1. Executive summary

The home payload is a presentation of one journey's position on a shared route. The only progress input is that journey's credited total for each metric the route requires, each capped at its own target. docs/architecture.md section 16 defines those metrics. The scene is the curated photograph of the highest checkpoint those totals have reached. Below the first checkpoint, and whenever that photograph is missing, the scene falls back along the route. It never looks ahead to a checkpoint the journey has not reached, and it never stores a separate visual-progress value. There is no combined percent.

GET /api/v1/home is the endpoint. docs/architecture.md already named it. A /me/home path would be a second name for the same authenticated read. The handler composes journey, route, expedition, and invitation reads. It does not own lifecycle rules, and activity saving does not select an image.

Home adds no scene table and no second progress column. docs/architecture.md section 16 replaces the elevation line with route and checkpoint requirements. This document only reads them. The home scene reads published route_media rows. The files stay under photos/. adventurer/photo_credits.json is archival editorial attribution and is not a runtime lookup. Organization and expedition branding columns already exist and stay on those owners. They decorate an expedition card. They do not replace the route scene. Storage, optimization, and delivery of the photograph files are docs/media-infrastructure.md.


2. Domain ownership and responsibilities

Route
  Shared geography. The route owns its metric requirements, the ordered checkpoints and their thresholds, the narrative in challenges.py, and the curated photographs. Every user and every expedition on that route sees the same places and the same pictures. A route does not know who is on it.

Route journey
  One user's attempt at one route. It owns status, attempt number, is_current, and progress_version. Its position is the capped credit total of each required metric. Those totals are the only input to scene selection. Switching, resuming, abandoning, and restarting stay in adventurer/journeys.py. Those operations do not copy progress and do not choose an image.

  The journey service remains a lifecycle service. It does not learn about photographs, expeditions, or the home payload.

Expedition
  One group on exactly one route. It owns identity, kind, organization link, branding overrides, participants, the live or final board, and its own lifecycle. Its image_ref, accent_color, and welcome_override are expedition presentation. They are not a second route illustration.

Expedition participation
  The link from a user to an expedition. It points at the journey the expedition currently displays. It does not own any metric. Home reads the pointer. It does not invent a participation total.

Organization
  Owns membership and the branding fields already on organizations: logo_ref, image_ref, colors, and welcome_message. Membership is not required to participate in an open organization expedition. Home may show that public branding to a participant who is not a member. It does not show the membership list.

Home composer
  A new read-only module, called by GET /api/v1/home. It asks the journey service for the user's journeys, a route read for checkpoints, the existing view_model for the current and next stop, a pure scene function for the image, and expedition and invitation reads for the surrounding context. It lists the actions the user is eligible to take. It does not order them for display, and reading home does not perform any of them.

Scene function
  A pure function. Inputs are the route's checkpoints and their requirement thresholds, the journey's capped storage totals, and the curated file lookup. Output is the selected place and the image that survives fallback. It has no database writes and it is not called from activity create, update, or delete.

Activity service
  Later work writes activities and journey credits. After commit, the next home read sees the new metric totals and, only when a checkpoint's thresholds are all met, a different scene. The activity transaction does not resolve a photograph.

Trophy case
  A personal collection of earned achievements, specified in docs/architecture.md section 17. Home does not read it and does not select a scene from it. A leaderboard place and a participation are not awards on this payload.


3. Dynamic scene-selection rules

Position comes from checkpoint requirements. Walk the route's checkpoints in ascending position. A checkpoint is reached only when every one of its thresholds is less than or equal to the journey's capped storage total for that metric. Meeting one threshold of a two-metric checkpoint does not reach it. Before the first checkpoint, nothing has been reached. The highest reached checkpoint is the reached one with the greatest position. Display rounding is not the comparison. Storage meters are.

Position is the only order. The earlier rule that elevation wins when it disagrees with position is withdrawn. docs/architecture.md requires thresholds to be monotonic along position, so a later checkpoint cannot be reached while an earlier one is not.

The journey totals are the sums of that journey's credits, each capped at its own target. display_credits_after does not apply. docs/architecture.md already says the personal journey, including home, shows the full journey. An expedition card on the same payload uses the board rule, including display_credits_after, for every metric, and those numbers are not fed back into the scene.

Selection, in order:

  1. If a checkpoint has been reached and that checkpoint has a curated image, the scene is that image. role is checkpoint.
  2. If the reached checkpoint has no image, walk downward by position and use the nearest lower checkpoint that has one. role is checkpoint. fallback is lower_checkpoint. The response still names the reached checkpoint, so the client can tell the place from the file that was found.
  3. If no reached checkpoint has an image, or no checkpoint has been reached, use the route hero. role is route_hero. fallback is route_hero when a reached checkpoint was skipped for lack of a file, and null when the journey is simply below the first checkpoint.
  4. If the route hero is also missing, image is null. role is none. fallback is none. The checkpoint identity is still returned.

The next checkpoint is reported separately. Its image is not the scene. A locked place stays locked, which is the current site rule that a stop photo is available only once the stop is unlocked. Between checkpoints the scene stays on the place already reached until every threshold of the next checkpoint is met. Meeting a threshold exactly counts as reaching it.

The route hero is the picture of the approach, before the first checkpoint opens. Everest's hero, when a reviewed package publishes one, shows the broader Everest and Khumbu setting, and it is not a photograph of Lukla, Timms Hill, or any later checkpoint. The opening of the journey then has its own scene without implying the person has reached a place they have not. Until that row is published, the selector returns image null rather than borrowing a checkpoint photograph or a file that merely shares a slug.

The choice is deterministic. The same route files and the same metric totals always return the same scene. There is no per-user scene row, no viewed-at marker, and no priority column.


4. Recommended data model and schema changes

What already exists

  routes has slug, name, region, summary, target_elevation_m, and status. It has no image column.
  route_checkpoints has slug, name, position, and elevation_m, unique per route for position, slug, and elevation. It has no image column.
  route_media holds the published hero, checkpoint scene, journal images, and discovery images. Home reads the first two roles. It does not discover a photograph from a filename.
  photo_credits.json remains beside the historical files as archival editorial attribution. Runtime scene selection does not open it.
  expeditions already has accent_color, welcome_override, and image_ref.
  organizations already has logo_ref, image_ref, colors, and welcome_message.
  Organization and expedition image bytes stay out of PostgreSQL. Resolution of the existing columns is implemented. Upload and mobile painting of those files are not. This design does not add an asset-management system.

Required migration

  None in this document. docs/architecture.md section 16 adds the requirement rows and then drops target_elevation_m and checkpoint elevation_m as progress inputs. Home does not add a media table, a segment table, or a progress-image column. Any of those would create a second geography or a second progress value. route_media chooses this scene and does not store a second total.

Image resolver

  adventurer/scene.py performs the lookup below, on the caller's database transaction. Activity services do not import it. Journey transitions do not import it. Whether a file exists has no effect on journey progress, checkpoint unlocks, or authorization. A missing picture changes only scene.image.

  One select loads the route's published route_hero and checkpoint_scene rows. Draft and retired rows are left out. A row whose asset_path is missing, malformed, or outside the curated photos directory is left out. The selector does not then search the directory by slug.

  Selection order:
    1. The highest reached checkpoint with a readable published checkpoint_scene.
    2. The nearest lower reached checkpoint with a readable published checkpoint_scene.
    3. The route's readable published route_hero.
    4. Null when no eligible image exists.

  A checkpoint is reached only when every required metric threshold is met. An unreached checkpoint's row is never selected. checkpoint_slug names the highest reached checkpoint, or is null before the first one. fallback is null for that checkpoint's own scene and for the approach hero, lower_checkpoint for an earlier reached stop's scene, route_hero when the reached stops have no eligible scene, and none when the image is null.

  image.path is the row's asset_path. It is site-relative, such as /static/photos/lukla.jpg, and the client resolves it against the backend origin. image.alt is the row's alt text. image.credit carries author, license, license_url, source_url, and rights_basis. permission_note is not returned. A route with no published media registry entries has image null. That is the state before Everest launch content is imported.

Optional later columns

  Nullable routes.hero_image_ref and route_checkpoints.image_ref, used only when a curator needs a path that the convention cannot express. Null means "use the convention." They are not required for Everest or for the first home implementation.

  This home model does not add an asset library, an upload path, or a CMS. One scene image per reached checkpoint, then the fallback, is the whole scene. Ordering is checkpoint position among reached stops. Journal and discovery rows in route_media stay off this payload. They do not replace this fallback and do not select an unreached photograph.

What home must not store

  No scene id on route_journeys. No last-image column. No user-specific copy of a route photograph. progress_version remains the credit-generation counter from docs/architecture.md. It is not an image version.


5. Proposed home API contract

Endpoint

  GET /api/v1/home

  Cookie or bearer identifies the user, as on every other personal route. Anonymous callers receive 401. A bearer that is missing, unknown, revoked, or expired does not fall through to the cookie. GET does not require CSRF.

  /api/v1/me/home is not added. The API has no /me collection. docs/architecture.md already assigned this response to /api/v1/home, and Phase 10 already accepts that payload.

The handler is aggregation only. Journey status changes remain POST /api/v1/journeys/switch, resume, abandon, and restart. Route detail remains GET /api/v1/routes/<slug>. Expedition boards, rosters, and feeds remain on the expedition routes. Home does not embed those collections.

journey and focus

  These are different objects. Reading home never changes either of them.

  journey is the active journey defined in docs/architecture.md, or null. When it is present its status is active, so this is the only journey that can receive credits. It carries the route and the progress that document assigns to it: id, status, attempt_number, progress_version, a metrics array, route slug, name, region, the curated scene, the current and next checkpoints, and pending_arrivals. Each pending arrival is the checkpoint slug, the published chapter slug or null, and the arrival title for a reached story this attempt has not acknowledged. A paused, completed, or abandoned attempt is never placed here, and those attempts do not carry pending_arrivals. The journey is complete only when status is completed, which is when every metric in that array has complete true. There is no summited flag and no route-level percent beside the array.

  focus is the journey context the home scene centers on. It does not grant an active status and it does not make a finished attempt eligible for credits.
    When an active journey exists, focus is a reference: journey_id and status active. The route, progress, checkpoints, and scene stay on journey. They are not copied.
    When the center is a completed or abandoned current attempt, journey is null and focus carries that attempt's presentation: journey_id, status, attempt_number, progress_version, the metrics array, route, checkpoints, and scene. status is completed or abandoned. Credits are refused for that attempt. A new attempt requires the restart action, which creates a different journey. Focus does not keep a second copy of those metrics when an active journey is present.
    When the center is neither an active journey nor a finished attempt, focus is null. That is the ready state, and the state in which only paused journeys exist.

  state names that center: active, resume, completed, abandoned, or ready. It does not remove actions.

Response shape

  you
    id, name. The same viewer object the session route returns.

  state
    ready, active, resume, completed, or abandoned. See section 6.

  next_actions
    Every journey action the caller is eligible to perform, in the stable order below. The array is not a ranking. The client chooses visual prominence. Home does not drop a valid action because another one is more central.

  journey
    The active journey, with its route and progress, or null. See above.

  focus
    A reference to journey when state is active. The full presentation of one finished attempt when state is completed or abandoned. Null when state is ready or resume.

  paused_journeys
    Current paused attempts. Each has the same progress shape as an active journey, including its route and scene, and is not focus. docs/architecture.md asked for that shape without making a paused journey the home center. The matching resume_journey action carries only ids. The progress body lives once, on this entry.

  expeditions
    The caller's active participations. Each has expedition slug, name, kind, status, route_slug, contributing, and displayed_metrics.
    contributing is true only when the participation's journey is the active journey.
    displayed_metrics uses the same metric object as the journey, with credited and remaining taken from the board rule: the pointed journey, each metric capped, and limited by display_credits_after when that timestamp is set. It is not a copy of the personal journey totals when that filter differs.
    Private rows add accent_color, welcome_override, and image_ref.
    Organization rows add the public organization branding object. Global rows have no organization.
    Withdrawn participations are omitted. A retained-read expedition stays on its own endpoint.

  incoming_invitations
    Pending invitations the caller may act on. Home does not accept or decline them. Each row holds the display context once: id, kind, expires_at, and expedition slug, name, and route_slug. Its actions array uses the action object below and contains only accept_invitation and, for a username invite, decline_invitation. Those actions are not repeated in next_actions.

Action object

  type
    A stable code. The home journey codes are continue_journey, resume_journey, start_new_attempt, and choose_route. Invitation rows use accept_invitation and decline_invitation. No other type is emitted. None of these types is a new write. continue_journey and choose_route do not change the database. resume_journey is POST /api/v1/journeys/resume. start_new_attempt is POST /api/v1/journeys/restart. accept_invitation and decline_invitation are the existing invitation accept and decline paths.

  Targets
    continue_journey and resume_journey: journey_id and route_slug. One resume_journey is returned for each paused journey. A resume action without a journey id is not returned.
    start_new_attempt: journey_id, route_slug, and status completed or abandoned. The status says why a new attempt is offered. It does not make that journey active and it does not accept credits on it.
    choose_route: no target. Returned only when the user has no current journey on any route.
    accept_invitation and decline_invitation: invitation_id only. The expedition and route stay on the invitation row.

  Eligibility
    continue_journey: the single active journey.
    resume_journey: each current journey whose status is paused. A completed journey is not resumed from home.
    start_new_attempt: each current journey whose status is completed or abandoned. An active or paused journey is not offered here. Restart of an in-progress journey remains available on the journey endpoint and is not a home action.
    choose_route: no current journey exists.
    accept_invitation: each pending invitation this user may accept.
    decline_invitation: a pending username invitation this user may decline. A share link does not get one. docs/architecture.md keeps a share link pending across accepts until it expires or is revoked.

  Order inside next_actions, which is not display priority:
    continue_journey, then resume_journey by route_slug, then start_new_attempt by route_slug, then choose_route.
    Invitation actions stay on their invitation, accept_invitation before decline_invitation. Invitations are ordered by id.

  Activity eligibility is not in the implemented home payload. `docs/architecture.md` section 20 is the approved rule and is not built. When it is, the active, paused, or focused journey includes `activity_policy` and, when a choice is waiting, `pending_policy_choice`. The scene still uses that journey's capped personal totals. A pending choice does not change the totals, the checkpoint, or the scene. Home remains one repeatable-read, read-only transaction and does not apply the choice.

  The proposed action is `resolve_activity_policy`, placed ahead of `continue_journey` only while that choice is pending on the current journey. It carries the journey, the route slug, whether the governor was left or closed, the former expedition slug, and the choices the server will accept. `keep` retains the current list. `open` is included only when no other active expedition would reject an unrestricted journey. Otherwise `blocked_by` names those expeditions and `open` is omitted. Opening and a join that changes policy both go through the policy preview in `docs/api-v1.md`. The client does not estimate the resulting checkpoint.

Progress fields, wherever a progress body is carried

  metrics: one object per required metric, in metric-code order. Each object is metric, unit, target, credited, remaining, and complete. Amounts are decimal strings in the display unit. unit is ft or mi. complete is true only when that metric's capped credit has reached its target. A metric the route does not require is omitted, not sent as zero.
  route: slug, name, region, and the same requirements the route detail returns: metric, unit, and target. The route object does not repeat the journey's credited amounts.
  checkpoint.current: null, or slug, name, position, and requirements. Each requirement is metric, unit, and threshold.
  checkpoint.next: null, or the same shape. It has no photo path.
  scene.role: checkpoint, route_hero, or none
  scene.checkpoint_slug: the reached checkpoint, or null below the first one
  scene.image: null, or path, alt, credit
  scene.fallback: null, lower_checkpoint, route_hero, or none

  On an active journey these fields live on journey. On a paused journey they live on that paused_journeys entry. On a finished focus they live on focus. They are not copied onto a second object for the same attempt.

  image.path is the published asset_path, a stable site-relative path such as /static/photos/lukla.jpg. It does not contain the journey id, a metric total, or progress_version. image.credit uses the public route_media fields, including rights_basis. The selector compares storage meters. The client displays the metric strings. The next checkpoint has no photo path. It is not unlocked.

The home screen stays a picture of one place on one route, with one indicator per required metric of the journey in focus. A paused journey card repeats that shape for its own route. The screen does not become a fitness dashboard: no combined score, no chart of unrelated history, and no indicator for a metric the route does not require.

Examples later in this file that still print total_ft, remaining_ft, percent, summit_ft, elevation_ft, or displayed_total_ft are elevation-only illustrations from before section 16. They are not extra fields. Read each of those numbers as the single elevation metric of an elevation-only route. Do not emit them beside metrics.

Errors stay a human error string plus a stable code. This route adds no new code. Unauthenticated is the existing 401.

Representative responses

No journey yet. state ready. The only journey action is choose_route.

  {
    "you": {"id": 1, "name": "Sam"},
    "state": "ready",
    "next_actions": [{"type": "choose_route"}],
    "journey": null,
    "focus": null,
    "paused_journeys": [],
    "expeditions": [],
    "incoming_invitations": []
  }

Active journey, below the first checkpoint, no expedition. The scene is on journey. focus only names that active journey. If the hero file is missing, image is null and fallback is none. The checkpoint object stays empty either way.

  {
    "you": {"id": 1, "name": "Sam"},
    "state": "active",
    "next_actions": [{
      "type": "continue_journey",
      "journey_id": 10,
      "route_slug": "everest"
    }],
    "journey": {
      "id": 10,
      "status": "active",
      "attempt_number": 1,
      "progress_version": 1,
      "total_ft": 400,
      "remaining_ft": 28632,
      "percent": 1.4,
      "summited": false,
      "route": {
        "slug": "everest",
        "name": "Everest",
        "region": "Khumbu, Nepal",
        "summit_ft": 29032
      },
      "checkpoint": {"current": null, "next": {
        "slug": "timms-hill",
        "name": "Timms Hill",
        "elevation_ft": 1951.5
      }},
      "scene": {
        "role": "route_hero",
        "checkpoint_slug": null,
        "image": {
          "path": "/static/photos/everest/hero.jpg",
          "alt": "The approach below the first checkpoint",
          "credit": null
        },
        "fallback": null
      }
    },
    "focus": {"journey_id": 10, "status": "active"},
    "paused_journeys": [],
    "expeditions": [],
    "incoming_invitations": []
  }

Active journey at a checkpoint that has a photograph. This fragment is Lukla, the first Everest stop with a credit entry. It lives on journey, not on focus.

  "journey.checkpoint.current": {
    "slug": "lukla",
    "name": "Lukla",
    "elevation_ft": 9383
  }
  "journey.scene": {
    "role": "checkpoint",
    "checkpoint_slug": "lukla",
    "image": {
      "path": "/static/photos/lukla.jpg",
      "alt": "The runway at Lukla, with the control tower and the hills beyond",
      "credit": {
        "author": "Vyacheslav Argenberg",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0",
        "source_url": "https://commons.wikimedia.org/wiki/File:Lukla_Airport,_Nepal.jpg"
      }
    },
    "fallback": null
  }

Reached checkpoint with no file, and no lower file. The journey is at Timms Hill. The scene does not advance to Lukla.

  "journey.checkpoint.current": {
    "slug": "timms-hill",
    "name": "Timms Hill",
    "elevation_ft": 1951.5
  }
  "journey.scene": {
    "role": "route_hero",
    "checkpoint_slug": "timms-hill",
    "image": {
      "path": "/static/photos/everest/hero.jpg",
      "alt": "The approach below the Khumbu",
      "credit": null
    },
    "fallback": "route_hero"
  }

Active journey and a private expedition on that route. Scene selection ignores the expedition. contributing is true because the participation points at the active journey.

  "expeditions": [{
    "slug": "sam-everest",
    "name": "Sam's climb",
    "kind": "private",
    "status": "active",
    "route_slug": "everest",
    "contributing": true,
    "displayed_total_ft": 9383,
    "accent_color": null,
    "welcome_override": null,
    "image_ref": null,
    "organization": null,
    "branding": {
      "primary_color": "#18354C",
      "accent_color": "#5095B6",
      "logo_ref": null,
      "image_ref": null,
      "welcome_message": null
    }
  }]

Active journey and an organization expedition. The participant need not be a member. Branding is the organization row. The route scene is unchanged. colors is the stored jsonb object, or null.

  "expeditions": [{
    "slug": "city-strides",
    "name": "City Strides",
    "kind": "organization",
    "status": "active",
    "route_slug": "everest",
    "contributing": true,
    "displayed_total_ft": 9383,
    "organization": {
      "slug": "runners",
      "name": "Runners",
      "logo_ref": null,
      "image_ref": null,
      "colors": null,
      "welcome_message": "A season on Everest."
    },
    "branding": {
      "primary_color": "#18354C",
      "accent_color": "#5095B6",
      "logo_ref": null,
      "image_ref": null,
      "welcome_message": "A season on Everest."
    }
  }]

Switched routes. Kilimanjaro is the active journey, so it owns the scene and focus points at it. Everest is paused. Both actions are present. The Everest expedition remains, and contributing is false because its journey is not active. Opening home does not resume Everest.

  "state": "active"
  "next_actions": [
    {"type": "continue_journey", "journey_id": 20, "route_slug": "kilimanjaro"},
    {"type": "resume_journey", "journey_id": 10, "route_slug": "everest"}
  ]
  "journey": {"id": 20, "status": "active", "route": {"slug": "kilimanjaro"}}
  "focus": {"journey_id": 20, "status": "active"}
  "paused_journeys": [{
    "id": 10,
    "status": "paused",
    "route": {"slug": "everest", "name": "Everest", "region": "Khumbu, Nepal", "summit_ft": 29032},
    "total_ft": 9383,
    "percent": 32.3,
    "scene": {
      "role": "checkpoint",
      "checkpoint_slug": "lukla",
      "image": {"path": "/static/photos/lukla.jpg", "alt": "...", "credit": {}}
    }
  }]
  "expeditions": [{
    "slug": "everest",
    "name": "Global Everest Challenge",
    "kind": "global",
    "status": "active",
    "route_slug": "everest",
    "contributing": false,
    "displayed_total_ft": 9383,
    "organization": null
  }]

Paused journey and a pending invitation, with no active journey. focus is null, so neither the paused route nor the invitation becomes the scene. The resume action names Everest. The invitation carries its own accept and decline actions and is not copied into next_actions. Home does not resume the journey and does not accept the invitation.

  {
    "you": {"id": 1, "name": "Sam"},
    "state": "resume",
    "next_actions": [{
      "type": "resume_journey",
      "journey_id": 10,
      "route_slug": "everest"
    }],
    "journey": null,
    "focus": null,
    "paused_journeys": [{
      "id": 10,
      "status": "paused",
      "route": {"slug": "everest", "name": "Everest", "region": "Khumbu, Nepal", "summit_ft": 29032},
      "total_ft": 9383,
      "percent": 32.3
    }],
    "incoming_invitations": [{
      "id": 4,
      "kind": "username",
      "expires_at": "2026-12-22T00:00:00",
      "expedition": {"slug": "kili-party", "name": "Kili party", "route_slug": "kilimanjaro"},
      "actions": [
        {"type": "accept_invitation", "invitation_id": 4},
        {"type": "decline_invitation", "invitation_id": 4}
      ]
    }]
  }

Two paused journeys and no active journey produce two resume actions, each with its own journey_id and route_slug. focus stays null.

Completed journey, nothing active or paused. journey is null. focus carries the finished attempt, and its status says credits are closed. The action starts a new attempt. It does not append credits to journey 10.

  {
    "state": "completed",
    "next_actions": [{
      "type": "start_new_attempt",
      "journey_id": 10,
      "route_slug": "everest",
      "status": "completed"
    }],
    "journey": null,
    "focus": {
      "journey_id": 10,
      "status": "completed",
      "attempt_number": 1,
      "progress_version": 4,
      "total_ft": 29032,
      "remaining_ft": 0,
      "percent": 100,
      "summited": true,
      "route": {"slug": "everest", "name": "Everest", "region": "Khumbu, Nepal", "summit_ft": 29032},
      "checkpoint": {
        "current": {"slug": "summit", "name": "Summit", "elevation_ft": 29032},
        "next": null
      },
      "scene": {
        "role": "checkpoint",
        "checkpoint_slug": "summit",
        "image": {"path": "/static/photos/summit.jpg", "alt": "Climbers on the Hillary Step below the summit", "credit": {}},
        "fallback": null
      }
    },
    "paused_journeys": [],
    "expeditions": []
  }

Abandoned is the same shape with state abandoned and focus.status abandoned. The start_new_attempt action reports status abandoned. The scene is the place that attempt had reached.

A paused journey on one route and a completed journey on another leaves focus null and state resume. next_actions contains resume_journey for the paused route and start_new_attempt for the completed route. The completed attempt is not treated as active.

No curated files at all. Position is still reported.

  "scene": {
    "role": "none",
    "checkpoint_slug": "timms-hill",
    "image": null,
    "fallback": "none"
  }


6. State handling and fallback behavior

Focus selection

  Reading home does not activate, resume, abandon, or restart a journey. The response describes actions. The matching POST performs them.

  1. An active journey exists, with or without paused journeys on other routes. state is active. focus is {journey_id, status: active} and points at journey. There is at most one active journey. Each paused journey remains in paused_journeys and keeps its own resume_journey action. A completed or abandoned current journey on another route keeps its start_new_attempt action. None of those actions is removed because the active journey is the center.
  2. No active journey, and one or more paused journeys. state is resume. focus is null, including when several routes are paused and including when another route has a completed or abandoned current journey. Every paused journey has its own resume_journey action, with that journey id and route slug. A finished attempt on another route still has start_new_attempt. Home does not pick one paused route and hide the others.
  3. No active journey and no paused journey, and at least one current journey is completed or abandoned. state is that selected attempt's status. journey is null. focus carries the selected attempt's presentation. Selection is the latest completed_at or abandoned_at, then the smallest route slug. Every finished current attempt, not only the selected one, has its own start_new_attempt action. Home does not restart the selected attempt.
  4. No current journey. state is ready. journey and focus are null. next_actions contains choose_route.

  A pending invitation does not change this selection. It adds an incoming_invitations row in every state above. Accept and decline sit on that row.

How journey and focus relate

  Active journey, nothing else. journey holds the route, progress, checkpoints, and scene. focus references journey.id.
  Active journey plus paused journeys. The same. The paused bodies are in paused_journeys, not in journey and not in focus.
  Paused journeys only, or paused plus a finished attempt elsewhere. journey is null. focus is null. Each paused entry carries that route's progress and scene, and none of them is the home center. The finished attempt is identified by its start_new_attempt action and is not given the center either.
  Finished attempts only. journey is null. focus holds the one selected attempt, with status completed or abandoned, and is the only place that attempt's scene appears. Other finished attempts appear as actions, not as a second focus.
  No journeys. Both are null.

A completed or abandoned focus cannot receive credits. continue_journey is absent in that state. resume_journey is absent for that route. start_new_attempt names the finished journey and, when used, creates a new attempt through the restart endpoint.

Expedition context does not change focus. A private, global, or organization expedition can point at the active journey, or at a paused or finished journey on its own route. Zero expeditions is normal, because registration creates neither a journey nor a participation, and starting a journey does not join one.

Image fallback never changes the reached checkpoint. current stays Timms Hill while the file falls back to the hero. next remains the following checkpoint and does not lend its picture. A route whose checkpoints have no files uses the hero for every reached position, with fallback route_hero, until the hero itself is missing.

Missing organization or expedition refs are nulls in the JSON. They do not remove the expedition row and do not change the scene.

Home of a user whose deletion_state is deleting returns 401, the same as the other authenticated reads, once the auth phase disables that account. Until that phase, the journey service already rejects a deleting user. The composer uses that result and does not build a scene for it.


7. Organization branding integration

The route scene and the organization brand answer different questions. The scene says where this person is on the shared route. The brand says which group they are with.

On an organization expedition card, home returns the organization's slug, name, logo_ref, image_ref, colors, and welcome_message. Those columns already belong to the organization. The composer reads them because the expedition has an organization_id. It does not check organization_memberships. An open-enrollment participant who never joined the organization still sees the public brand. A member who does not participate does not see that expedition on home, and does not gain the roster from this payload.

Private expeditions do not borrow an organization. Their optional accent_color, welcome_override, and image_ref stay on the expedition, as docs/architecture.md defined. Global Everest has neither.

Expedition image_ref and organization image_ref are card artwork. The scene function does not read them. A branded expedition on Everest still shows the Everest checkpoint the journey has reached. Route photographs remain platform content. An organization does not receive a private copy of the route, and it cannot override a checkpoint image.

The card above is the implemented payload, including `branding`. The server resolves product defaults, then the organization, then the expedition's accent, cover, and welcome. The client paints `branding` and does not merge those layers itself. The raw `organization` object and the raw private override fields stay in the payload so existing readers keep working. `branding.image_ref` is still card artwork. It does not enter `scene`. If a resolved logo or cover fails to load, the client uses a product placeholder or omits the image. It does not walk back through the inheritance chain, and it does not change the checkpoint photograph. Home loads the card columns in the same repeatable-read snapshot as the scene.


8. Performance and caching

The composer reads, in one request:

  the user
  that user's current journeys, which is a small set because only one journey per route is current and only one is active
  checkpoints for the active journey's route, for each paused route, and for the finished attempt carried on focus when journey is null
  active participations joined to expeditions and, for organization expeditions, the organization row
  pending invitations

Checkpoint images are not queried per stop. One select loads the published hero and checkpoint scenes for that route. The home document does not contain stop stories, the full checkpoint list, roster rows, or feed rows. Those stay on their own routes.

Image paths are stable. Progress changes the selected path only when the fallback result changes, usually when the total reaches a higher checkpoint that has a file. Clients cache by path. progress_version tells the client that the journey total may have changed. It is not appended to the image URL. Replacing a curated file is a new deployment of that static file, not a journey write.

A missing file is image null inside a 200 response. Home does not emit a path it could not resolve, so a client that trusts the payload does not request a file the server already knows is absent.

Scene selection stays out of the credit transaction. Credit commit updates the total and, when the activity flow says so, progress_version. The following GET /api/v1/home is the first moment an image is chosen. Two devices with the same journey therefore see the same scene without a stored choice.

The payload is one screen of identifiers, a handful of journey summaries, and image paths. It does not base64-encode photographs.


9. Dependencies and recommended implementation sequence

Already in place
  Route, checkpoint, journey, participation, expedition, and organization tables.
  Journey lifecycle service and its tests.
  view_model's current and next checkpoint rule.
  Curated photo files remain in adventurer/static/photos. photo_credits.json remains as archival editorial attribution. Scene selection reads published route_media.
  docs/architecture.md's decision that GET /api/v1/home is the aggregate, and that its photographs are the curated files.

Still required before the numbers and the scene are meaningful
  Journey credit reads. The total is zero until an activity service can write credits. Scene selection can be built and tested against a supplied total before that service exists. The endpoint should wait until a journey total is a real credit sum, so a home response of zero is a new journey and not an unfinished writer.

Not required first
  Organization administration. Branding fields can be null.
  Bearer auth. A cookie session can call the route. The mobile client sends a bearer the server does not accept yet. The client method belongs with docs/architecture.md's home phase, after bearer auth, and this document does not add a screen.
  Close, leave flags, and privacy removal. displayed_metrics uses the board rules once those writes exist. Until then the composer sums each required metric on the pointed journey and applies display_credits_after when the column is set.

Sequence
  1. The registry-backed scene function is implemented. Everest's hero is still unpublished. When launch content imports one, it is a broader Everest and Khumbu photograph, not a checkpoint. Image misses do not change progress, unlocks, or authorization.
  2. Home composer, after credit totals are readable. It calls the journey service, route reads, view_model, the scene function, participation reads, and invitations.
  3. GET /api/v1/home in the docs/architecture.md home phase, including the scene fields from the first version of the payload so a later visual pass does not change the contract.
  4. One method on the existing mobile API client. No screen in that phase.
  5. Nullable image_ref columns only if a real route cannot use the file convention.

Activity services should not import the scene function. Expedition services should not copy route photographs.


10. Settled decisions, open questions, and deferred concerns

Settled

  GET /api/v1/home is the only home route. /api/v1/me/home is not a second contract.
  journey is the active journey and carries its route, progress, checkpoints, and scene. focus references that id while a journey is active. focus carries presentation only for a completed or abandoned center, and that status cannot receive credits.
  An active journey is the center whenever one exists. Paused and finished journeys on other routes stay visible as their own actions.
  With no active journey, paused journeys leave focus null. Each paused journey has its own resume_journey target. With neither active nor paused journeys, the latest finished current attempt is focus, and every finished current attempt still has start_new_attempt.
  next_actions is an array of eligible journey actions. Invitation accept and decline live once, on the invitation row. Home returns every eligible action and does not pick a winner. The client chooses prominence. Reading home performs none of them.
  Scene selection is the highest reached checkpoint with a published scene, then the nearest lower reached checkpoint with a published scene, then the published route hero, then null. A checkpoint with several thresholds is reached only when all of them are met. The scene never uses a locked checkpoint's image, a draft or retired row, or a file chosen by its name. Everest's hero is a broader Everest and Khumbu photograph, not Lukla or another checkpoint. That row is part of the later launch-content import.
  The personal scene uses the full capped totals of the journey. Expedition cards use the board totals. Those numbers can differ after privacy rejoin, and the scene follows the personal totals.
  Home adds no media table of its own. The metric tables belong to docs/architecture.md section 16. The scene reads published route_media. A published scene row already has the rights in section 18.4. It does not put a second photograph on this scene, and it does not put journal bodies in this payload. The active journey's pending_arrivals list names unacknowledged reached checkpoints by slug and arrival title. It carries no arrival body, journal body, or discovery. Acknowledgment stays on the journey attempt in section 18 and does not change this scene. Restart leaves those acknowledgments on the old attempt. Paused, abandoned, and completed attempts do not put that list on the home scene.
  Organization branding and expedition image_ref are card fields. They do not own or override route imagery. Participation does not require organization membership. The implemented card returns the raw columns and the resolved branding object from docs/architecture.md section 19. Logo and cover upload is not implemented, and a JSON request cannot set those paths.
  The home module is a composer. Journey lifecycle, route content, and expedition rules stay in their own services. Activity writes do not select images.

Relationship to docs/architecture.md

  This document does not reopen journey lifecycle, credit ownership, restart, snapshots, or registration. journey remains the active journey or null, and now keeps the route, progress, and checkpoint fields docs/architecture.md put on that object. focus is the center of the home scene. When the center is the active journey, focus only repeats the id. When the center is a finished attempt, journey stays null and focus holds the presentation, with a status that cannot take credits.

  docs/architecture.md named next_actions as a plural list and also described a server-chosen next action. This document keeps the array. The server returns every eligible action in a stable order and does not discard one because another is a better center. docs/architecture.md's codes map onto continue_journey, resume_journey, start_new_attempt, and choose_route. review_invitation is not a competing singleton. Accept and decline are the existing invitation operations, carried on the invitation so the expedition is not described twice.

  docs/architecture.md says each of the current and next checkpoints includes a photo path when the stop is unlocked. The next stop is not unlocked, so it has no photo in the home payload. The current stop's photo is the scene, subject to the fallback above. That is a refinement of the same rule, not a second progress system. The photograph is the published route_media asset_path.

  Phase 10 in docs/architecture.md still owns implementation of the endpoint and the client method. This document fixes the scene contract those deliver. It does not pull that work forward into activity services. Section 16 of docs/architecture.md replaces total_ft, remaining_ft, percent, and summit_ft with the metrics array. This document uses that array and does not keep the old fields beside it.

Combined route

  A route that requires 500 ft and 2 miles, with 500 ft credited and 1 mile credited, is still active. Elevation is complete. Distance is not. The route is not complete, and status stays active. The gate checkpoint requires 200 ft and 1 mile, so it is reached. The finish checkpoint requires 500 ft and 2 miles, so it is next and locked. The scene stays on the gate. focus only names the active journey. Nothing on this payload adds the feet to the miles.

  {
    "journey": {
      "id": 10,
      "status": "active",
      "metrics": [
        {"metric": "distance", "unit": "mi", "target": "2", "credited": "1", "remaining": "1", "complete": false},
        {"metric": "elevation", "unit": "ft", "target": "500", "credited": "500", "remaining": "0", "complete": true}
      ],
      "checkpoint": {
        "current": {
          "slug": "gate",
          "name": "Gate",
          "position": 1,
          "requirements": [
            {"metric": "distance", "unit": "mi", "threshold": "1"},
            {"metric": "elevation", "unit": "ft", "threshold": "200"}
          ]
        },
        "next": {
          "slug": "finish",
          "name": "Finish",
          "position": 2,
          "requirements": [
            {"metric": "distance", "unit": "mi", "threshold": "2"},
            {"metric": "elevation", "unit": "ft", "threshold": "500"}
          ]
        }
      },
      "scene": {"role": "checkpoint", "checkpoint_slug": "gate"}
    },
    "focus": {"journey_id": 10, "status": "active"}
  }

Pace

  A later picture may draw a dot on the route. The dot is the highest checkpoint this payload already selected, or the approach when checkpoint.current is null. It does not move because a forecast says the person would be farther along.

  A ring or color around that dot may be on_track, behind, insufficient_data, or no_deadline. Those labels are not chosen yet. Home does not return them and does not store them on the journey. The live leaderboard's projected date is a separate field on that board, from the same read model, and it is not copied into this payload. If a response later adds a ring, it is a sibling of scene and safe to omit or recompute. It cannot move the dot or change the metrics.

Open questions

  None. The two product choices that were open are settled above.

Deferred

  More than one photograph in the home scene, user photographs, a CMS, and a general asset library. Extra journal and discovery images may exist under docs/architecture.md section 18 and stay off this payload. Collectible files, when they exist, use that later asset decision. docs/architecture.md section 17 defines them as achievement presentation. They are not home scene files.
  Where organization logo and expedition image bytes are stored. The nullable refs remain, and branding resolution does not wait on that host. Journey and activity services do not depend on that choice. Upload and mobile rendering of those files are not implemented.
  Everest launch photographs. The files and archival credits remain. A reviewed package still has to publish the route_media rows before those pictures appear in a scene.
  Screen layout, navigation, and interaction, except for the rule that home shows one place and one indicator per required metric. The payload names the place and the file. It does not describe how they are drawn.
  The pace calculation. The dot's place is the reached checkpoint. The forecast status is not part of this payload yet.
  Caching headers on the static files themselves. The path contract is enough for the client to cache.
