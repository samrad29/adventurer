import re

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def _csrf(client, path):
    page = client.get(path)
    assert page.status_code == 200
    match = CSRF_RE.search(page.get_data(as_text=True))
    assert match, path
    return match.group(1)


def test_health(client):
    assert client.get("/health").status_code == 200


def test_invite_code_rejects_a_wrong_code(app, client):
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
    assert "invite code" in rejected.get_data(as_text=True).lower()


def test_missing_csrf_is_rejected(client):
    response = client.post(
        "/join",
        data={"name": "Sam", "password": "trailhead", "confirm": "trailhead"},
    )
    assert response.status_code == 400


def test_unknown_challenge_is_json_for_the_api(client):
    assert client.get("/api/v1/challenges/k2").status_code == 404
    assert client.get("/api/v1/challenges/k2").is_json
    assert client.get("/no-such-place").status_code == 404
    assert not client.get("/no-such-place").is_json
