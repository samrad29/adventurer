# The Adventurer

A private climb for friends. The first route is Everest, on the Nepal side, measured in feet from sea level. You log elevation you actually gain — runs, hikes, stairs, walks — and camps open when the total reaches them. An open camp can hold a picture, a Himalayan recipe, or a short note about that place. Everyone shares a leaderboard. Notes stay in your own journal.

This is a Flask app on purpose. A Next.js frontend can sit on the JSON API later without throwing away the climb.

## Run it

Accounts and climbs are stored in Supabase Postgres. `DATABASE_URL` is required. The live site is the Render service `adventurer` in Project Smirky.

```powershell
cd the_adventurer
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set `DATABASE_URL` to the Supabase session-pooler string, plus `SECRET_KEY` and `INVITE_CODE` if you want them fixed. With no invite code, anyone who can open the site can join.

The application expects the Phase 1 schema and does not create tables on startup. Schema changes go through Alembic, which only runs against the isolated local test database.

## How progress works

The summit target is **29,032 ft**, the rounded 2020 survey height, and the climb starts at sea level. Log one outing at a time, up to 10,000 ft, so a typo cannot finish the mountain. Older dates are allowed when someone is catching up.

Before Nepal, three checkpoints open on the same climb: Timms Hill in Wisconsin (1,951.5 ft, the state's highest natural point), a generic checkpoint at 4,000 ft, and a trail at 7,000 ft. That trail's name is drawn from a fixed list and stays with the account that opened it. Friends draw their own. Until it opens, the route just says "7,000 feet."

Then the route is Lukla (9,383 ft), Namche Bazaar, Tengboche, Dingboche, Lobuche, Gorak Shep, Everest Base Camp, Kala Patthar, then Camps I–III, the South Col, and the summit. Kala Patthar is the viewpoint, not a camp on the climb. Camp heights are the common published elevations, converted to feet. The summit is 29,032 ft.

Drop a replacement photo in `adventurer/static/photos/` named for the stop, for example `namche.jpg` (also `.jpeg`, `.webp`, or `.png`). The bundled pictures are freely licensed photographs from Wikimedia Commons, and the credit sits under the picture once that stop is open.

## Render

`DATABASE_URL` is the Supabase session-pooler string for the same project the run coach uses. The climb lives in its own `adventurer` schema, away from the coach tables. This branch expects that database to already have the Phase 1 tables. Do not deploy it to the live service until `DATABASE_URL` points at that schema.

For the web service that is already on Render:

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 1 --timeout 60`
- Health check path: `/health`
- Environment: `SECRET_KEY`, `DATABASE_URL`, and `INVITE_CODE`

Set an invite code before sharing the public URL. Without one, anyone who can open the site can join.

## Tests

```powershell
python -m pytest
```

The app tests use the isolated Postgres database on `127.0.0.1:54329` (`adventurer_migration_test`). They do not read `DATABASE_URL`. Each test clears user rows and extra expeditions, and leaves the Everest route and the Global Everest Challenge in place.

## Routes and expeditions

A route is the curated adventure. An expedition is one group on that route. Route metadata and checkpoints live in the database. The long checkpoint writing stays in `adventurer/challenges.py`, keyed by route slug, until the narrative model in `docs/architecture.md` is implemented. See `docs/phase3-schema.md` for the historical route migration and `docs/architecture.md` for the domain contract.

The Global Everest Challenge is still `/everest`. Account creation does not join it. The `/join` page does, unless the person is signing up from a private invitation. Users can start private expeditions on a published route, invite by username or a 30-day share link, and keep one activity library. A credit always counts the full elevation of an outing toward one expedition.

Checkpoint narrative in `adventurer/challenges.py` still uses the same unlock kinds. An unlock is one of:

- `scene` — what that spot looks like
- `tidbit` — a historical or cultural note
- `recipe` — `makes`, `ingredients`, `steps`, and an optional `aside`

A `story` kind can be added the same way when you want one. The last stop's elevation has to match `summit_ft`. The proposed chapters, arrivals, and discoveries that replace these kinds are `docs/architecture.md` section 18. They are not implemented.

## JSON, for a later frontend

The pages use a session cookie. These endpoints speak JSON and expect that same cookie, plus the CSRF token from `GET /api/v1/session` on any change.

- `GET /api/v1/session`
- `GET /api/v1/routes` and `GET /api/v1/routes/<slug>`
- `GET /api/v1/expeditions` and `GET /api/v1/expeditions/<slug>`
- `POST /api/v1/expeditions`, `PATCH /api/v1/expeditions/<slug>`, `POST /api/v1/expeditions/<slug>/archive`
- `POST` and `DELETE /api/v1/expeditions/<slug>/participation`
- `GET /api/v1/expeditions/<slug>/leaderboard`
- `POST` and `GET /api/v1/expeditions/<slug>/invitations`, `DELETE /api/v1/expeditions/<slug>/invitations/<id>`
- `POST /api/v1/invitations/<token>/accept` and `POST /api/v1/invitations/<token>/decline`
- `GET` and `POST /api/v1/activities`, `PATCH` and `DELETE /api/v1/activities/<id>`
- `POST /api/v1/activities/<id>/credits` and `DELETE /api/v1/activities/<id>/credits/<expedition_slug>`
- `GET /api/v1/challenges`
- `GET /api/v1/challenges/everest`
- `GET /api/v1/challenges/everest/leaderboard`
- `POST /api/v1/challenges/everest/logs`
- `DELETE /api/v1/challenges/everest/logs/<id>`

Locked stops come back without their writing. A logged-out call treats you as still at the trailhead.
