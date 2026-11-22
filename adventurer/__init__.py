"""The Adventurer application factory."""

from __future__ import annotations

import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db
from .challenges import FEATURED_SLUG
from .progress import format_day, format_km, format_feet, ordinal
from .services import current_viewer, load_route, place_label

load_dotenv()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or None,
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
        INVITE_CODE=os.environ.get("INVITE_CODE", ""),
        MAX_CONTENT_LENGTH=64 * 1024,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)
    if not (app.config.get("DATABASE_URL") or "").strip():
        raise RuntimeError("Set DATABASE_URL to the Supabase Postgres connection string.")

    os.makedirs(app.instance_path, exist_ok=True)
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = _secret_key(app)
    if os.environ.get("RENDER"):
        app.config["SESSION_COOKIE_SECURE"] = True
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    from . import api, routes

    api.init_app(app)
    app.register_blueprint(api.bp)
    app.register_blueprint(routes.bp)
    app.add_template_filter(format_feet, "feet")
    app.add_template_filter(format_km, "km")
    app.add_template_filter(format_day, "day")
    app.add_template_filter(ordinal, "ordinal")
    app.add_template_filter(place_label, "place_label")

    @app.before_request
    def resolve_actor():
        from . import limits
        from .db import get_db
        from .tokens import authenticate_bearer

        header = request.headers.get("Authorization", "")
        if header:
            key = f"{request.remote_addr or ''}\nbearer"
            if limits.too_many(key):
                return jsonify(error="Try again later.", code="rate_limited"), 429
            token = header[7:].strip() if header.startswith("Bearer ") else ""
            if not token or " " in token:
                limits.record(key)
                return jsonify(error="Log in first.", code="unauthenticated"), 401
            user_id = authenticate_bearer(token)
            if user_id is None:
                limits.record(key)
                return jsonify(error="Log in first.", code="unauthenticated"), 401
            limits.clear(key)
            g.actor_id = user_id
            g.auth_method = "bearer"
            return None
        viewer = current_viewer()
        if viewer is not None:
            row = get_db().execute(
                "SELECT deletion_state FROM users WHERE id = ?",
                (viewer["id"],),
            ).fetchone()
            get_db().rollback()
            if row is None or row["deletion_state"] == "deleting":
                session.clear()
                g.actor_id = None
                g.auth_method = None
                return None
        g.actor_id = None if viewer is None else viewer["id"]
        g.auth_method = None if viewer is None else "cookie"
        return None

    @app.before_request
    def guard_post():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        if g.get("auth_method") == "bearer":
            return None
        if request.path in {"/api/v1/auth/login", "/api/v1/auth/register"} and not session.get(
            "adventurer_id"
        ):
            return None
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("csrf", "")
        if not expected or not sent or not hmac.compare_digest(sent, expected):
            if request.path.startswith("/api/"):
                return jsonify(error="Refresh the page and try again.", code="csrf"), 400
            return render_template("expired.html"), 400
        return None

    @app.context_processor
    def inject_globals():
        slug = FEATURED_SLUG
        if request.view_args and request.view_args.get("slug"):
            slug = request.view_args["slug"]
        try:
            challenge = load_route(slug)
        except KeyError:
            challenge = load_route(FEATURED_SLUG)
        return {
            "challenge": challenge,
            "viewer": current_viewer(),
            "csrf_token": csrf_token,
            "nav": _nav,
        }

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found", code="not_found"), 404
        return render_template("404.html"), 404

    return app


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_hex(16)
        session["csrf"] = token
    return token


def _nav(name: str) -> bool:
    groups = {
        "home": {"main.home"},
        "trail": {"main.trail", "main.stop"},
        "board": {"main.board"},
        "log": {"main.log_entry"},
        "journal": {"main.journal"},
        "routes": {"main.route_catalog", "main.route_detail"},
        "activities": {"main.activities"},
    }
    return (request.endpoint or "") in groups.get(name, set())


def _secret_key(app: Flask) -> str:
    path = Path(app.instance_path) / "secret_key"
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    key = secrets.token_hex(32)
    path.write_text(key, encoding="utf-8")
    return key
