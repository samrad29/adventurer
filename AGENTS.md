# The Adventurer

Expo/React Native client in `apps/mobile`, Flask JSON API, PostgreSQL. The mobile app is the product. The server-rendered HTML frontend has been removed.

## Read first

- `docs/architecture.md` is the canonical domain architecture. Preserve its invariants. Section 16 is the progress contract. Section 17 is the award contract and is not implemented. Section 18 is the narrative contract. The journal API is implemented. Scene selection reads published `route_media` rows. Launch content is not seeded, so a route with no published media returns a null scene. `adventurer/photo_credits.json` is archival editorial attribution, not a runtime lookup. Section 19 is the organization and expedition branding contract. Resolution, validation, and branding edits are implemented on the existing columns. Asset upload and mobile branding screens are not implemented. Section 20 is the multi-sport and expedition-governed journey contract. Release 1A and Release 1B are implemented: `cycle` and `indoor_cycle`, persistent assignments, `journey_credit_origins`, and the deterministic allocation of unfinished journeys. Restricted policies, governing expeditions, eligibility replay, and route recommendations are not implemented. Journeys still credit every supported activity type.
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
