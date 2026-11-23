The Adventurer — final domain architecture

This file is the canonical domain architecture. It replaces the earlier Phase 4A draft. There is no live expedition or journey data to preserve, so this version has no historical backfill, no reconciliation report, no dual-read period, and no legacy snapshot.

Section 16 revises progress. A route requires one or more metrics, and a journey completes only when every required metric has reached its own target. That section supersedes every elevation-only progress, checkpoint, confirmation, feed, and leaderboard rule in this document. It does not change ownership, lock order, one-activity-one-journey, restart, leave flags, privacy removal, or authentication.

Section 17 adds achievements, collectibles, and a personal trophy case. It does not change journeys, activity credits, expedition participation, leaderboards, the privacy-removal deletes in section 5.9, lock order, or authentication.

Section 18 adds the curated narrative experience. Migration 0010, the chapter and journal API, activity arrival lists, and home pending arrivals are implemented. Content seeding and scene selection from route_media are not. The home presentation rules stay in docs/dynamic-home.md, and the HTTP contract stays in docs/api-v1.md. Section 18 does not change journeys, activity credits, expedition standings, lock order, privacy removal, or authentication.


1. Executive summary

A route is a curated adventure. An expedition is one group on exactly one route. A route journey is one person's attempt at one route, and it is the only live score. An activity credits at most one journey. That journey records each metric the route requires. Each metric is the sum of its own credits, capped at that metric's target. The activity row keeps the full value of every metric it recorded. The journey completes only when every required metric has reached its target.

A user has one active journey in the whole account. Switching routes pauses that journey and resumes, or starts, the other route. Returning to a completed or abandoned route does not start a new attempt. Restart is a separate action. It creates a zeroed journey and repoints active expeditions on that route. Closed expedition snapshots stay as they were.

While an expedition is active, its board reads each participant's current journey for that route. When an organization expedition completes, or a private expedition is archived, the board becomes a final snapshot. That snapshot includes people who were active at the close and people who had already finished the route and then left. It excludes people who left without finishing, and anyone who removed their expedition data. Read access after leaving is a separate flag from that standing.

Registration creates an account. It does not join Global Everest and it does not create a journey. The existing Expo app under apps/mobile stays; later work adds methods to the current API client and stores bearer tokens in the current SecureStore wrapper.


2. What exists today

Source of truth is the repository on branch the-adventurer-refactor, schema through migration 0006_invitation_expired. The notes below are current behavior, not the target.

Accounts. users and user_profiles. create_adventurer inserts those two rows and nothing else. Passwords are Werkzeug hashes. There is no email column. The HTML POST /join does call join_public for the Global Everest expedition unless an invite token is already in the session. That auto-join is a current behavior this plan changes.

Session. Cookie adventurer_id, thirty days, HttpOnly, SameSite=Lax, Secure when RENDER is set. guard_post in adventurer/__init__.py requires a CSRF token on every non-GET, from the form or X-CSRF-Token. GET /api/v1/session returns the CSRF token and the current user. Login, join, and logout are HTML forms. There is no bearer token table.

Routes. routes and route_checkpoints. Everest is a published route. Narrative text lives in adventurer/challenges.py. Stop photos live in adventurer/static/photos and are resolved by routes._photo. Users cannot create routes. Expedition pages load route structure from the database and story text from challenges.py.

Expeditions. One row per group, exactly one route_id, globally unique slug. Visibility is public or private. Status is active or archived. owner_user_id is nullable. starts_at and ends_at are optional, and the end must be after the start. The Global Everest row keeps its id and slug, name "Global Everest Challenge", public, active, owner null. User-created expeditions are private. The owner can edit, transfer ownership to an active member, or archive. Archive is a status change with no snapshot. App-managed expeditions have no owner and users cannot edit them. Date windows currently block new credits through can_add_credit. They do not block joins.

Participation. Unique (user_id, expedition_id), status active or withdrawn. Join and rejoin reuse the same row. The owner of a private expedition can rejoin without an invitation. A withdrawn non-owner loses private read access. Leaderboards in leaderboard_rows select active participations only, including zeros, ordered by elevation descending, last credit time ascending, then display name.

Credits. activity_credits copies the full activity elevation onto each chosen participation. One activity can be credited to several expeditions, including expeditions on different routes. update_activity rewrites every copy. This is the model being retired.

Invitations. expedition_invitations. Username invites and one pending share link per expedition. The raw share token is shown once. The database stores only its SHA-256. Links expire in thirty days, are revocable, and stay pending so they can be used more than once. Status values are pending, accepted, declined, revoked, and expired. Username accept activates the participation and marks the invite accepted in one transaction. Accept requires the expedition to be active and the route published. Incoming invitations are loaded for the HTML activities page only. There is no JSON list.

Activities. run, hike, walk, stair_climb, other, unspecified. Manual entries require verification_status not_required. activity_verification_events is append-only. competition_seasons exists and is unused. Leave it unused.

Authorization. Row level security is enabled and not forced. anon and authenticated are revoked. The application role bypasses row level security. Privacy is entirely in adventurer/access.py. A hidden private expedition raises KeyError and the route layer returns the same not-found body as a missing expedition.

Mobile. apps/mobile is an Expo SDK 58 scaffold with Expo Router, a typed createApiClient, and secureTokenStore under the key adventurer.access-token. The client already attaches Authorization: Bearer when a token exists, and X-CSRF-Token on non-GET when a CSRF getter returns one. Product screens are not built. apps/mobile/src/auth/native-auth.ts still says registration should enroll Global Everest. That comment is wrong for this plan and is corrected when bearer auth is added. The client is extended, not replaced.

Operations. Gunicorn on Render uses one worker and no scheduler. Elevation is numeric(12,5) meters. The interface shows international feet. New migrations are new files. Applied migrations 0001 through 0006 stay as they are. SQL sent through psycopg must not contain a literal percent sign. The test database is the isolated local URL in TEST_DATABASE_URL. Alembic tests must not read DATABASE_URL.


3. Entity relationship model

Users, profiles, routes, checkpoints, activities, and verification events stay. Their meanings stay. New and altered structures are below.

organizations
  id, slug unique, name, description, status active or suspended
  logo_ref, image_ref, colors jsonb, welcome_message
  created_at, updated_at
  Platform administrators create these. logo_ref and image_ref are nullable references. Where the bytes live is unresolved and does not block the journey work.

organization_memberships
  id
  organization_id, user_id, unique pair
  role: owner, admin, or member
  status: active or removed
  Partial unique index: one active owner per organization
  Membership does not create a participation, and a participation does not create a membership.

expeditions, altered
  Existing columns remain, including the Everest id and slug.
  kind: global, organization, or private
  organization_id, nullable, foreign key, restrict on delete
  enrollment: open or invitation, required for organization, null otherwise
  enrollment_opens_at, enrollment_closes_at
  Optional presentation overrides: accent_color, welcome_override, image_ref
  status gains completed. Private expeditions use archived. Organization expeditions use completed. Global Everest stays active.
  New unique (id, route_id), so a participation can reference the pair.
  Checks:
    global: organization_id null, owner_user_id null, visibility public
    organization: organization_id set, owner_user_id null, enrollment set, status active or completed
    private: organization_id null, visibility private, status active or archived
    an active private expedition has a non-null owner
    an archived private expedition may have a null owner after the owner-release path in section 5
    each date pair, when both ends are set, ends after it starts
    the two date pairs are independent

route_journeys
  id
  user_id, route_id
  status: active, paused, completed, or abandoned
  attempt_number, unique with user and route
  progress_version integer, starts at 1
  is_current boolean
  started_at, paused_at, completed_at, abandoned_at, created_at
  Unique (id, user_id, route_id)
  Also unique (id, user_id), for the credit foreign key
  Partial unique indexes:
    one row per user with status active
    one row per user and route with status active or paused
    one row per user and route with is_current true

activities, for progress measurements
  elevation_gain_m numeric(16, 6) null
  distance_m numeric(16, 6) null
  Null means that metric was not measured. Zero means it was measured and the value is zero. Those are different, and neither is a negative.
  A check rejects a negative. Zero is allowed. The current elevation_gain_m NOT NULL and the current checks that both amounts are greater than zero do not survive. distance_m is numeric(14, 3) today and elevation_gain_m is numeric(12, 5). Both widen to numeric(16, 6) so a foot and a mile convert without rounding away a place the client was allowed to send.
  Section 16 states which of those values can become a credit.

activity_journey_assignments
  id
  activity_id unique
  journey_id, user_id
  created_at
  Unique (id, journey_id)
  Foreign key (activity_id, user_id) to activities (id, user_id)
  Foreign key (journey_id, user_id) to route_journeys (id, user_id)
  One activity has at most one row, so it has at most one journey. The service is not the thing that enforces that.

journey_credits
  id
  assignment_id, journey_id
  metric_code
  credited_amount > 0, stored in meters at numeric(16, 6)
  created_at
  Unique (assignment_id, metric_code)
  Foreign key (assignment_id, journey_id) to activity_journey_assignments (id, journey_id)
  Foreign key metric_code to progress_metrics
  The journey on a credit row is the journey on its assignment. A credit cannot name a different journey, because the foreign key includes both columns.
  A trigger rejects a credit larger than the activity's stored value for that metric, and rejects a metric the activity left null.
  A trigger rejects a metric the journey's route does not require.
  A trigger rejects inserts, updates, and deletes of assignments and credits when the journey is completed.
  Delete is allowed only during account deletion, and only when users.deletion_state is deleting and the transaction has set adventurer.account_deletion to on. Privacy removal does not delete credits.
  The implemented journey_credits table still has a single credited_elevation_m column and a unique activity_id. That table is replaced by the assignment and these child rows. The old column is not kept beside them.

expedition_participations, altered
  Existing unique (user_id, expedition_id) and unique (id, user_id) stay.
  route_id not null
  journey_id not null
  retained_read_access boolean not null default false
  final_standings_eligible boolean not null default false
  display_credits_after timestamptz null
  Foreign key (expedition_id, route_id) to expeditions (id, route_id)
  Foreign key (journey_id, user_id, route_id) to route_journeys (id, user_id, route_id)
  Every participation, active or withdrawn, points at a journey for that same user and that expedition's route.
  display_credits_after is set only when the row is created by a rejoin after privacy removal.

The organization slot is a constraint trigger, not a partial index, because it joins expeditions. For one user, at most one row may be status active on an expedition whose kind is organization and whose status is active. Global and private rows do not count. Pausing a journey does not withdraw the participation, so the slot stays occupied.

expedition_result_entries
  id
  expedition_id, participation_id, unique pair
  user_id, journey_id
  display_name, final_rank, checkpoint_name, captured_at
  final_rank is the official place. It is null when section 16 gives that person no performance rank. It is not a live projected place, and the live board does not read it.
  Per-metric totals live on expedition_result_metrics, not in a single total_m.
  Written only by the close transaction.
  Ordinary progress cannot update or delete a row.
  Privacy removal and account deletion may delete the subject's rows. They set adventurer.privacy_removal or adventurer.account_deletion in that transaction so the immutability trigger allows the delete. Other rows are not rewritten. Ranks are not renumbered.
  There is no legacy snapshot type.

expedition_updates
  id, expedition_id, user_id, participation_id
  kind: activity_logged, checkpoint_reached, or summit_reached
  activity_id nullable, checkpoint_name nullable, created_at
  Credited amounts for an activity_logged row live on expedition_update_metrics.
  summit_reached means every required route metric is satisfied. It does not mean the last increment was elevation.
  Append-only for ordinary requests. Creating an activity may append activity_logged, one checkpoint_reached per newly reached checkpoint, and summit_reached when the journey completes. An edit or a delete does not append activity_logged or checkpoint_reached. An edit that completes the active journey appends summit_reached and nothing else. Feed rows are not rewritten. The live board re-sums credits. The same deletion settings that release result rows allow delete. New kinds can be added later as new allowed values. No comment, post, or announcement kind is added now.

expedition_privacy_removals
  expedition_id, user_id, primary key
  removed_at
  Foreign key expedition_id to expeditions, ON DELETE CASCADE
  Foreign key user_id to users, ON DELETE CASCADE
  One row per user and expedition. It records that this person removed their participation and has not rejoined since.
  It stores no token, display name, elevation, activity, journey, or result.
  Written in the privacy-removal transaction after that person's participation, feed rows, and result row are deleted, and only when the expedition remains rejoinable.
  The next new participation for that pair consumes the row and sets display_credits_after to the insert time.
  A repeated removal before rejoin updates removed_at on the same row.
  After rejoin the row is gone. An ordinary leave and rejoin does not create one and does not read one.
  Deleting the expedition or the user removes the row through the foreign keys. Owner release that deletes the expedition therefore leaves nothing behind. Owner release that archives the expedition does not insert a row, because that expedition cannot be rejoined. Account deletion does not need a separate copy of this table's contents.

api_tokens
  id, user_id
  token_hash unique, SHA-256 hex of a secrets.token_urlsafe(32) value
  expires_at, revoked_at, last_used_at, client_label nullable

users.deletion_state
  Null, or deleting. A finished deletion removes the user row, so there is no deleted state.

deletion_log
  id, completed_at
  No user id, name, token, or journey id.

expedition_invitations stays, including the expired status from migration 0006, the hashed share link, and the partial unique indexes for one pending username invite and one pending share link. Organization admins become allowed inviters in the service. The table does not need a second invitation system. A privacy removal is not an invitation and is not stored in this table.

activity_credits is dropped in the progress cutover. Nothing is copied out of it.

competition_seasons stays in place, empty, with no new features and no new foreign keys.

New tables enable row level security and revoke anon and authenticated, matching migration 0005. The application role still bypasses those policies. Service checks remain in adventurer/access.py and the modules beside it.

Ownership boundaries:
  Routes are platform content. Organizations do not own a private copy.
  An organization owns its memberships and its organization expeditions.
  A private expedition is owned by owner_user_id until the owner-release path clears it.
  A journey and an activity are owned by the user.
  An earned achievement is owned by the user. Section 17. The expedition does not own it. It does not require a participation row, or the continued existence of the expedition, route, activity, or organization it mentions, to remain.
  A final result row is owned by the expedition. The user may delete their own row through privacy removal or account deletion.
  A bearer token is owned by the user. Logout revokes one token. Revoke-all revokes every token for that user.


4. State machines

Journey
  none --start--> active, is_current, attempt 1
  active --switch away--> paused, is_current
  paused --resume--> active, is_current
  active or paused --abandon--> abandoned, is_current
  active --every required metric reaches its target--> completed, is_current
  any current row --explicit restart--> that row loses is_current
      if it was active or paused, it becomes abandoned
      if it was completed, it stays completed
      if it was abandoned, it stays abandoned
      a new row is inserted: active, is_current, next attempt number, no credits

is_current is the attempt expeditions display. It is not a synonym for active. After abandon or completion, that row stays current until an explicit restart. A completed journey cannot return to active, paused, or abandoned. One required metric reaching its target does not complete the journey.

Switch and resume never insert a journey. Start inserts the first journey on a route that has never had one. Restart is the only path onto a route whose current journey is completed or abandoned.

One active journey per user. Starting or resuming a route pauses the active journey on any other route in the same transaction. The paused row stays is_current for its own route.

Expedition
  organization: active --ends_at reached, or admin ends it--> completed
  organization: active --admin extends ends_at--> active, with a later ends_at
  private: active --owner archives--> archived
  global: remains active
  completed and archived are read-only. They do not reopen, accept joins, or accept feed rows.
  Completion and archival do not delete journeys or participations.

Participation
  absent --join or accept invite--> active, journey assigned by the activation rules
  active --leave--> withdrawn
  withdrawn --rejoin or accept invite--> active, same row, journey activated again
  active or withdrawn --privacy removal or account deletion--> row deleted

On an ordinary leave the service sets the two flags from the state at that moment. They are independent columns. Leave writes both. Nothing later derives one from the other.

  final_standings_eligible is true only when the journey currently pointed at by this participation has status completed.
  retained_read_access is true when this user has any completed journey on this route.

A completion after leave does not update either flag. A later restart does not clear either flag. Restart repoints only participations whose expedition is still active, so a withdrawn finisher keeps pointing at the completed journey.

A second leave recomputes both flags from the journey state at that later moment.

Invitation
  pending --accept--> accepted, for a username invite, in the same transaction as participation activation
  pending --decline--> declined
  pending --owner or admin revoke--> revoked
  pending --expires_at passed--> expired, marked before a replacement insert so the partial unique index can take a new pending row
  A share link stays pending across accepts until it expires or is revoked.
  An expired, revoked, declined, or accepted username invite cannot be accepted.
  Email delivery is not part of this machine.

Account
  normal --deletion request accepted--> deleting, tokens revoked, commit
  deleting --batches finish--> user row removed, deletion_log gains completed_at
  deleting --batch fails--> stays deleting, login stays disabled, retry continues the deletes
  A refusal because of ownership happens before the deleting commit. The account still works.


5. Core transaction flows

Lock order for every flow in this section:
  1. users, ascending id
  2. expeditions, ascending id
  3. route_journeys, ascending id
  4. expedition_participations, ascending id
  5. activities, ascending id
  Then write journey_credits, expedition_updates, and expedition_result_entries.

A transaction that needs several rows of one kind sorts them and locks in that order. It never locks a later kind and then an earlier kind. Two transactions therefore cannot deadlock against each other. A unique-index or trigger failure becomes a 409. A close that sees the participation set change rolls back and retries, up to three times, then returns 409.

An organization row is locked after the acting user and before any expedition that transaction inserts. Creating an organization expedition locks the acting user, locks the organization with FOR UPDATE, then reads the organization status and the actor's active membership again. It inserts only when that status is still active and the actor is still an owner or admin. Suspending an organization locks the platform administrator, then locks that same organization row. Creation and suspension serialize on it. If creation acquires the row first, suspension sees the new active expedition, closes it through the close flow, and only then marks the organization suspended. If suspension acquires the row first, creation sees the suspended status and inserts nothing.

Lazy completion runs as its own transaction before a slot check, a home read, or an organization-expedition read. It selects due organization expeditions, locks them in id order using the close flow, and commits. The caller's transaction starts after that.

5.1 Journey activation

Used by joining, rejoining, and accepting an invitation, for global, organization, and private expeditions alike.

Lock the user. Lock the target expedition. Lock the user's current journey on that route and the user's active journey, in journey id order. Lock the participations that will be updated, in id order.

Then:
  The user already has an active journey on this route. Point the participation at it.
  The user has a paused journey on this route. Pause the active journey on the other route, if there is one. Set this journey back to active. Point the participation at it. Progress is unchanged.
  The user has never had a journey on this route. Pause the other active journey, if there is one. Insert the first journey, active and current. Point the participation at it.
  The current journey on this route is completed or abandoned. Write nothing. Return 409 restart_required, with the route slug, the current status, and the slugs of active expeditions a restart would reset.

A follow-up with confirm_new_attempt true runs the restart flow and the participation insert in one transaction. The server does not start that restart from a plain join. A client may send the flag immediately; the effect is still a restart of that user's own route, under the restart rules, not a second silent path.

Creating a private expedition uses the same activation for its owner. If restart confirmation is required and absent, the expedition insert rolls back too.

5.2 Join

After activation:
  Global. The expedition is public, active, and on a published route. Insert or reactivate the participation. Global Everest does not use the organization slot.
  Organization, open enrollment. The caller does not need a membership, and the join does not create one. The enrollment window must be open. The slot trigger must pass. Lazy completion has already run, so an overdue expedition no longer holds the slot.
  Organization, invitation enrollment. Join is refused. The accept path is the only entry, and it uses this same activation.
  Private. An invitation accept, or the owner's direct rejoin, uses this same activation. Ordinary users do not join a private expedition because it is public.

The participation's route_id is the expedition's route. journey_id is the journey activation just chose. display_credits_after stays null on an ordinary join or on reactivation of an existing row. A brand-new row after privacy removal is specified in 5.9.

5.3 Switch, resume, abandon, restart

Switch to a route. If a paused journey exists there, resume it and pause the previous active journey. If no journey exists there, start the first one and pause the previous active journey. If the current journey there is completed or abandoned, return 409 restart_required and leave the active journey untouched.

Resume. Allowed only when that route has a paused journey. Same pause-the-other-route behavior. Pointers do not move, because the participation already points at this journey.

Abandon. The chosen in-progress journey becomes abandoned and stays current. Participations keep their pointers. Boards keep that total. The account has no active journey until a later start, resume, or restart.

Restart. Lock the user, then the active expeditions on the restarted route, then the active journey on any other route, then the old and new journeys, then the participations.
  Pause the active journey on the other route. It stays current for that route.
  Clear is_current on the old journey for this route. Abandon it if it was active or paused. Leave it completed or abandoned otherwise.
  Insert the new active current journey with no credits.
  Repoint participations whose expedition status is still active and whose route is this route.
  Do not update withdrawn participations. A person who finished, left, and later restarts their personal climb keeps the participation pointed at the completed journey.
  Do not update completed or archived expeditions, and do not update their result rows.

5.4 Activity credit and route confirmation

Preview, POST /api/v1/activities/preview, and the preview before an update, do not write. For each required metric the preview reports the current credited total, the remaining amount, the proposed credit from this save, and the resulting total. A metric the activity does not supply shows a proposed credit of zero and an unchanged result. will_complete is true only when those results would leave every required metric at its target. It is false when one metric would finish and another would not, and it is false when the activity supplies none of the remaining metrics. When will_complete is true the preview also returns an HMAC and a ten-minute expiry. Creating and editing use this preview, including a distance-only activity and an elevation-only activity. Deleting does not. A delete removes credits. It cannot complete a journey, so it never requires a confirmation token.

The signed payload is the string
  v2|{user_id}|{journey_id}|{progress_version}|{canonical_credits}|{exp_unix}
canonical_credits lists every metric that would be credited, sorted by metric code, as code=amount pairs separated by commas. The amount is the storage-unit numeric string, not the display unit. An elevation-only credit is elevation=152.40000. A combined credit is distance=3218.68800,elevation=152.40000. The key is the server SECRET_KEY. The token is not stored. The previous elevation-only v1 string is not accepted. There is no second token format to keep alive.

Save and update lock the user, the active expeditions on that route for which this user has an active participation, the journey being credited, those participations, and the activity when one already exists.

The journey row lock is the serialization point for remaining capacity. Lock every affected `route_journeys` row, in ascending id, and read the credited total in a following statement. An edit that changes two journeys locks both before it recalculates either total. Metric credits for one journey are written in metric code order. The credit trigger then updates `journey_metric_totals` and rejects a total above the route target. Callers do not lock that table, and the trigger does not lock `route_journeys`, so the global order stays intact. Two direct inserts that skip the journey lock still cannot commit a combined total above the target; the second fails. The journey lock is what lets the service see that committed total and credit only the remainder.

The trigger's advisory key is `pg_advisory_xact_lock(journey_id)`. The single 64-bit key is the journey id, so the full bigint range maps onto distinct locks and the id is not cast to a 32-bit integer. That single-bigint advisory space is reserved for journey credit locks. Other advisory locks use the two-integer form. A credit that changes journey locks the lower journey id first, then the higher one.

Under those locks the service recomputes each metric on its own.
  A null measurement is not credited. A stored zero is not credited. Otherwise the credited amount is the smaller of the activity's full value and the remaining target. A journey that needs 500 ft and 2 miles, given an activity of 900 ft and 3 miles, stores 900 ft and 3 miles on the activity and credits 500 ft and 2 miles. The same journey given 500 ft and no distance stores 500 ft, leaves distance null, credits 500 ft, and stays active. Neither leftover moves to another journey, another metric, or a later attempt.
  A metric that is already at its target gets no new credit row. The other required metrics still can.
  The journey stays active until every required metric is at its target. The credit that satisfies the last remaining metric completes it, whether that metric is elevation, distance, or both in one save.
  If a create or an edit would complete the journey, the token must match the locked user, journey, progress_version, and the full canonical credit string, and it must be unexpired. Otherwise the transaction rolls back and the response is 409 confirmation_required with a fresh preview. No activity insert or update is left behind. A delete is not this path.
  progress_version increments on every commit that changes any credited total: a new credit, an edit that changes a credited amount, a delete that removes one, or any other write that changes those totals. A metadata edit that changes no credit does not increment it. The first completing commit increments it as well. A replay then no longer matches, so it writes nothing.
  If there is no active journey, the activity is stored and no assignment is written. No token is required.
  An activity after the journey is already completed is stored with no credit and no token.
  An activity whose supplied amounts are all null or zero writes no assignment. Zero is a real measurement. It is not, by itself, a credit.

The credit transaction writes feed rows only for expeditions that are still active and whose active participation points at this journey, and only for a newly created activity, plus the summit case below. One activity_logged row carries the credited amounts, not the activity's full measurements. Each checkpoint that this create newly satisfies gets its own checkpoint_reached row. Completing the journey writes summit_reached. An edit does not write activity_logged or checkpoint_reached. An edit that completes the active journey writes summit_reached only. A delete writes no feed row. An expedition that closed before this transaction took its lock gets no feed row. The personal credit is still written when the journey itself is active.

Edits and deletes of credited amounts are allowed while the credited journey is active, paused, or abandoned. Changing elevation, distance, or both recalculates each metric's credit from the new full value and the other credits already on that journey. If an edit on the active journey would complete every required metric, it uses the same confirmation token. If an edit on a paused or abandoned journey would complete the route, the response is 409 resume_required and nothing is written. Completion happens on the active journey, through a create or an edit and the confirmation path. Deleting the activity deletes its assignment and every credit row in that transaction. That keeps the one-activity-one-journey rule, and progress_version increments when a credited total changes. The delete does not ask for a token and cannot be the write that completes the journey.

Once the journey is completed, no assignment and no credit may be inserted, updated, or deleted for it. The completed-journey activity trigger watches both elevation_gain_m and distance_m. It rejects an update of either column, and rejects delete of the activity, when that change would alter a credit on the completed journey. Note, date, activity type, and duration remain editable. A measurement the route did not credit remains editable. Account deletion is the only delete bypass, and it is the bypass in section 5.10.

5.5 Leave

Lock the user, the expedition, the pointed journey, and the participation. Set status withdrawn. Recompute the two flags as specified in section 4. Do not delete activities, credits, journeys, feed rows, or result rows.

The live board lists active participations only, so the person disappears from it immediately. Their journey total remains their personal progress. If final_standings_eligible is true, the later close includes them.

5.6 Close

Used when an organization expedition reaches ends_at, when an admin ends it early, and when a private owner archives it. Global Everest has no close path.

Lock the candidate users in id order. Candidates are participations that are active, plus participations that are withdrawn and final_standings_eligible. Lock the expedition. Read the candidate set again. If the set of participation ids changed, roll back and retry. Lock their journeys and participations in id order.

Set the expedition completed or archived. For each remaining candidate, insert one result row: display name, journey id, final_rank, checkpoint name, captured_at, plus one expedition_result_metrics row per required metric. Each metric total is the sum of that journey's credits for that metric, capped at the route target, and when display_credits_after is set, only credits created at or after that time. final_rank follows section 16 and never uses a projected date. Active participants with zero credit on every metric are included. Withdrawn people who did not finish are not candidates. People whose participation was deleted are not candidates. The checkpoint name is the highest checkpoint that journey had actually reached.

The slot releases because the expedition is no longer active. Journeys are not modified. A later restart does not repoint this expedition and does not update the result rows.

Concurrent activity. If the activity transaction locks the expedition first, its credit and feed commit, and the close sums that credit. If the close commits first, the activity may still credit a personal active journey and writes no feed row for this expedition. It does not update the result rows.

Concurrent leave. If leave commits first, the person is a candidate only when the new flag is set. If the close commits first, the person is inside the snapshot as an active participant. A later ordinary leave does not remove that row.

Concurrent restart. If restart commits first, the close snapshots the new journey for people who are still active. If the close commits first, restart's repoint predicate no longer matches this expedition.

Concurrent privacy removal. If it commits first, the person is gone from the candidate set. If the close commits first, privacy removal may delete that person's result row afterward. Other rows stay, ranks included.

5.7 Switch while an organization expedition is underway

The participation stays active. The slot stays occupied. The journey for the expedition's route is paused and remains the participation's journey, so the board still shows that progress. Credits on the new active route go to the other journey. Home marks the organization expedition as not contributing. Resuming the expedition route uses the resume flow. No new attempt is created unless the user explicitly restarts.

5.8 Read access after leave

Active participant: roster, live or final board, and feed.
Private owner: the same, including after a normal withdraw, which matches can_read today.
Withdrawn with retained_read_access: read-only roster, board, and feed.
Withdrawn without that flag, on a private expedition: the same not-found body as a stranger.
Public expeditions remain readable because they are public. The read flag still matters for organization expeditions, which are not world-readable.

Organization discovery is separate from the roster. An open organization expedition exposes its name, route, dates, branding, and enrollment to a signed-in user so they can join. Roster, board, and feed require participation, or an owner or admin role in that organization. An invitation-only organization expedition exposes that profile to invitees and to organization admins. A non-member participant does not gain the membership list.

5.9 Privacy removal

POST /api/v1/expeditions/<slug>/privacy-removal

The caller must already have a participation row, including a withdrawn one. Authorization does not use can_read, so a former member who lost access can still erase the association. No participation produces the same not-found body as a stranger.

If the caller is the owner of this private expedition and any other participation is active, return 409 ownership_transfer_required and write nothing. They transfer ownership with the existing owner update, then remove themselves as an ordinary participant. The expedition's owner_user_id then names the new owner.

If the caller is the owner and no other participation is active, the same transaction releases ownership:
  Set the privacy-removal setting.
  Build the close snapshot from eligible departed finishers, excluding the owner.
  Delete the owner's feed rows, result rows, and participation.
  If no participations and no result rows remain, delete the expedition's invitations and the expedition. The route remains. Earned achievements that named the expedition remain with their holders. Section 17. The expedition reference on those rows may clear. The historical award stays.
  If departed finishers' result rows remain, archive the expedition and set owner_user_id null. The archived-private check allows that. No owner column still names the removed person. Do not insert a privacy-removal row. The archived expedition cannot be rejoined.

For everyone else, one transaction:
  Set the privacy-removal setting.
  Delete their feed rows and result rows for this expedition.
  Delete the participation. An organization slot held by that row is free.
  Do not delete activities, journey credits, or journeys.
  Do not delete earned achievements. Section 17 keeps those with the person. They are not a substitute for the deleted feed or result, and they do not put the person back on the roster or the board.
  Do not insert a substitute anonymous total.

Rejoin inserts a new participation because the old row is gone. Journey activation runs. The same transaction finds this user and expedition in expedition_privacy_removals, deletes that row, and sets display_credits_after to the participation's insert time. The live board sums only credits created at or after that time, so the removed total does not reappear. Home and the personal journey still show the full journey. A later restart still resets active expeditions, including this new participation. retained_read_access and final_standings_eligible do not survive, because the row that held them was deleted. New credits after the rejoin count normally.

The privacy-removal row is kept only until that rejoin, or until the expedition or the user is deleted. It is not a log of past removals after the person has rejoined. An ordinary leave keeps the participation, so the later rejoin updates that same row, leaves display_credits_after unchanged, and does not consult expedition_privacy_removals. A second privacy removal writes the pair again. The following rejoin starts a new board clock and does not restore the previous one.

If the owner-release path deletes the expedition, it does not insert a privacy-removal row. The expedition is gone, and there is no rejoin. If that path archives the expedition, it does not insert one either. If the expedition stays active, the row is inserted after the caller's participation is deleted.

5.10 Account deletion

POST /api/v1/account/deletion with a confirmation phrase. Refusals happen before any destroying commit, and the account still works.

Refuse when the user is the active owner of an organization that is not suspended. They transfer ownership first. If they are the only member, a platform administrator assigns another owner or suspends the organization. Suspend completes any active organization expeditions through the close flow, then removes the active-owner requirement. The deletion endpoint does not dissolve an organization.

Refuse when the user owns a private expedition that still has another active participant. They transfer that owner first. Archive alone is not enough in this case, because the owner column would still name them.

When those refusals do not apply, a short transaction sets deletion_state to deleting, revokes every API token, and commits. Cookie and bearer authentication both fail from then on. That commit is the point of no return.

Later transactions are idempotent deletes by user id. They may be retried by the same request and by the lazy pass that also completes due expeditions. Each batch sets adventurer.account_deletion and locks in the global order, then deletes feed rows, result rows, participations, journey credits, verification events, activities, journeys, invitations where they are the inviter or the invitee, memberships, and tokens. When earned-achievement rows exist, that batch deletes them too. Their limited-edition sequence numbers are not reassigned. Section 17 states that rule. expedition_privacy_removals for that user disappear when the user row is deleted, because the foreign key cascades. Share links they created are deleted, so the hash stops matching. invited_by_user_id is ON DELETE RESTRICT, so those invitation rows are removed rather than left in place.

For a private expedition they still own, with no other active participant, the batch uses the owner-release path from 5.9 before the user row can go. The owner column is null, or the expedition is gone. It does not keep a foreign key to the deleted user.

The last transaction deletes the profile and the user, then inserts deletion_log with completed_at only. Completed-journey credits are deleted here. The immutability trigger allows that delete only behind the deletion state and the transaction setting. It does not allow a credited metric to be updated. Other people's activities, journeys, and remaining result totals stay. Routes, expeditions, and organizations stay.


6. Authorization matrix

The application role bypasses row level security. These checks are service-layer checks. A private miss and a missing expedition return the same not-found body, with no name in the error.

Anonymous
  Read published routes and public expedition profiles.
  No roster for a private or organization expedition, no join, no activity write.

Signed-in user
  Register and login.
  Create a private expedition.
  Join Global Everest and open organization expeditions, subject to journey activation and the slot.
  Log personal activities.
  Read and edit their own activities within the completed-journey rules.
  Privacy-remove their own participation.
  Delete their own account, subject to the ownership refusals.
  Read an organization profile when they are a member, and read an open organization expedition's public profile when they are not.

Active participant
  Read that expedition's roster, board, and feed.
  Leave.
  Their journey receives credits. The expedition board reflects the pointed journey while the expedition is active.

Withdrawn, retained_read_access
  Read-only roster, board, and feed.
  No invite, no archive, no credit into a closed expedition.

Withdrawn, flag false, private expedition
  No expedition read. Personal journey remains.

Private owner
  Edit name and description, invite, revoke, transfer to an active member, archive, and rejoin without an invitation.
  These management actions remain available after a normal withdraw.
  They do not remain after ownership release.

Organization member
  Read the branded organization home.
  No roster or board unless they also participate.
  No expedition create.

Organization admin or owner
  Create expeditions for that organization, choose open or invitation enrollment, invite non-members, end an expedition early, and extend ends_at while it is active.
  Read rosters and boards for that organization's expeditions.
  No access to another organization's membership list, and no access to a private expedition they do not belong to.

Platform administrator
  User ids listed in PLATFORM_ADMIN_IDS, read by Flask from the server environment.
  Create an organization, assign its owner, and suspend an organization.
  The value is never sent to the client and is never an EXPO_PUBLIC variable.
  No request can grant the role. Changing it is an environment change and a process restart.
  There is no database flag. The application role can write every table, so a column would not be a boundary.

Bearer requests with a valid token skip CSRF. Cookie requests do not. A present Authorization header that is missing, unknown, revoked, or expired returns 401 and does not fall through to the cookie. The token is never read from a query string or a cookie.


7. API contract

Cookie or bearer identifies the same user. Responses stay JSON. Errors keep a human error string and add a stable code. Existing HTML pages keep their flashes. Codes used below: confirmation_required, restart_required, resume_required, slot_occupied, ownership_transfer_required, forbidden, not_found.

Auth
  POST /api/v1/auth/register
    body: name, password
    calls create_adventurer, returns the user and a raw token once
    creates no participation and no journey
  POST /api/v1/auth/login
    body: name, password
    uses authenticate, returns the user and a raw token once
  POST /api/v1/auth/logout
    revokes the bearer token presented on this request
    a cookie logout still clears the browser session and does not require a bearer token
  POST /api/v1/auth/revoke
    revokes every token for the user and does not clear a browser cookie
  GET /api/v1/session
    unchanged for the browser: csrf and you
    a bearer call returns you and a null csrf

Home
  GET /api/v1/home
    you
    journey: null, or id, status, progress_version, one metrics entry per required metric,
      route slug, name, region, curated photo path,
      current checkpoint and next checkpoint, each with its requirement thresholds, and a photo path when the stop is unlocked
    The metrics entry is section 16. There is no second combined percent.
    paused_journeys: the same progress shape, without making them the home focus
    expeditions: slug, name, kind, route slug, status, contributing, displayed total
      contributing is false when the active journey is on another route
    incoming_invitations
    next_actions, chosen by the server:
      choose_route, when there is no current journey
      continue, for the active journey
      resume, for a paused journey the screen can offer
      start_new_attempt, when the user is looking at a completed or abandoned current journey
      review_invitation

The photo paths are the existing curated files under photos/. The API does not accept user photographs.

Journeys
  POST /api/v1/journeys/switch   body: route_slug
  POST /api/v1/journeys/resume   body: route_slug
  POST /api/v1/journeys/abandon  body: route_slug
  POST /api/v1/journeys/restart  body: route_slug
  Switch and resume return 409 restart_required when the current journey is completed or abandoned.
  Restart returns the expeditions that were reset and the journey that was paused, if one was.

Activities
  POST /api/v1/activities/preview
    body: the activity fields
    no write
  POST /api/v1/activities
    body: the activity fields, plus the summit token when the preview said will_complete
    409 confirmation_required, fresh preview, no row, when the token is missing, expired, or stale
  PATCH /api/v1/activities/<id>
    metadata, and any credited metric, under the rules in 5.4
  DELETE /api/v1/activities/<id>
    deletes the activity, its assignment, and its credits when the journey is not completed
    no confirmation token, because a delete cannot complete the journey
    increments progress_version when a credited total changes
    rejected when that activity credits a completed journey, except inside account deletion

Routes
  GET /api/v1/routes
  GET /api/v1/routes/<slug>
    current payload, plus this user's current journey and historical attempts on that route
    checkpoint content stays locked until every threshold on that checkpoint is met, using the section 16 comparison in storage units

Expeditions
  GET /api/v1/expeditions/<slug>
    says whether totals are live or final
    a final read returns result rows only
    a live read sums each required metric on the pointed journey, including credits from before starts_at, and applies display_credits_after to every metric
    zeros stay on the live board
  GET /api/v1/expeditions/<slug>/roster
  GET /api/v1/expeditions/<slug>/leaderboard
  GET /api/v1/expeditions/<slug>/updates
  POST /api/v1/expeditions/<slug>/join
    body may include confirm_new_attempt
    409 restart_required, or 409 slot_occupied
  POST /api/v1/expeditions/<slug>/leave
  POST /api/v1/expeditions
    private create: route_slug, name, description, optional confirm_new_attempt
  Existing update, archive, and invitation routes stay.
    Archive and the new organization complete action run the close flow.
    Organization create is a sibling that also sends organization slug and enrollment.
  POST /api/v1/expeditions/<slug>/privacy-removal
    body: confirm true
    409 ownership_transfer_required for a private owner who still has another active member

Invitations
  GET /api/v1/invitations
    the list already computed for the HTML activities page
  Existing create, revoke, accept, and decline paths stay.
    Accept runs journey activation in the invitation transaction.

Organizations
  GET /api/v1/organizations/<slug>
    branding and the caller's role
    membership list only for an owner or admin of that organization
  GET /api/v1/organizations/<slug>/expeditions
    public profile fields for a member or a participant
    management fields for an owner or admin
  POST /api/v1/organizations
    platform allowlist
    body: slug, name, initial owner username
  POST /api/v1/admin/organizations/<slug>/owner
    platform allowlist, body: username
  POST /api/v1/admin/organizations/<slug>/suspend
    platform allowlist
    completes active expeditions for that organization, then suspends it

Account
  POST /api/v1/account/deletion
    body: confirmation phrase
    409 ownership_transfer_required and a still-working account, when a refusal applies
    after the deleting commit, a repeat call continues the batches and does not log anyone in

Achievement and trophy-case routes are not in this contract. Section 17 is the domain. This section does not add them.

Browser /join stops calling join_public for Global Everest. Invitation signup already skips that enroll. Login and logout forms stay. Cookie CSRF stays.

CORS is added only if the Expo web build calls Flask from a browser. Native iOS and Android calls do not need it.

Login and register, and repeated bad bearer presentations, share a process-local failure limit. One Gunicorn worker makes a process-local limit real. The starting point is five failures in fifteen minutes for a client address and username. Those numbers are an implementation default.


8. Concurrency

The lock order in section 5 is the only order. Close, credit, join, leave, switch, restart, privacy removal, account deletion, organization-expedition creation, and organization suspension all use it. Limited-edition achievement numbers, when that issuance exists, lock their own edition only after every lock this order already requires. Section 17 states that. They do not move ahead of users, expeditions, journeys, participations, or activities, and they do not lock journey_metric_totals. Creation and suspension take the same organization-row lock after their acting user lock. The transaction that locks the organization first decides whether the new expedition is closed by suspension or never inserted.

Deadlock is avoided because a later resource is never locked before an earlier one, and ids within one resource are always sorted. PostgreSQL waits instead of cycling.

Retryable failures are limited and specific.
  Close retries when the candidate participation set changes between the user-lock step and the reread. Bound of three, then 409.
  A duplicate active journey, a duplicate in-progress journey, a duplicate current journey, a second assignment for one activity, a second credit for the same assignment and metric, or a second organization slot raises a constraint failure and returns 409. The application does not catch those and try a second winner.
  Route confirmation does not retry a stale token. The client asks for a new preview.

Final snapshot contents are the candidate set at the successful close commit: active participants, including zeros, and withdrawn participants whose final_standings_eligible flag is true. Departed finishers are in that set because leave set the flag while their participation still pointed at the completed journey. A restart after that leave does not move a withdrawn pointer, so the closed per-metric totals remain the totals from that completed attempt.


9. Clean-slate schema transition

Section 16 replaces elevation-only targets and credits. That later migration is also clean-slate for journey progress. It does not restore activity_credits, and it does not keep a dual read of the old elevation column.

Reuse without a domain change
  users and user_profiles, aside from deletion_state
  routes and route_checkpoints
  activities and activity_verification_events
  expedition_invitations
  competition_seasons, left unused
  the Everest route and the Global Everest expedition row, including its id and slug
  challenges.py narrative and the curated photo files
  cookie session, CSRF, access.py's not-found behavior, the Expo client, and SecureStore

Modify
  expeditions, with kind, organization, enrollment, presentation, and the completed status
  expedition_participations, with the journey pointer and the two independent flags
  users, with deletion_state
  the HTML /join handler, so registration no longer enrolls Global Everest
  activity create, update, and delete, so they talk to journey credits
  archive, so it writes a final snapshot
  native-auth.ts, so it no longer says registration enrolls Global Everest

Add
  organizations, organization_memberships
  route_journeys, journey_credits
  expedition_result_entries, expedition_updates
  api_tokens, deletion_log

Retire
  activity_credits
  the service paths that copy one activity onto many participations
  any client field that asks which expedition a new activity should credit
  An activity credits the active journey, or it credits nothing.

The cutover migration is destructive on purpose, and only because there is no meaningful expedition or journey data to keep. It deletes expedition_participations and activity_credits, drops activity_credits, and does not create journeys from the deleted rows. Existing activities remain personal history with no journey credit. Users, routes, the Everest expedition row, and invitations that are not dangling from a deleted participation remain. Take a database backup before applying it anyway. If that assumption about production data is later found to be false, stop before this migration. Do not invent a backfill at that point without a new product decision.

Tests that describe per-expedition credit copies are rewritten in the cutover phase. They are not preserved by a compatibility layer. The current suite is expected to fail against the new progress model until those tests are replaced. A green run of today's suite is evidence of the old model only.

Migrations 0001 through 0007 are not edited. Migration 0008 adds expedition_privacy_removals only. It does not copy invitations, participations, or progress into that table.


10. Implementation phases

Each phase is releasable only when its acceptance checks pass on the isolated database. Phases 1 through 4 can land as separate changes on the branch. Production does not run an intermediate release that reads both credit models. The first production deploy that serves journey totals is the deploy that drops activity_credits.

Phase 1. Schema
  New tables, altered expedition and participation columns, deletion_state, and the constraint triggers.
  No request behavior changes. activity_credits still serves the live app.
  Acceptance: migration upgrade and downgrade on the empty test database; the new uniqueness rules reject a second active journey, a second in-progress journey per route, a second current journey per route, a cross-user pointer, and a cross-route pointer; the existing suite still passes.

Phase 2. Journey and activity service
  Activation, switch, resume, abandon, restart, preview, and summit confirmation.
  Not yet wired to expedition boards.
  Acceptance: the service tests in section 11 for journeys and summit tokens, with no HTTP required.

Phase 3. Progress cutover
  Join, leave, accept, and private create call activation.
  Boards read the pointed journey.
  Activity writes stop inserting activity_credits.
  The migration deletes obsolete participation and credit rows and drops activity_credits.
  /join no longer enrolls Global Everest.
  Acceptance: a new account has no participation and no journey; one activity cannot credit two routes; restart resets active expeditions on that route and leaves a closed snapshot unchanged; the rewritten expedition tests pass.

Phase 4. Close
  Lazy completion, admin early end, private archive, and final snapshots that include departed finishers.
  Acceptance: the close, leave-before-close, and leave-after-close tests in section 11.

Phase 5. Organizations
  Memberships, branding fields, enrollment windows, the slot, admin create and invite.
  Acceptance: a second active organization expedition is rejected; a private expedition and Global Everest do not occupy the slot; a non-member can open-enroll; a member who does not participate cannot read the roster.

Phase 6. Leave flags and feed
  The two leave flags, feed writes, and read rules.
  Acceptance: a departed finisher can read and is named in the later snapshot; a departed non-finisher cannot read and is absent from the snapshot; the two flags can differ after a restart-then-leave.

Phase 7. Privacy removal
  Including the private-owner release path and display_credits_after on rejoin.
  Acceptance: the privacy tests in section 11.

Phase 8. Bearer auth
  api_tokens, the auth endpoints, the failure limit, and methods on the existing mobile client and SecureStore wrapper.
  Browser cookie login stays.
  Correct the Everest comment in native-auth.ts.
  Acceptance: cookie without CSRF fails; valid bearer without CSRF succeeds; a bad bearer returns 401 and does not use the cookie; register still creates no journey.

Phase 9. Account deletion
  The deleting commit, batched deletes, lazy retry, and the completed-credit bypass.
  Acceptance: the deletion tests in section 11.

Phase 10. Home
  GET /api/v1/home and the matching client method.
  No product screens in this phase.
  Acceptance: the payload contains the active route, checkpoint, paused journeys, expeditions, invitations, and a server-chosen next action, including start_new_attempt for a completed current journey.


11. Test strategy

Run on the isolated local database only. Do not point tests at hosted Supabase. Do not treat a run of the current suite as evidence that journeys exist.

Database constraints
  Second active journey, second in-progress journey on one route, second current journey on one route, second assignment for one activity, a credit whose journey differs from its assignment, credit whose user does not own the activity, participation aimed at another user's journey, participation aimed at another route, second active organization expedition.

Journey flows
  Resume does not move pointers and restores the same total.
  Abandon does not move pointers.
  Switch onto a completed or abandoned route writes nothing.
  Restart while another route is active pauses that other route, abandons the old in-progress row, repoints only active expeditions on the restarted route, and does not move a withdrawn finisher's pointer.
  Join on a brand-new route creates attempt 1 and pauses the previous active journey.
  Join on a paused route resumes it.
  Join on a completed route returns restart_required unless confirm_new_attempt is set, in which case restart and join commit together or not at all.

Route confirmation
  On an elevation-only route, 28,532 ft plus a 900 ft activity stores 900 on the activity and 500 on the elevation credit, and completes the journey only with a matching token.
  On a route that needs 500 ft and 2 miles, an activity of 900 ft and 3 miles stores both full values and credits 500 ft and 2 miles.
  An activity that finishes only one required metric leaves the journey active and does not require a token.
  A stale, expired, or missing token writes nothing.
  Two overlapping confirmations complete the journey once.
  An edit or a delete of any credited metric fails for every credit on that completed journey.
  A note edit succeeds. A distance edit succeeds when the route never credited distance.
  Account deletion, with the deletion setting, deletes those credits.

Close
  An activity that commits first is inside the snapshot. An activity that loses the race credits the person and adds no feed row and no snapshot change.
  Leave before close: a finisher is in the snapshot, a non-finisher is not.
  Leave after close does not remove the active row that was already snapshotted.
  Restart after a finisher has left does not change that later snapshot.
  Restart while the person is still active, followed by close, snapshots the new journey.
  Both a live read and a final read agree with the rules above. A final read does not sum live credits.

Authorization
  A non-participant member of the organization cannot read the roster.
  A non-member participant can read that expedition and cannot read the membership list.
  A withdrawn non-finisher receives not-found on a private expedition.
  A withdrawn finisher can read it.
  Platform routes reject a user who is only an organization owner.
  A private miss matches the missing-expedition response.

Privacy and deletion
  Privacy removal deletes the result row and the feed rows, keeps the journey, frees the slot, and a rejoin does not show the old total.
  The rejoin clock comes from expedition_privacy_removals. That row is consumed by the rejoin. A second removal starts a new clock. An ordinary leave and rejoin does not.
  Invitation lists and accepts are unchanged by a privacy removal.
  A privacy-removal transaction that rolls back leaves the participation and writes no privacy-removal row.
  A private owner with another active member is refused.
  A sole owner with a departed finisher leaves an archived expedition, a null owner, and the finisher's result row. No privacy-removal row remains.
  A sole owner with nobody else leaves no expedition.
  Account deletion refuses an active organization owner and a private owner who still has another active member.
  After the deleting commit, cookie and bearer both fail, including when a later batch fails and is retried.
  Another participant's result total and rank are unchanged.
  deletion_log has no user id.

Mobile client
  Extend client.test.ts for the new methods.
  A bearer header is attached from the existing token store.
  No screen tests in this plan, because no screens are built.

Concurrency
  Two joins to two organization expeditions for one user: one wins, the other gets slot_occupied.
  Close overlapping leave, restart, and privacy removal, as specified in 5.6.
  Organization expedition creation overlapping suspension. The organization row decides the order. The new expedition is closed and the organization suspended, or creation fails and inserts nothing.


12. Field Journal support

No screens are part of this plan. The home payload is shaped for the Field Journal direction.

The active journey is the focus: route name, curated route image, current checkpoint, one indicator per required metric, and a continue action. It is not a combined fitness score. Paused journeys and expeditions are present and secondary. Invitations are present so the next action can offer them. Activity history remains a dated personal list through the existing activity endpoints. Those rows are the person's own log. The API does not accept user photos, motivational copy, or a social feed.

Curated chapters, arrivals, journal entries, and discoveries are section 18. They are shared route content, not activity rows. The journal API, activity arrival lists, and home pending-arrival titles are implemented. Home still does not carry journal bodies, discovery bodies, or arrival bodies. docs/dynamic-home.md governs the scene.

Organization profiles supply the branded place a Community tab can open: name, colors, welcome, and that organization's expeditions. Roster and party details stay on the expedition endpoints, for participants and managers only.


13. Deferred

Competitive seasons, official rankings, provider imports, automated validation, appeals, moderation, comments, chat, direct messages, social posts, authored announcements, automated milestone announcements, and email invitations. competition_seasons and the verification-event table stay. No competitive feature is built on them. Manual activities continue to record a single not_required verification event. Personal achievements, collectibles, and trophy cases are section 17. They are not a competitive season, an official ranking, or an automated announcement.


14. Consistency audit

Every active participation has a non-null journey for the same user and the expedition's route. The foreign keys require it, and the cutover deletes old participation rows instead of leaving them journey-less.

Join, rejoin, and invite accept all use one activation function. A paused journey is resumed. A first journey is started. A completed or abandoned journey produces restart_required unless the client explicitly confirms a new attempt.

Several private expeditions can be active together. They share one journey when they share a route. They pause each other's routes when they do not. One organization expedition can be active in the whole account. Global Everest occupies neither restriction.

Restart repoints active expeditions on that route and pauses a different route's active journey. It does not update a completed or archived snapshot, and it does not move a withdrawn participation.

A person who finished and then left stays pointed at the completed journey, keeps final_standings_eligible, and is written into the close snapshot. A person who left first and finished later does not gain the flag. A person who restarted while still a participant, and then left, is judged on the new journey: they can keep read access because an older completed attempt exists, and they are absent from the final standings because the journey the expedition was showing was not completed. The two flags are written by the same leave function and are not derived from each other.

Privacy removal deletes a private owner's expedition, or archives it with a null owner, when nobody else is active. It refuses while another active member exists, until ownership is transferred. The owner column does not keep the removed person.

Account deletion uses that same owner release, refuses while the user is still an active organization owner or the owner of a private expedition with other active members, and deletes completed-journey credits only after login is already disabled.

Route confirmation is recomputed under the journey lock, bound to progress_version and the full canonical credit string, and unused tokens do not write. Completion freezes every credit on that journey without blocking account deletion.

One lock order covers every multi-row flow in section 5.


15. Open questions

1. Which user ids belong in PLATFORM_ADMIN_IDS. The allowlist mechanism is decided. The ids are operational.

2. Where organization logo and image bytes are stored. The columns may stay null until that choice is made. Journey work does not depend on it.

3. The failure-limit numbers. A limit is part of the auth phase. Five failures in fifteen minutes is the default written into that phase, and it can change without a domain change.

4. How the forecast read model turns credit history into a projected completion date. Section 16 decides what the live board does with that date, and it forbids inventing one. It does not decide the history window, the minimum history, inactivity and pauses, pre-expedition credits, deadline changes, confidence labels, or how to keep small new activities from shuffling the board. The pace dot's place is decided. Its on_track and behind labels are part of this same open calculation.


16. Multi-metric route progress

This section is the progress contract. Elevation-only wording earlier in this document remains only as the description of an elevation-only route, which is one configuration of these rules, not a separate code path.

16.1 Requirements

A route does not have a type such as climb, walk, or mixed. It has requirement rows. Initially the catalog contains two metrics.

  elevation. Storage is meters. Display is international feet. 1 ft = 0.3048 m exactly. The activity column is elevation_gain_m.
  distance. Storage is meters. Display is the statute mile. 1 mi = 1609.344 m exactly. The activity column is distance_m. The target API asks for miles. The implemented distance_km field is not a second stored distance and is removed rather than kept beside distance_mi.

Both measurements, route targets, checkpoint thresholds, and credits use numeric(16, 6) meters. One scale is enough. Elevation does not keep numeric(12, 5) while distance keeps numeric(14, 3), because those scales do not both hold the conversions below. Metric-specific scales would only be worth it if a later metric could not share six decimal places. These two can.

Accepted input is a decimal string, not a binary float. Elevation is accepted to 0.01 ft. Distance is accepted to 0.001 mi. Anything finer is rejected. Those steps convert exactly into numeric(16, 6): 0.01 ft is 0.003048 m, and 0.001 mi is 1.609344 m. The service does that conversion with Decimal. It does not use binary floating point, and it does not round away a place the client was allowed to send.

A null measurement is not supplied. Zero is supplied and is stored. A negative is rejected. Journey credit requires a supplied amount greater than zero for a metric the route requires. Zero alone, or a null, does not create an assignment. A distance-only activity leaves elevation null and can credit a distance-only route. An elevation-only activity leaves distance null and can credit an elevation-only route. A combined activity may supply both. A supplied metric the route does not require stays on the activity and is not credited.

Completion, caps, and checkpoint checks compare the stored meters to the stored target. The complete flag is that comparison. Display strings are Decimal conversions of those stored meters, quantized to the input step only for presentation. If that quantization would show a remaining amount of zero, or a credited amount equal to the target, while the stored target is not met, the response still says complete false and shows a remaining amount that is not zero. Display rounding never finishes a route, and it never hides a finish the stored values already reached.

progress_metrics
  code primary key
  storage_unit, display_unit
  The first rows are elevation and distance. A later metric is a new catalog row plus a place to store the activity's full value. It is not a new branch in the journey or expedition services. This revision does not add a third metric. It also does not replace elevation_gain_m and distance_m with a generic activity value table. Those two columns already hold the full activity values. A third metric should add activity_metrics and move these columns at that time, so the activity table does not grow a column per metric. That move is not part of this revision.

route_requirements
  route_id, metric_code, primary key
  target_amount numeric(16, 6), check greater than zero
  Foreign key metric_code
  A published route has at least one row.
  No row means the metric is absent. The route does not show it, credit it, or wait for it.
  A required target of zero is rejected. Absence and zero are not the same, and zero is not representable.
  routes.target_elevation_m stops being a progress input. Everest's published target becomes one elevation requirement of the same meter value. The column is dropped in the same migration. There is not a second target to consult.

checkpoint_requirements
  checkpoint_id, metric_code, primary key
  threshold_amount numeric(16, 6), check greater than zero
  The metric must be one the route requires.
  The threshold must be less than or equal to that route requirement's target.
  A checkpoint with no rows is invalid.
  route_checkpoints.elevation_m stops being the unlock input. Each former elevation becomes one elevation threshold of the same meter value, then the column is dropped. Slug, name, and position stay. Position is the route order. It is not a progress value.

Validation along position, so unlocks stay monotonic:
  A later checkpoint includes every metric required by an earlier checkpoint.
  For each metric, the later threshold is greater than or equal to the earlier threshold.
  The last checkpoint requires every route metric at that metric's target. Reaching it and completing the journey are the same condition.
  A checkpoint may require both metrics. It unlocks only when both thresholds are met. Meeting one leaves it locked.

Requirements and checkpoint thresholds are immutable once any route_journeys row exists for the route. A trigger rejects insert, update, and delete. A draft route with no journeys may still be edited by platform curation. Users never edit route configuration. Retiring a route does not change its requirements. Publishing does not by itself freeze them. The first journey does.

JSON on the route row was rejected. A document can hold the same numbers, but it cannot share a foreign key with credits, enforce one target per metric, or keep a checkpoint threshold inside the route target without a parser in a trigger. Queries for one metric become containment checks. Normalized rows let journey credits, checkpoint thresholds, feed amounts, and result totals all reference progress_metrics.code.

Completion compares storage amounts. Display rounding cannot finish a route or unlock a checkpoint. The API returns decimal strings in the display unit, with the unit beside them. It does not return a unitless percent of the whole route.

16.2 Credits

One journey tracks every metric its route requires. The total for a metric is the sum of journey_credits.credited_amount for that journey and metric, capped at the requirement target. Credits from before an expedition's starts_at still count. display_credits_after limits an expedition board and does not limit the personal journey.

An activity with an active journey and at least one positive supplied amount for a required metric gets one activity_journey_assignments row. Each such metric gets a child credit row on that assignment. A metric left null, a metric stored as zero, and a metric whose remaining target is already zero get no child row. The other metrics are still written. All of those rows share the assignment's journey. The unique activity_id and the child foreign key are what stop a second journey, not a check in the service. An activity with no active journey, or with nothing positive to credit, writes no assignment and stays in personal history. An activity never writes a credit for a route it is not on.

Paused and abandoned journeys keep the credits they already have. New activities do not credit them, because they are not the active journey. An edit of an activity that already credits a paused or abandoned journey recalculates those rows. If the recalculation would complete that journey, the response is 409 resume_required and the edit is not written.

Restart inserts a journey with no credit rows. Prior credits stay on the prior attempt. Active expedition participations on that route move to the new journey in the same transaction. Completed and archived expeditions, their result rows, and withdrawn participations do not move.

16.3 Checkpoints and scene position

A checkpoint is reached when the capped personal totals meet every one of its thresholds. The comparison is storage meters. The highest reached checkpoint is the reached checkpoint with the greatest position. The next checkpoint is the unmet checkpoint with the least position. Restart starts the new attempt with none reached. A closed expedition snapshot keeps the checkpoint name it wrote at close.

Missing photography does not change totals, unlocks, or authorization. Scene selection uses the highest reached checkpoint, then the nearest lower position that has a file, then the route hero, then no image. It never uses an unmet checkpoint's file. docs/dynamic-home.md states the fallback in full. The old rule that elevation wins over position is withdrawn, because a journey no longer has one elevation total that can order the stops.

16.4 Expeditions

An expedition still has one route. Each participant's live progress is that route's required metrics on the pointed journey. The board shows every metric. It does not collapse them.

A newly created activity appends one activity_logged row and one expedition_update_metrics row for each metric actually credited. It appends one checkpoint_reached row for each checkpoint that create newly satisfied, including when one activity crosses several. It appends summit_reached when that create completes the journey, whichever metric was last. An edit or a delete appends nothing, except that an edit which completes the active journey appends summit_reached and no checkpoint rows. Amounts on those rows are the credited amounts. Feed rows stay append-only.

Restart behavior is unchanged: active expeditions on that route follow the new zeroed attempt, and a final snapshot does not.

16.5 Leaderboards

Live standings and final standings are different results. A live read sums current credits under the board rules and may ask the forecast read model for a projected date. A final read returns expedition_result_metrics and final_rank. It does not sum the journey again and it does not store or consult a projected date. Eligibility is unchanged: active participants at close, including zeros; departed participants whose journey was completed when they left; not departed non-finishers; privacy removal deletes that person's result and leaves the other final ranks in place. Account deletion does the same. Snapshots are not otherwise rewritten.

A per-metric ratio of credited amount to target may be shown beside that metric. It is display only. It is not a score, and ratios are not averaged, weighted, or added. Feet and miles are never added.

Single-metric routes, including Everest, rank on actual credit, not on a forecast. Completed participants come first, ordered by completed_at, then display name. Unfinished participants follow, ordered by credited amount descending, then by the earlier time that amount was reached, then display name. Zero is a real total and stays on the board.

Multi-metric live order:
  Completed participants come first, ordered by completed_at, then display name. That place is an actual finish. The entry says so. It is not a projection.
  Unfinished participants who have a projected completion date come next, earliest date first, then display name. The date is the one from section 16.6: the latest estimate among required metrics that are not already satisfied. A satisfied metric is not estimated. The entry carries that date and says the place is projected.
  Unfinished participants with insufficient history stay on the board, after the projected rows, ordered by display name. They have no live rank and no projected date. The board says the forecast data is insufficient. It does not invent either value.

Multi-metric final order uses only actual results. Finishers are ordered by completed_at, then display name, and that place is final_rank. Participants who did not finish keep their per-metric totals and a null final_rank. They do not receive a performance rank. Single-metric final order is the same order as the single-metric live board, with the place stored as final_rank. No final row stores a projected date.

The live API uses live_rank, rank_basis, and projected_completion_at. rank_basis is actual_completion, projected_completion, or insufficient_data. live_rank is null for insufficient_data. The snapshot column is final_rank. The two are not one field reused for both meanings.

16.6 Forecast read model

The live board and, later, the pace dot call a read model. It is not a table, not a credit, and not a journey column. A required metric whose credited total already meets its target is satisfied. It needs no forecast. Its projected completion is the evaluation time, already passed, and its history is not used to move the route date. Only an incomplete required metric is estimated, from its own credit history and the amount still left. The route's projected completion date is the latest of those incomplete-metric estimates. If any incomplete required metric lacks sufficient data, the route result is insufficient data. If every required metric is already complete, the journey is an actual finisher. The read model does not return a projected finish for that person.

A route that needs 500 ft and 2 miles, with 500 ft credited and 1 mile credited, estimates only the remaining mile. An estimate of ten days makes the route projection ten days after the evaluation time. Elevation is not given another date.

That result must not change credited totals, checkpoint unlocks, the reached checkpoint, journey completion, progress_version, or final_rank. A cache, if a later decision adds one, is disposable and is recomputed from those inputs. This revision does not add the cache.

The pace dot stays the highest checkpoint actually reached, or the approach when none is reached. A ring around it may later say on_track, behind, insufficient_data, or no_deadline. It must not move the dot. Those labels are not chosen here. Open question 4 is the calculation, shared by the board's date and the dot's ring. Until that calculation exists, the read model returns insufficient data rather than a guessed date, and the live board still lists those people without a rank.

16.7 Implementation sequence

No migration in this revision. The sequence below is the order that avoids a second progress source. There is no dual read, no elevation-only runtime left beside the new rules, and no journey backfill. Accounts, the Everest route identity, the Global Everest expedition row, curated narrative, and photographs stay. Everest receives an elevation requirement equal to its current target. Existing activity rows keep their elevation_gain_m and distance_m values. Existing journey and expedition progress rows may be discarded with the progress cutover. There are no live users whose attempts must survive.

1. Schema. Add progress_metrics, route_requirements, checkpoint_requirements, activity_journey_assignments, expedition_result_metrics, and expedition_update_metrics. Replace journey_credits with the child rows in section 3. Drop the elevation NOT NULL and the greater-than-zero checks, allow null and zero on both measurement columns, and widen both to numeric(16, 6). Extend the completed-journey trigger to distance_m and to the assignment. Drop routes.target_elevation_m and route_checkpoints.elevation_m in that same migration after the requirement rows exist. Name the snapshot place final_rank and allow it to be null. Add the immutability trigger for requirements. Do not add a forecast table.

2. Services. adventurer/activities.py credits and confirms each metric, including distance edits that complete a route. adventurer/expeditions.py sums, ranks, snapshots, and writes feed amounts per metric. adventurer/scene.py and adventurer/home.py select the checkpoint from requirements and return the metrics array. adventurer/journeys.py keeps the lifecycle and still inserts an empty attempt on restart. Lock order and transaction boundaries stay as they are.

3. Tests. Replace assertions that a journey has one elevation total. Keep the elevation-only cases as one configuration. Add a distance-only route and a combined route, including one metric finishing first, the capped credit example, a stale multi-metric token, a distance edit that completes, and a completed journey that refuses both metrics.

4. API. The endpoints in section 7 and docs/api-v1.md gain the metrics arrays. Authentication, CSRF, and error codes stay. Mobile screens wait until those payloads exist. They do not build against total_ft, remaining_ft, percent, or summit_ft.


17. Achievements, Collectibles & Trophy Cases

This section is the award contract. It adds no tables, services, endpoints, or issuance. Journey progress, activity credits, expedition standings, and leaderboard rank stay what sections 3, 5, and 16 already define. An achievement is not a second progress total and not a standing. Current journey, activity, and expedition work continues without waiting on this domain.

17.1 Definitions, editions, and earned awards

Three records stay distinct.

  An achievement definition describes the accomplishment and the award.
  An achievement edition is one release of that definition. It has its own identity, its own edition size when the release is limited, and any availability or eligibility window that applies to that release. An unlimited achievement has no edition.
  An earned achievement is the durable award issued to one user. It refers to the definition and, when the award came from an edition, to that edition and its sequence number.

A later change to the definition leaves issued editions and earned awards as they were.

An earned achievement records the user, the definition, and the time it was issued. When it came from an edition, it also stores that edition's identity, the sequence number, and the edition size as part of the historical award. It records the route, expedition, activity, organization, or other context that applies, both as a snapshot and as an optional reference. It records the issuance source or the authorized grant. It keeps enough of the awarded name, description, and presentation to render the same award after the definition, the edition, or a source record changes or disappears.

A definition carries:

  A stable identifier.
  A name and a description.
  A scope, from 17.2.
  Eligibility criteria that a later implementation can evaluate one source at a time. This is not a general-purpose rules engine. The sources are the ones in 17.5. A definition does not embed a programmable rule language. A later source adds its own check.
  Availability: active, inactive, or retired. Active definitions may be issued. Inactive definitions are withheld. Retired definitions accept no further issuance, including no further editions. All three leave awards already issued in place.
  Optional collectible presentation, from 17.3.
  An optional default for future editions, from 17.4. That default is not a scope, and it is not the rules of an edition already issued.
  Creation and modification metadata for the definition itself: who changed it and when. That metadata is not the earned timestamp.

17.2 Scopes

Scope says what the accomplishment is about. The scopes named here are:

  Global. An accomplishment across the platform or across one person's own history. It does not require an expedition.
  Route. An accomplishment tied to one curated route.
  Expedition. An accomplishment tied to one expedition.
  Community. Participation or contribution tied to one community or organization.

A later scope is another named value on the same model. It is not a second award system. Limited-edition status is not a scope. An edition, from 17.4, is one issuance cohort, and a definition of any scope may have editions.

17.3 Collectibles

A collectible is how one achievement is shown or rewarded. One earned record may have a digital badge or medal in the trophy case, a shareable graphic, and an associated physical collectible. Those are presentations of one accomplishment. Issuance does not create a separate earned row for each format.

Rendering, file storage, and asset management are not part of this revision. Collectible bytes wait with the unresolved organization and expedition image storage in section 15. docs/dynamic-home.md already defers a general asset library. This section does not create one. Route photography references proposed in section 18 are editorial route content, not collectible files.

17.4 Editions

An unlimited achievement has no edition. A limited release is an achievement edition. The limit lives on that edition.

The definition may state a default for editions that have not yet been issued, including whether a future release is limited and the size it would start with. Once issuance of an edition has begun, that edition's size, numbering, and window stay fixed. An ordinary edit of the definition changes the default for later editions only.

The same definition can have more than one edition over time. Founding Finisher can have a 2026 edition of 25 and a separate 2027 edition of 50. Each edition has its own identity. Sequence numbers are unique inside that edition, so number 7 in the 2026 edition and number 7 in the 2027 edition are different awards. A trophy case may show 07 / 25 for the first and 07 / 50 for the second. More than one edition is part of the model. This revision does not build an edition-management product around it.

  The edition size stays fixed once issuance of that edition has begun.
  Sequence numbers are unique within the edition, not merely within the definition.
  The earned record stores that edition's identity, its sequence number, and its edition size.
  Allocation is concurrency-safe. Two qualifying completions at once receive different numbers, and the edition does not pass its size.
  The number and the earned record commit in one transaction. A failed transaction leaves no earned row without its number and consumes no edition slot.
  A number is taken only when the award is issued. Eligibility does not reserve a number.
  Issued numbers stay consumed. Privacy removal of a source record, account deletion, and an explicit audited revocation do not give the number to anyone else. A permanent gap is the rule. Revocation, if it is built, is an explicit audited action. It is not a silent delete, and this revision does not build it.
  A limited edition is not by itself a verified accomplishment. Verification, when a definition needs it, is a separate future policy. This section does not assume one.

There is no inventory, trade, resale, or marketplace.

17.5 Issuance

Five steps stay separate. None of them is inferred from the others, and they do not share one status.

  1. Eligibility. The person meets the definition. Nothing has been written.
  2. Issuance. An earned achievement row exists.
  3. Collectible presentation. How that row is drawn.
  4. Physical reward claim. The person asked for a physical item, when one is offered.
  5. Physical reward fulfillment. Shipment or delivery of that item.

Issuance is durable, idempotent, transactional, and traceable.

  The earned row remains after the expedition ends, after the person leaves, after they lose access to that expedition's active experience, after an organization membership changes, and after the expedition is archived. It does not depend on an active participation row.
  The same user, the same definition, the same edition when the award belongs to an edition, and the same qualifying event produce one award, unless that definition explicitly allows repeats. A repeatable definition says so. A later edition is a different cohort, so an award from an earlier edition does not fill it.
  The earned row, the historical context it keeps, and any edition identity, sequence number, and edition size commit together.
  The row records its source: the qualifying event, or an authorized grant.

Issuance sources this domain can accept later are route completion, expedition completion, a global personal milestone, a community award, and an authorized administrative grant. None of those issuers is implemented here.

Expedition completion may be the reason an award is issued. The earned row still belongs to the user. Issuance does not change journey progress, activity credits, participation, live standings, or final_rank. It does not write expedition_updates. It does not take the journey credit lock and it does not lock journey_metric_totals. When a later completion transaction also issues an award, it writes that award after the credit writes section 5 already requires. An edition allocation locks only its own edition, and only after any user, expedition, journey, participation, and activity locks that transaction already holds.

17.6 Physical rewards

A physical reward is optional. It is associated with an achievement or with that achievement's collectible. Earning the achievement does not claim the item and does not deliver it. Reward eligibility may be narrower than earning, so a person can hold the award and still be ineligible for the object. Claim and fulfillment are later records, including shipment or delivery status when that work exists.

Merchandise inventory, orders, payments, and shipping stay outside this domain. The earned achievement does not wait on them.

17.7 Trophy case

The trophy case is the user's collection of earned achievements. It presents those rows. It is not a store of route progress or expedition standings.

It holds awards of every scope and stays available when the person has no active participation. A later interface may order the case by the time each award was earned, or by another presentation order. That order is not a rank and it is not final_rank. Limited-edition numbers and collectible presentation come from the earned record.

Current journey progress, a live or final place, and an active participation are not earned achievements. Retained read access after leaving remains the participation flag in section 4. A departed finisher may keep that read access and may also hold an earned award. Losing read access leaves the award in place. Keeping read access does not create one.

17.8 History

Retiring a definition stops future issuance, including future editions, and leaves issued editions and earned rows in place.

An ordinary edit of a definition leaves issued editions unchanged, including their size and the sequence numbers already taken from them. The earned row keeps the historical context in 17.1. The schema phase chooses how that history is stored. This section requires the outcome.

An edition's availability or eligibility window can close without retiring the definition. Closing that window stops further issuance from that edition and does not renumber the awards already taken from it.

Administrative correction or revocation, if added later, is its own audited action. It is not an edit of the definition and it is not a silent delete.

17.9 Privacy and deletion

Ordinary leave leaves earned achievements in place. Expedition completion and archival leave them in place. An organization membership change leaves them in place.

Expedition privacy removal remains section 5.9. It still deletes that person's feed rows, result rows, and participation for that expedition. It still leaves activities, journey credits, and journeys in place, and it still leaves every other person's final_rank as it was. It also leaves earned achievements in place. The owner-release path may delete the private expedition itself. That delete does not remove earned achievements that named it. The trophy case is not a way to rebuild the removed board total, feed, or result, and it does not return the person to that expedition's roster or leaderboard. A shareable graphic of an expedition award is not a public stand-in for the deleted result. After the removal, the owner's trophy case may still show the award from the snapshot on the earned row, using only the context section 5.9 still permits. The removal does not restore display_credits_after, retained_read_access, or final_standings_eligible.

An earned achievement does not depend on the continued existence of the expedition, route, activity, organization, or other contextual record it mentions. Those mentions are optional historical references. They are not ownership, and deleting the source does not cascade to the earned row. The reference may become null. The snapshot on the earned row still renders the award. Ownership stays with the user.

Account deletion remains section 5.10. When earned achievements exist, the deleting batch removes them with the rest of that user's personal records. That deletion, and an explicit audited deletion of one earned record, are the exceptions that remove the award itself. Sequence numbers already issued stay consumed either way. Other people's awards, journeys, and result rows stay. Routes, expeditions, and organizations stay.

A later request to erase one earned record is its own audited delete. It does not recycle the sequence number. Leaving an expedition, or deleting a source record, does not perform that delete.

17.10 Boundaries

  An achievement is separate from a route journey.
  An achievement is separate from an activity and from a journey credit.
  An achievement is separate from a participation, a live standing, and a final snapshot.
  Expedition completion may be the occasion for issuance. The earned record belongs to the user.
  Retained read access stays the leave flag. The trophy case is the earned rows.
  Issuance leaves journey progress, activity credits, expedition standings, and leaderboard rank unchanged.
  A global or community award does not require an expedition.
  A personal activity stays an activity. An achievement is not an activity row. The curated Field Journal in section 18 is route prose, not an activity and not an award.
  competition_seasons, official rankings, and automated milestone announcements stay deferred in section 13.

17.11 Implementation sequence

This sequence does not reorder or block the journey, activity, and expedition work already underway.

  1. Architecture foundation. This section.
  2. Database schema and a transactional issuance foundation. Definitions are not seeded in that step unless a later product decision names them.
  3. A basic personal trophy case.
  4. Route-specific collectible presentation and shareable achievement cards.
  5. Limited-edition numbering, including more than one edition of the same achievement, then physical reward claim and fulfillment. The numbering step does not require an edition-management product.

No migration, service, API, or interface is part of the current change.


18. Narrative experience

This section is the curated storytelling contract. Migration 0010_narrative_content is applied. Published chapters, the field journal, arrival acknowledgments, activity arrival lists, and home pending arrivals are implemented. Content seeding and scene selection from route_media are not. Journey progress, activity credits, expedition standings, scene fallback, and the privacy-removal deletes in section 5.9 stay what sections 3, 5, 16, and docs/dynamic-home.md already define. Narrative content cannot award credits, complete a journey, change a participation, or write a leaderboard.

The implemented prose is still adventurer/challenges.py. The implemented photographs are still adventurer/static/photos and adventurer/photo_credits.json. Section 18 is the replacement to build later, not a second live source.

The story is linear. Chapters follow checkpoint position. Reaching a checkpoint reveals that checkpoint's arrival and opens its journal entry and discoveries. Nothing later on the route is shown early, and nothing branches on a choice. There is no AI-generated personal story and no general-purpose CMS. Platform curation writes the rows. Users do not.

The same published rows are the story for every personal journey and for every global, private, and organization expedition on that route. An expedition does not own a copy.

18.1 Six records

  A route chapter is an ordered act. It groups existing checkpoints. It does not have its own progress threshold.
  A checkpoint arrival is the short reveal shown when that checkpoint becomes reached.
  A Field Journal entry is the optional longer story for a reached checkpoint. It lives on the same checkpoint story row as the arrival. It is not an activity.
  A discovery is optional deeper content at a reached checkpoint: cultural, historical, geographic, culinary, or photographic. Discoveries do not gate later checkpoints.
  Curated media is a photograph reference and its license, attribution, and alt text. The bytes are not in PostgreSQL.
  A reveal acknowledgment is presentation state for one journey attempt. It records that this attempt has already been shown that checkpoint's arrival. It is not progress.

Unlocked, displayed, and acknowledged are different.

  Unlocked means the journey's capped personal totals meet every threshold of that checkpoint. Section 16.3 is the only authority. The narrative tables do not store a reached flag.
  Displayed means a response included the text or image. Home may name an unacknowledged arrival. It does not include a journal body or a discovery body. The journal endpoints return those bodies only when the checkpoint is unlocked and the row is published.
  Acknowledged means a journey_story_views row exists for that attempt and checkpoint. Acknowledgment does not change totals, progress_version, checkpoint reach, or expedition feed.

18.2 Tables

All of these tables are in schema adventurer. Status on editorial rows is draft, published, or retired. Draft and retired rows are not returned by the API. Users never insert, update, or delete editorial rows. The application role remains the writer, as in section 6. A later curation path is limited to platform administrators. Row level security is not the boundary.

route_chapters
  id bigint generated by default as identity primary key
  route_id bigint not null
  slug text not null
  title text not null
  summary text null
  position integer not null
  status text not null default 'draft'
  created_at timestamptz not null default now()
  updated_at timestamptz not null default now()
  Unique (id, route_id), so a checkpoint can reference the pair
  Unique (route_id, slug)
  Unique (route_id, position)
  Foreign key route_id to routes (id) on delete cascade
  Check position >= 0
  Check status in ('draft', 'published', 'retired')
  Check char_length(btrim(title)) > 0
  Check summary is null or char_length(btrim(summary)) > 0

route_checkpoints gains one nullable column, chapter_id bigint. This column is editorial. Changing it does not change a threshold, a position, or a credit.

  The foreign key is composite so a checkpoint cannot point at another route's chapter:

    FOREIGN KEY (chapter_id, route_id)
      REFERENCES route_chapters (id, route_id)
      ON UPDATE NO ACTION

  route_chapters unique (id, route_id) is required. The primary key on id is not a valid target for a two-column reference. route_checkpoints.route_id is already NOT NULL. Migration 0009 already has route_checkpoints_id_route_key on (id, route_id), which is the target for the story, discovery, and media foreign keys below. Migration 0007 already has route_journeys_id_user_route_key on (id, user_id, route_id), which is the target for journey_story_views.

  Unqualified ON DELETE SET NULL is rejected. PostgreSQL would null every column of the key, including route_id, and the NOT NULL constraint on route_id would then abort the chapter delete.

  The declaration that nulls only chapter_id is valid PostgreSQL 15:

    ON DELETE SET NULL (chapter_id)

  The isolated database is PostgreSQL 14.20. That column list was added in PostgreSQL 15, so migration 0010 cannot parse it there. 0010 uses the same foreign key with ON DELETE NO ACTION and a BEFORE DELETE trigger on route_chapters. The trigger sets route_checkpoints.chapter_id null where chapter_id equals the chapter being deleted. It does not assign route_id and it does not delete checkpoints. NO ACTION then finds no referencing row. A later upgrade to PostgreSQL 15 can replace that trigger with ON DELETE SET NULL (chapter_id). The effect is the same.

  MATCH SIMPLE applies. A null chapter_id does not consult the foreign key, so a checkpoint with no chapter is valid. ON UPDATE NO ACTION rejects moving a chapter onto another route while a checkpoint still names it. It does not rewrite route_checkpoints.route_id.

  route_checkpoints_position_immutable is BEFORE UPDATE OF position only. Assigning chapter_id does not fire it. route_progress_shape_checkpoints does fire on any update of route_checkpoints, including chapter_id. That function rechecks requirement shape and does not read chapter_id, so the assignment stays allowed after a journey exists. Requirement immutability is unchanged.

  A checkpoint belongs to at most one chapter because the column holds one id. Deleting a chapter leaves checkpoint_stories, story_discoveries, and route_media in place. Those rows follow the checkpoint.

  Checkpoints that share a chapter occupy one contiguous span of position. A checkpoint lying strictly between two members of a chapter belongs to that same chapter. A chapter with a lower position has only checkpoints whose positions are lower than every checkpoint of a later chapter. An unassigned checkpoint may sit before, between, or after those spans, and not inside one. The constraint trigger is route_chapter_spans. It is DEFERRABLE INITIALLY DEFERRED, and it is not route_progress_shape. SET CONSTRAINTS ALL IMMEDIATE stays forbidden. A curation transaction sets route_chapter_spans immediate by name when it needs the check before commit.

A route may have zero chapters. A checkpoint may have a null chapter_id. Progress and the scene do not wait for a chapter. A published route is not required to narrate every checkpoint.

checkpoint_stories
  id bigint generated by default as identity primary key
  checkpoint_id bigint not null
  route_id bigint not null
  arrival_title text not null
  arrival_body text not null
  journal_title text null
  journal_body text null
  status text not null default 'draft'
  created_at timestamptz not null default now()
  updated_at timestamptz not null default now()
  Unique (checkpoint_id), so a checkpoint has one story
  Unique (id, checkpoint_id)
  Foreign key (checkpoint_id, route_id) to route_checkpoints (id, route_id) on delete cascade
  Check status in ('draft', 'published', 'retired')
  Check char_length(btrim(arrival_title)) > 0
  Check char_length(btrim(arrival_body)) > 0
  Check journal_title is null or char_length(btrim(journal_title)) > 0
  Check journal_body is null or char_length(btrim(journal_body)) > 0
  Check ((journal_title IS NULL) = (journal_body IS NULL))
  Published arrival text is the short reveal. The journal pair is the optional deeper entry. There is no second story row and no user-authored body.

story_discoveries
  id bigint generated by default as identity primary key
  checkpoint_id bigint not null
  route_id bigint not null
  slug text not null
  kind text not null
  title text not null
  body text not null
  position integer not null
  status text not null default 'draft'
  created_at timestamptz not null default now()
  updated_at timestamptz not null default now()
  Unique (id, checkpoint_id)
  Unique (checkpoint_id, slug)
  Unique (checkpoint_id, position)
  Foreign key (checkpoint_id, route_id) to route_checkpoints (id, route_id) on delete cascade
  Check position >= 0
  Check kind in ('cultural', 'historical', 'geographic', 'culinary', 'photographic')
  Check status in ('draft', 'published', 'retired')
  Check char_length(btrim(title)) > 0
  Check char_length(btrim(body)) > 0
  Order is position within the checkpoint. Kind is a label, not a branch. A culinary row is the structured place for what challenges.py calls a recipe. The body is prose. This table does not add an ingredients schema. A later edition can add columns if a recipe layout needs them. It does not add them now.

route_media
  id bigint generated by default as identity primary key
  route_id bigint not null
  checkpoint_id bigint null
  discovery_id bigint null
  role text not null
  asset_path text not null
  alt_text text not null
  rights_basis text not null default 'undocumented'
  author text null
  license text null
  license_url text null
  source_url text null
  permission_note text null
  position integer not null default 0
  status text not null default 'draft'
  created_at timestamptz not null default now()
  updated_at timestamptz not null default now()
  Foreign key route_id to routes (id) on delete cascade
  Foreign key (checkpoint_id, route_id) to route_checkpoints (id, route_id) on delete cascade
  Foreign key (discovery_id, checkpoint_id) to story_discoveries (id, checkpoint_id) on delete cascade
  Check position >= 0
  Check status in ('draft', 'published', 'retired')
  Check role in ('route_hero', 'checkpoint_scene', 'journal', 'discovery')
  Check rights_basis in ('public_domain', 'cc0', 'creative_commons', 'commissioned', 'undocumented')
  Check char_length(btrim(asset_path)) > 0
  Check char_length(btrim(alt_text)) > 0
  Check author is null or char_length(btrim(author)) > 0
  Check license is null or char_length(btrim(license)) > 0
  Check license_url is null or char_length(btrim(license_url)) > 0
  Check source_url is null or char_length(btrim(source_url)) > 0
  Check permission_note is null or char_length(btrim(permission_note)) > 0
  A published row must satisfy route_media_publish_rights, stated in section 18.4. Draft and retired rows may be undocumented. The check uses equality and position(), not a pattern that needs a percent sign, so the later migration can copy it.
  Check (role <> 'route_hero' OR (checkpoint_id IS NULL AND discovery_id IS NULL))
  Check (role <> 'checkpoint_scene' OR (checkpoint_id IS NOT NULL AND discovery_id IS NULL))
  Check (role <> 'journal' OR (checkpoint_id IS NOT NULL AND discovery_id IS NULL))
  Check (role <> 'discovery' OR (checkpoint_id IS NOT NULL AND discovery_id IS NOT NULL))
  Partial unique index on (route_id) WHERE role = 'route_hero' AND status = 'published'
  Partial unique index on (checkpoint_id) WHERE role = 'checkpoint_scene' AND status = 'published'
  Partial unique index on (checkpoint_id, position) WHERE role = 'journal' AND status <> 'retired'
  Partial unique index on (discovery_id, position) WHERE discovery_id IS NOT NULL AND status <> 'retired'
  Index (route_id, role, position)
  Index (checkpoint_id, role, position)

asset_path is a reference, such as /static/photos/lukla.jpg. The table does not store bytes, width, or a binary thumbnail. Extra journal and discovery images use position. They are not the home scene. The home scene remains the one published checkpoint_scene, or the route_hero, under the fallback in docs/dynamic-home.md.

journey_story_views
  id bigint generated by default as identity primary key
  journey_id bigint not null
  user_id bigint not null
  route_id bigint not null
  checkpoint_id bigint not null
  acknowledged_at timestamptz not null default now()
  Unique (journey_id, checkpoint_id)
  Foreign key (journey_id, user_id, route_id) to route_journeys (id, user_id, route_id) on delete cascade, on update no action
  Foreign key (checkpoint_id, route_id) to route_checkpoints (id, route_id) on delete cascade, on update no action
  Index (user_id, journey_id)
  The row has no metric amount, no progress_version, and no expedition id.
  Both foreign keys carry route_id, so the checkpoint and the journey are on the same route. user_id is the journey owner because it is part of route_journeys_id_user_route_key. A separate foreign key to users is not added. Account deletion already cascades from the user to the journey and then to these rows. There is no second cascade path.

Editorial tables reuse adventurer.set_updated_at. They do not add another timestamp function. Story, discovery, and media foreign keys to a checkpoint are ON DELETE CASCADE and ON UPDATE NO ACTION. A route delete is already rejected while a journey or an expedition still points at it, so those cascades are the cleanup for a route that has no attempts. They do not run during privacy removal.

Deleting a route cascades its chapters, and cascades its checkpoints, which cascade stories, discoveries, media, and views. Deleting a chapter sets checkpoint.chapter_id null and does not delete checkpoints. Deleting a journey deletes its views and does not delete stories. Account deletion already deletes the user's journeys, so the views go with them. Privacy removal does not delete journeys, activities, or credits, and it does not delete views. Expedition close, leave, and restart do not delete views on the attempt they leave behind.

18.3 Unlock and reveal

Reach is the capped personal totals compared with checkpoint_requirements, in storage meters. A checkpoint with several thresholds unlocks only when every threshold is met. Display rounding does not unlock it. display_credits_after does not apply. Restart begins an attempt with none reached.

One activity can cross several checkpoints. Reach does not depend on which metrics the route uses. An elevation-only route, a distance-only route, and a combined route all unlock a checkpoint only when every one of its thresholds is met. The create response lists every checkpoint that write newly satisfied, in position order. An update lists the checkpoints newly satisfied on the journey that activity already credits, including a paused or abandoned journey whose totals moved without completing. An edit that would complete a paused or abandoned journey is still 409 resume_required and still writes nothing, so it returns no arrivals. That list is for the client. It does not add expedition_updates. Section 16.4 still appends checkpoint_reached only on create, and a completing edit still appends summit_reached and no checkpoint rows. A delete lists nothing. The following journal read shows whatever the totals still reach.

Acknowledgment is one insert for the caller's journey and one checkpoint. The service locks the user, then that journey, in the order section 5 already uses for a journey write. It does not lock expeditions, participations, activities, or journey_metric_totals. It reads the existing progress totals. If the checkpoint is not reached, or the story is not published, the insert is rejected and no view row is written. A second acknowledgment of the same pair returns the existing acknowledged_at and writes nothing. The insert does not change route_journeys.

Previously unlocked journal content stays readable after the journey is completed, because the completed totals stay. It also stays readable while the journey is paused or abandoned. New activities still do not credit a paused or abandoned journey, so they do not unlock further checkpoints on it. An edit that would complete a paused or abandoned journey is still 409 resume_required and still writes nothing. An edit or delete that lowers a paused or abandoned journey's totals below a threshold locks that checkpoint again, which is the existing credit rule. The view row is kept. While the checkpoint is locked, journal endpoints omit it. If the same attempt reaches it again, the content returns and the arrival is already acknowledged, so the client does not present the automatic reveal a second time.

Restart creates a new journey. Views belong to the old journey id and are not copied. The new attempt has no views, so its arrivals can be shown again. The abandoned attempt keeps its views and its credits. Its journal remains readable on that journey id.

Home, for the active journey only, may list unacknowledged reached checkpoints as identifiers and arrival titles. It does not include journal_body, discovery bodies, or extra journal images. A paused or abandoned journey's unacknowledged arrivals are not placed on the home scene. They are available from that journey's journal endpoint. docs/dynamic-home.md remains the scene contract. Narrative reads do not choose the photograph.

18.4 Media

The immersive scene never uses an unreached checkpoint's file. That rule is docs/dynamic-home.md. Section 18 does not add a second selector.

Roles:

  route_hero. One published row per route. The approach picture, before the first checkpoint. Everest's hero remains a broader Everest and Khumbu photograph, not Lukla or another checkpoint.
  checkpoint_scene. One published row per checkpoint. This is the file the scene selector may use once that checkpoint is reached. Fallback to a lower reached checkpoint, then the hero, then null, is unchanged.
  journal. Additional images for a reached checkpoint's journal entry. Ordered by position. Absent from home.
  discovery. Images for one photographic or other discovery. Ordered by position. Absent from home and absent until that checkpoint is reached.

A missing or unpublished asset is omitted. The selector does not borrow a later checkpoint, and it does not invent a path. A published row whose file is absent is treated as missing. Scene selection order is unchanged. docs/dynamic-home.md still allows the current resolver to return a file whose photo_credits.json entry is missing, with credit null. That is the implemented lookup. It is not permission to publish a route_media row.

Publication is a commercial-use gate. A file being on the public web is not a right to use it. rights_basis records which of these cases the row actually is.

  public_domain. The work is public domain where the service operates. license is the text Public domain. source_url is required. author is optional when the author is unknown.
  cc0. The rights holder dedicated the work under CC0. license is the text CC0. license_url and source_url are required. author is optional.
  creative_commons. The license permits commercial use. The accepted license texts are CC BY 2.0, CC BY-SA 2.0, CC BY 2.5, CC BY-SA 2.5, CC BY 3.0, CC BY-SA 3.0, CC BY 4.0, and CC BY-SA 4.0. license_url, source_url, and author are required. A license that contains NC or ND is not accepted. Those conditions do not permit this commercial use, or they prohibit the way a scene image is displayed. The current Everest credits are CC BY or CC BY-SA at 2.5, 3.0, or 4.0, which this list accepts. A missing credit entry still cannot be published.
  commissioned. Someone granted this service the right to use the photograph, and that grant includes commercial use. license, author, and permission_note are required. permission_note names the grant. It is curation data and is not returned by the API. source_url is optional.
  undocumented. The rights are unknown or the record is incomplete. This is the default. A row with this basis cannot be published.

route_media_publish_rights is this check. status = 'published' is allowed only for the four granted bases, and only when the fields that basis requires are non-empty. The creative-commons branch also requires position('NC' in license) = 0 and position('ND' in license) = 0. Draft and retired rows may stay undocumented. Alt text is required on every row, including drafts.

  CHECK (
    status <> 'published'
    OR (rights_basis = 'public_domain' AND license = 'Public domain' AND source_url IS NOT NULL)
    OR (rights_basis = 'cc0' AND license = 'CC0' AND license_url IS NOT NULL AND source_url IS NOT NULL)
    OR (
      rights_basis = 'creative_commons'
      AND license IN (
        'CC BY 2.0', 'CC BY-SA 2.0',
        'CC BY 2.5', 'CC BY-SA 2.5',
        'CC BY 3.0', 'CC BY-SA 3.0',
        'CC BY 4.0', 'CC BY-SA 4.0'
      )
      AND position('NC' in license) = 0
      AND position('ND' in license) = 0
      AND author IS NOT NULL
      AND license_url IS NOT NULL
      AND source_url IS NOT NULL
    )
    OR (
      rights_basis = 'commissioned'
      AND license IS NOT NULL
      AND author IS NOT NULL
      AND permission_note IS NOT NULL
    )
  )

Until 0010 is applied and a later cutover reads route_media, adventurer/scene.py continues to resolve adventurer/static/photos and adventurer/photo_credits.json. This design does not move, download, or replace those files. asset_path can name those static paths. Seeding a published row from an existing file copies a complete rights record from photo_credits.json. A file with no credit entry, or with a license this section does not accept, stays unpublished. A later move to object storage or a CDN replaces the path and the file host. It does not put bytes in PostgreSQL and it does not change unlock rules. Organization logo bytes remain the separate open question in section 15.

18.5 Authorization

Anonymous route catalog may list published chapter slug, title, position, and the checkpoint slugs in each chapter. It does not include arrival text, journal text, discoveries, or journal and discovery images. Checkpoint names and thresholds stay public, as they are today.

Authenticated journal and story reads require the caller to own the journey. Another person's journey is 403 forbidden. An unreached checkpoint, a draft or retired story, and an unknown slug are 404 not_found, with the same not-found message, so a locked body is not confirmed. Discoveries and extra images use that same gate. Acknowledgment uses it too.

Expedition membership does not grant a story the personal journey has not reached. A board that shows another person ahead does not reveal that person's unreached text, and it does not reveal text for a checkpoint the caller has not reached on their own journey.

18.6 What stays outside

  No branching, no per-user story text, and no generated prose.
  The per-account trail name drawn in challenges.py is not a table in 0010. The shared checkpoint name is the name. Retiring that draw is a product decision. It is not part of this schema.
  No user photographs, comments, or social posts. Section 13 stays deferred.
  Achievements stay section 17. Reading a story does not issue an award.
  Forecasts stay section 16.6. A story does not move the pace dot.
  challenges.py remains the running text until a reviewed migration and an explicit content cutover replace it.

18.7 Implementation sequence

Steps 1, 2, and 4 are done. Home lists pending arrival titles on the active journey and still omits journal bodies, discovery bodies, and arrival bodies. Steps 3 and 5 are not done. This sequence does not modify migrations 0001 through 0010.

  1. Apply 0010_narrative_content on the isolated test database only.
  2. Read services and PostgreSQL tests for reach, hidden unreached bodies, acknowledgment idempotence, restart, and an edit that relocks a checkpoint.
  3. Seed rows for Everest and one distance-only route from the existing prose and photo files. Publish a photograph only when section 18.4 accepts its rights. Do not treat Everest as the only shape.
  4. Add the proposed API fields. Home still omits journal bodies.
  5. Point scene selection at published route_hero and checkpoint_scene rows, keeping the current fallback and the current files until each asset_path resolves.

18.8 Open decisions

These are not settled by the rest of this document.

  1. The per-account trail name in challenges.py. Section 18.6 leaves it out of 0010. Retiring the draw, or keeping it as a label beside the shared checkpoint name, needs a product decision.
  2. A retired story or media row is hidden from every caller, including an attempt that already acknowledged it. The alternative is to keep serving it to attempts that have a view row. Section 18.4 already blocks publishing an undocumented or noncommercial photograph. Retirement is how a published asset is pulled later.
  3. Route photography bytes stay in adventurer/static/photos until a later move to object storage or a CDN. The column is only a path. Organization logo storage remains section 15 and is a separate choice.
  4. Published chapter titles and their checkpoint slugs are public on the route catalog. Bodies are not. If a title would spoil a later place, the catalog rule needs to change before implementation.
  5. A view row survives an edit or delete that drops the checkpoint below its thresholds. The content is hidden until the same attempt reaches it again, and the automatic reveal does not play twice. Deleting the view on relock would show the reveal again.
  6. Discovery bodies are prose. A recipe does not get an ingredients table in 0010. Add columns later if the layout needs them.
