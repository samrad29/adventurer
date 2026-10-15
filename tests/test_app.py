import re
from datetime import date, timedelta

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def _csrf(client, path):
    page = client.get(path)
    assert page.status_code == 200
    match = CSRF_RE.search(page.get_data(as_text=True))
    assert match, path
    return match.group(1)


def _join(client, name="Sam", password="trailhead", invite=""):
    token = _csrf(client, "/join")
    response = client.post(
        "/join",
        data={
            "csrf": token,
            "name": name,
            "password": password,
            "confirm": password,
            "invite": invite,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    return response


def _log(client, elevation, note="", when=None):
    token = _csrf(client, "/everest/log")
    return client.post(
        "/everest/log",
        data={
            "csrf": token,
            "elevation_ft": str(elevation),
            "distance_km": "",
            "happened_on": when or date.today().isoformat(),
            "note": note,
        },
        follow_redirects=True,
    )


def test_health(client):
    assert client.get("/health").status_code == 200


def test_home_explains_the_climb(client):
    page = client.get("/")
    assert page.status_code == 302
    page = client.get("/everest")
    text = page.get_data(as_text=True)
    assert "Sagarmatha" in text
    assert "Namche Bazaar" in text
    assert "The board is empty" in text


def test_approach_checkpoints_open_before_lukla(client):
    from adventurer.challenges import display_name, get_challenge

    _join(client)
    hill = _log(client, "1951.5")
    hill_text = hill.get_data(as_text=True)
    assert "Opened Timms Hill" in hill_text
    assert "Price County" in hill_text
    assert "photos/lukla.jpg" not in hill_text

    four = _log(client, "2048.5")
    four_text = four.get_data(as_text=True)
    assert "Opened 4,000 feet" in four_text
    assert "walk the steep pitches" in four_text

    named = _log(client, "3000")
    trail = next(stop for stop in get_challenge("everest")["stops"] if stop["slug"] == "seven-thousand")
    with client.application.app_context():
        from adventurer.db import get_db

        account_id = get_db().execute(
            "SELECT id FROM adventurers WHERE name_key = ?",
            ("sam",),
        ).fetchone()["id"]
    expected = display_name(trail, account_id, unlocked=True)
    named_text = named.get_data(as_text=True)
    assert f"Opened {expected}" in named_text
    assert f"<h1>{expected}</h1>" in named_text
    again = client.get("/everest/stops/seven-thousand").get_data(as_text=True)
    assert f"<h1>{expected}</h1>" in again

    board = client.get("/everest/board").get_data(as_text=True)
    assert expected in board


def test_join_log_and_unlock_lukla(client):
    _join(client)
    opened = _log(client, 9383)
    text = opened.get_data(as_text=True)
    assert "and Lukla." in text
    assert "Butter tea" in text
    assert "Dal bhat" not in text

    namche = client.get("/everest/stops/namche")
    assert "Still ahead" in namche.get_data(as_text=True)
    assert "Saturday market" not in namche.get_data(as_text=True)
    assert "photos/lukla.jpg" in text
    assert "Vyacheslav Argenberg" in text
    assert "photos/namche.jpg" not in namche.get_data(as_text=True)


def test_notes_stay_off_the_board(client):
    _join(client, "Sam")
    _log(client, 400, note="secret knee")
    board = client.get("/everest/board").get_data(as_text=True)
    journal = client.get("/everest/journal").get_data(as_text=True)
    assert "secret knee" not in board
    assert "secret knee" in journal
    assert "Sam" in board


def test_someone_else_cannot_delete_your_log(client, app):
    _join(client, "Sam")
    _log(client, 100, note="mine")
    alex = app.test_client()
    _join(alex, "Alex")
    token = _csrf(alex, "/everest/journal")
    stolen = alex.post("/everest/logs/1/delete", data={"csrf": token})
    assert stolen.status_code == 404
    assert "mine" in client.get("/everest/journal").get_data(as_text=True)


def test_leaderboard_ranks_more_elevation_first(app):
    sam = app.test_client()
    alex = app.test_client()
    _join(sam, "Sam")
    _join(alex, "Alex")
    _log(sam, 500)
    _log(alex, 1200)
    text = sam.get("/everest/board").get_data(as_text=True)
    names = re.findall(r"<strong>([^<]+)</strong>", text)
    assert names == ["Alex", "Sam"]


def test_invite_code_is_required_when_set(app, client):
    app.config["INVITE_CODE"] = "khumbu"
    token = _csrf(client, "/join")
    rejected = client.post(
        "/join",
        data={
            "csrf": token,
            "name": "Sam",
            "password": "trailhead",
            "confirm": "trailhead",
            "invite": "nope",
        },
    )
    assert rejected.status_code == 400
    token = _csrf(client, "/join")
    accepted = client.post(
        "/join",
        data={
            "csrf": token,
            "name": "Sam",
            "password": "trailhead",
            "confirm": "trailhead",
            "invite": "khumbu",
        },
        follow_redirects=True,
    )
    assert accepted.status_code == 200
    assert "Log a climb" in accepted.get_data(as_text=True)


def test_api_hides_unlocks_until_they_are_earned(client):
    _join(client, "Sam")
    token = client.get("/api/v1/session").get_json()["csrf"]
    locked = client.get("/api/v1/challenges/everest").get_json()
    assert locked["you"]["name"] == "Sam"
    assert locked["progress"]["stops"][0]["unlocks"] == []

    created = client.post(
        "/api/v1/challenges/everest/logs",
        json={"elevation_ft": 10000, "note": "long hills"},
        headers={"X-CSRF-Token": token},
    )
    assert created.status_code == 201
    assert created.get_json()["opened"] == [
        "timms-hill",
        "four-thousand",
        "seven-thousand",
        "lukla",
    ]
    finished = client.post(
        "/api/v1/challenges/everest/logs",
        json={"elevation_ft": 1286},
        headers={"X-CSRF-Token": token},
    )
    assert finished.status_code == 201
    body = finished.get_json()
    assert body["opened"] == ["namche"]
    namche = next(stop for stop in body["progress"]["stops"] if stop["slug"] == "namche")
    assert any(item["title"] == "Dal bhat" for item in namche["unlocks"])
    board = client.get("/api/v1/challenges/everest/leaderboard").get_json()
    assert board["leaderboard"][0]["name"] == "Sam"
    assert "long hills" not in str(board)


def test_future_date_is_rejected(client):
    _join(client)
    response = _log(client, 20, when=(date.today() + timedelta(days=1)).isoformat())
    assert response.status_code == 400
    assert "future" in response.get_data(as_text=True)


def test_missing_csrf_is_rejected(client):
    response = client.post("/join", data={"name": "Sam", "password": "trailhead", "confirm": "trailhead"})
    assert response.status_code == 400


def test_unknown_challenge_is_json_for_the_api(client):
    assert client.get("/api/v1/challenges/k2").status_code == 404
    assert client.get("/api/v1/challenges/k2").is_json
    assert client.get("/no-such-place").status_code == 404
    assert not client.get("/no-such-place").is_json
