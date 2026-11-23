"""Process health and the JSON error contract that replaced the HTML pages."""

from __future__ import annotations


def test_health(client):
    assert client.get("/health").status_code == 200
    assert client.get("/health").get_json()["ok"] is True


def test_root_is_json(client):
    root = client.get("/")
    assert root.status_code == 200
    assert root.get_json()["api"] == "/api/v1"


def test_unknown_paths_are_json(client):
    missing = client.get("/no-such-place")
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "not_found"
    api = client.get("/api/v1/challenges/k2")
    assert api.status_code == 404
    assert api.get_json()["code"] == "not_found"


def test_a_cookie_change_without_csrf_is_rejected(client):
    created = client.post(
        "/api/v1/auth/register",
        json={"name": "Sam", "password": "trailhead"},
    )
    assert created.status_code == 201
    rejected = client.post("/api/v1/journeys/start", json={"route_slug": "everest"})
    assert rejected.status_code == 400
    assert rejected.get_json()["code"] == "csrf"
    allowed = client.post(
        "/api/v1/journeys/start",
        json={"route_slug": "everest"},
        headers={"X-CSRF-Token": created.get_json()["csrf"]},
    )
    assert allowed.status_code == 201
