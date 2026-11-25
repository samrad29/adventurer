# The Adventurer

Expo/React Native client in `apps/mobile`, Flask JSON API, PostgreSQL. The mobile app is the product. The server-rendered HTML frontend has been removed.

## Read first

- `docs/architecture.md` is the canonical domain architecture. Preserve its invariants. Section 16 is the progress contract. Section 17 is the award contract. The schema and transactional issuance foundation are implemented. Definitions are unseeded. Source-specific issuers and trophy case presentation are deferred. Section 18 is the narrative contract. The journal API is implemented. Scene selection reads published `route_media` rows. Launch content is not seeded, so a route with no published media returns a null scene. `adventurer/photo_credits.json` is archival editorial attribution, not a runtime lookup. Section 19 is the organization and expedition branding contract. Resolution, validation, and branding edits are implemented on the existing columns. Asset upload and mobile branding screens are not implemented. Section 20 is the multi-sport and expedition-governed journey contract. Release 1A and Release 1B are implemented: `cycle` and `indoor_cycle`, persistent assignments, `journey_credit_origins`, and the deterministic allocation of unfinished journeys. Assignment `created_at` and a privacy-rejoin cutoff are chosen under the journey lock. An unassigned activity that stays unassigned does not change `progress_version`. The allocation preflight is a read-only diagnostic. Release 2 is implemented: restricted and unrestricted expedition policies, governing expeditions, join confirmation, succession, keep, and expansion. Restart clears an unfinished attempt's governor without opening a policy choice, and an eligibility expansion does not resume a paused journey. The allocator in `adventurer/allocation.py` applies the journey's stored policy. Release 3 adds editorial route recommendations to content imports and the route catalog. A journey credits a supported activity type only when that policy accepts it. Section 21 is the Trail Identity contract. The schema, editorial imports, account identity, and Flask presentation are implemented. Launch names, qualifying launch route content, and mobile presentation remain pending.
- `docs/content-authoring.md` is the editorial package format. Importing a package does not change journey progress.
- `docs/media-infrastructure.md` is the media lifecycle, storage, optimization, and delivery note. It does not override the domain, package, scene, or HTTP contracts.
- `docs/api-v1.md` is the HTTP contract. Fields and routes marked proposed are not implemented.
- `docs/dynamic-home.md` is the home presentation spec. It does not override `docs/architecture.md`.
- `docs/phase1-schema.md` and `docs/phase3-schema.md` describe historical migrations. They are not the current progress model.

Implemented behavior is the code, the applied migrations, and the parts of those docs that describe what the API returns today. Proposed sections do not authorize schema or API changes by themselves.

## Backend changes

- Test against the isolated database in `TEST_DATABASE_URL` (`postgresql://postgres:postgres@127.0.0.1:54329/adventurer_migration_test`). Do not use `DATABASE_URL`, hosted Supabase, or Render.
- Do not modify migrations `0001` through `0010` without explicit approval. New schema is a new migration file.
- Alembic may run only against localhost and only against `adventurer_migration_test`.
- Do not run two pytest processes against that database at once.

## Approval

Do not commit, push, deploy, or change a hosted database unless the request explicitly asks for that step.

## Engineering workflow

- This is pre-launch V1. Avoid unnecessary backward compatibility and operational infrastructure.
- Follow the canonical architecture. Read relevant architecture sections and affected source files rather than rereading entire documents.
- Keep changes scoped to the requested task and avoid unrelated refactoring.
- Prefer existing services and abstractions over new implementations.
- Run targeted regression tests during development. Run the full suite at release milestones or when shared infrastructure changes materially.
- Update documentation when contracts or implemented behavior change.
- Keep implementation reports concise: changes, tests, unresolved issues, and architectural deviations.
- Avoid restating existing documentation in responses.
- Raise material architectural ambiguities rather than inventing product behavior.
