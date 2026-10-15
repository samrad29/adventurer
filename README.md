# The Adventurer

A private climb for friends. The first route is Everest, on the Nepal side. You log elevation you actually gain — runs, hikes, stairs, walks — and camps open when the total reaches them. An open camp can hold a picture, a Himalayan recipe, or a short note about that place. Everyone shares a leaderboard. Notes stay in your own journal.

This is a Flask app on purpose. A Next.js frontend can sit on the JSON API later without throwing away the climb.

## Run it

Python 3.11 or newer.

```powershell
cd the_adventurer
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
flask run --debug
```

Open http://127.0.0.1:5000

Copy `.env.example` to `.env` if you want a fixed `SECRET_KEY` or an `INVITE_CODE`. With no invite code, anyone who can open the site can join. That is fine on your own machine. If friends will reach it over the network, set an invite code and keep the site off the public internet.

```powershell
flask run --debug --host=0.0.0.0
```

## How progress works

The summit target is **8,849 m**, a rounded form of the 2020 survey height (8,848.86 m). Log one outing at a time, up to 3,000 m, so a typo cannot finish the mountain. Older dates are allowed when someone is catching up.

The route is Lukla, Namche Bazaar, Tengboche, Dingboche, Lobuche, Gorak Shep, Everest Base Camp, Kala Patthar, then Camps I–III, the South Col, and the summit. Kala Patthar is the viewpoint, not a camp on the climb. Heights are widely published figures, rounded.

Drop a photo in `adventurer/static/photos/` named for the stop, for example `namche.jpg` (also `.jpeg`, `.webp`, or `.png`). It shows once that stop is open.

## Tests

```powershell
python -m pytest
```

## Another route later

Add a challenge dict in `adventurer/challenges.py` and append it to `CHALLENGES`. Logs are stored with a challenge slug, so a second mountain does not reuse Everest's meters. Stops need a slug, a name, an elevation, a place (`village`, `camp`, `viewpoint`, or `summit`), a one-line summary, and unlocks.

An unlock is one of:

- `scene` — what that spot looks like
- `tidbit` — a historical or cultural note
- `recipe` — `makes`, `ingredients`, `steps`, and an optional `aside`

A `story` kind can be added the same way when you want one. The last stop's elevation has to match `summit_m`.

## JSON, for a later frontend

The pages use a session cookie. These endpoints speak JSON and expect that same cookie, plus the CSRF token from `GET /api/v1/session` on any change.

- `GET /api/v1/session`
- `GET /api/v1/challenges`
- `GET /api/v1/challenges/everest`
- `GET /api/v1/challenges/everest/leaderboard`
- `POST /api/v1/challenges/everest/logs`
- `DELETE /api/v1/challenges/everest/logs/<id>`

Locked stops come back without their writing. A logged-out call treats you as still at the trailhead.
