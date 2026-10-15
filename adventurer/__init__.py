"""The Adventurer application factory."""

from __future__ import annotations

import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db
from .challenges import FEATURED_SLUG, get_challenge
from .progress import format_day, format_km, format_feet, ordinal
from .services import current_viewer, place_label

load_dotenv()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or None,
        DATABASE=os.path.join(app.instance_path, "adventurer.db"),
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
        INVITE_CODE=os.environ.get("INVITE_CODE", ""),
        MAX_CONTENT_LENGTH=64 * 1024,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = _secret_key(app)
    if os.environ.get("RENDER"):
        app.config["SESSION_COOKIE_SECURE"] = True
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    from . import routes

    app.register_blueprint(routes.bp)
    app.add_template_filter(format_feet, "feet")
    app.add_template_filter(format_km, "km")
    app.add_template_filter(format_day, "day")
    app.add_template_filter(ordinal, "ordinal")
    app.add_template_filter(place_label, "place_label")

    @app.before_request
    def guard_post():
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("csrf", "")
        if not expected or not sent or not hmac.compare_digest(sent, expected):
            if request.path.startswith("/api/"):
                return jsonify(error="Refresh the page and try again."), 400
            return render_template("expired.html"), 400
        return None

    @app.context_processor
    def inject_globals():
        slug = FEATURED_SLUG
        if request.view_args and request.view_args.get("slug"):
            slug = request.view_args["slug"]
        try:
            challenge = get_challenge(slug)
        except KeyError:
            challenge = get_challenge(FEATURED_SLUG)
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
            return jsonify(error="Not found"), 404
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
