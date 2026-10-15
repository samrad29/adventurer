"""Pages and the small JSON API a later frontend can use."""

from __future__ import annotations

import hmac
from datetime import date
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .challenges import FEATURED_SLUG, all_challenges, get_challenge
from .progress import clean_entry, format_meters, join_names, opened_between, view_model
from .services import (
    AccountError,
    add_log,
    authenticate,
    create_adventurer,
    current_viewer,
    delete_log,
    leaderboard,
    list_logs,
    place_label,
    total_elevation,
)

bp = Blueprint("main", __name__)
PHOTO_EXTS = (".jpg", ".jpeg", ".webp", ".png")


@bp.get("/")
def root():
    return redirect(url_for("main.home", slug=FEATURED_SLUG))


@bp.get("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="favicon.svg"))


@bp.get("/<slug>")
def home(slug: str):
    challenge = _challenge_or_404(slug)
    model = _model(challenge)
    board = leaderboard(challenge)
    rank = _rank(board)
    return render_template(
        "home.html",
        model=model,
        board=board,
        rank=rank,
        board_count=len(board),
    )


@bp.get("/<slug>/trail")
def trail(slug: str):
    challenge = _challenge_or_404(slug)
    return render_template("trail.html", model=_model(challenge))


@bp.get("/<slug>/stops/<stop_slug>")
def stop(slug: str, stop_slug: str):
    challenge = _challenge_or_404(slug)
    model = _model(challenge)
    chosen = next((item for item in model["stops"] if item["slug"] == stop_slug), None)
    if chosen is None:
        abort(404)
    neighbors = _neighbors(model["stops"], stop_slug)
    return render_template(
        "stop.html",
        model=model,
        stop=chosen,
        photo=_photo(stop_slug) if chosen["unlocked"] else None,
        place_label=place_label(chosen["place"]),
        **neighbors,
    )


@bp.get("/<slug>/board")
def board(slug: str):
    challenge = _challenge_or_404(slug)
    rows = leaderboard(challenge)
    return render_template("board.html", board=rows, rank=_rank(rows))


@bp.route("/<slug>/log", methods=["GET", "POST"])
def log_entry(slug: str):
    challenge = _challenge_or_404(slug)
    viewer = _require_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    form = {
        "elevation_m": "",
        "distance_km": "",
        "happened_on": date.today().isoformat(),
        "note": "",
    }
    if request.method == "POST":
        form = {key: request.form.get(key, "") for key in form}
        try:
            entry = clean_entry(
                form["elevation_m"],
                form["distance_km"],
                form["happened_on"],
                form["note"],
                date.today(),
            )
        except ValueError as exc:
            return render_template("log.html", model=_model(challenge), form=form, error=str(exc)), 400
        before = total_elevation(viewer["id"], challenge["slug"])
        after = add_log(viewer["id"], challenge["slug"], entry)
        opened = opened_between(challenge, before, after)
        if opened:
            flash(f"Opened {join_names([item['name'] for item in opened])}.")
            return redirect(url_for("main.stop", slug=slug, stop_slug=opened[-1]["slug"]))
        updated = view_model(challenge, after)
        logged = format_meters(entry["elevation_m"])
        if updated["next"] is not None:
            flash(f"Logged {logged}. {format_meters(updated['to_next_m'])} to {updated['next']['name']}.")
        else:
            flash(f"Logged {logged}. You are on the summit, and extra meters still count.")
        return redirect(url_for("main.log_entry", slug=slug))
    return render_template("log.html", model=_model(challenge), form=form, error=None)


@bp.get("/<slug>/journal")
def journal(slug: str):
    challenge = _challenge_or_404(slug)
    viewer = _require_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    return render_template(
        "journal.html",
        model=_model(challenge),
        logs=list_logs(viewer["id"], challenge["slug"]),
    )


@bp.post("/<slug>/logs/<int:log_id>/delete")
def remove_log(slug: str, log_id: int):
    challenge = _challenge_or_404(slug)
    viewer = _require_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    if not delete_log(viewer["id"], challenge["slug"], log_id):
        abort(404)
    flash("Entry removed.")
    return redirect(url_for("main.journal", slug=slug))


@bp.route("/join", methods=["GET", "POST"])
def join():
    if current_viewer() is not None:
        return redirect(url_for("main.home", slug=FEATURED_SLUG))
    error = None
    name = ""
    if request.method == "POST":
        name = request.form.get("name", "")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not _invite_ok(request.form.get("invite", "")):
            error = "That invite code does not match."
        elif password != confirm:
            error = "Those passwords don't match."
        else:
            try:
                created = create_adventurer(name, password)
            except AccountError as exc:
                error = str(exc)
            else:
                _login_session(created["id"])
                flash("You're on the list. Log a climb when you have one.")
                return redirect(url_for("main.log_entry", slug=FEATURED_SLUG))
        if error:
            return render_template(
                "auth.html",
                mode="join",
                error=error,
                name=name,
                needs_invite=_needs_invite(),
            ), 400
    return render_template(
        "auth.html",
        mode="join",
        error=None,
        name=name,
        needs_invite=_needs_invite(),
    )


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_viewer() is not None and request.method == "GET":
        return redirect(_safe_next(request.args.get("next")))
    error = None
    name = ""
    if request.method == "POST":
        name = request.form.get("name", "")
        person = authenticate(name, request.form.get("password", ""))
        if person is None:
            error = "That name and password don't match."
            return render_template(
                "auth.html",
                mode="login",
                error=error,
                name=name,
                needs_invite=False,
                next_url=request.form.get("next", ""),
            ), 400
        _login_session(person["id"])
        return redirect(_safe_next(request.form.get("next")))
    return render_template(
        "auth.html",
        mode="login",
        error=None,
        name=name,
        needs_invite=False,
        next_url=request.args.get("next", ""),
    )


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.home", slug=FEATURED_SLUG))


@bp.get("/api/v1/session")
def api_session():
    viewer = current_viewer()
    return jsonify(csrf=_csrf(), you=_you(viewer))


@bp.get("/api/v1/challenges")
def api_challenges():
    return jsonify(
        [
            {
                "slug": challenge["slug"],
                "name": challenge["name"],
                "region": challenge["region"],
                "summit_m": challenge["summit_m"],
            }
            for challenge in all_challenges()
        ]
    )


@bp.get("/api/v1/challenges/<slug>")
def api_challenge(slug: str):
    challenge = _challenge_or_404(slug)
    model = _model(challenge)
    return jsonify(progress=model, you=_you(current_viewer()))


@bp.get("/api/v1/challenges/<slug>/leaderboard")
def api_leaderboard(slug: str):
    challenge = _challenge_or_404(slug)
    return jsonify(leaderboard=leaderboard(challenge))


@bp.post("/api/v1/challenges/<slug>/logs")
def api_add_log(slug: str):
    challenge = _challenge_or_404(slug)
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    payload = request.get_json(silent=True) or {}
    try:
        entry = clean_entry(
            _text(payload.get("elevation_m")),
            _text(payload.get("distance_km")),
            _text(payload.get("happened_on")),
            _text(payload.get("note")),
            date.today(),
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    before = total_elevation(viewer["id"], challenge["slug"])
    after = add_log(viewer["id"], challenge["slug"], entry)
    opened = opened_between(challenge, before, after)
    return jsonify(
        progress=view_model(challenge, after),
        opened=[item["slug"] for item in opened],
    ), 201


@bp.delete("/api/v1/challenges/<slug>/logs/<int:log_id>")
def api_delete_log(slug: str, log_id: int):
    challenge = _challenge_or_404(slug)
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    if not delete_log(viewer["id"], challenge["slug"], log_id):
        return jsonify(error="Not found"), 404
    total = total_elevation(viewer["id"], challenge["slug"])
    return jsonify(progress=view_model(challenge, total))


def _challenge_or_404(slug: str) -> dict:
    try:
        return get_challenge(slug)
    except KeyError:
        abort(404)


def _model(challenge: dict) -> dict:
    viewer = current_viewer()
    total = total_elevation(viewer["id"], challenge["slug"]) if viewer else 0
    return view_model(challenge, total)


def _require_viewer():
    return current_viewer()


def _rank(rows: list[dict]) -> int | None:
    viewer = current_viewer()
    if viewer is None:
        return None
    for index, row in enumerate(rows, start=1):
        if row["id"] == viewer["id"]:
            return index
    return None


def _neighbors(stops: list[dict], stop_slug: str) -> dict:
    index = next(i for i, stop in enumerate(stops) if stop["slug"] == stop_slug)
    return {
        "previous_stop": stops[index - 1] if index else None,
        "following_stop": stops[index + 1] if index + 1 < len(stops) else None,
    }


def _photo(stop_slug: str) -> str | None:
    folder = Path(current_app.static_folder) / "photos"
    for ext in PHOTO_EXTS:
        if (folder / f"{stop_slug}{ext}").is_file():
            return f"photos/{stop_slug}{ext}"
    return None


def _needs_invite() -> bool:
    return bool(current_app.config.get("INVITE_CODE"))


def _invite_ok(submitted: str) -> bool:
    expected = current_app.config.get("INVITE_CODE") or ""
    if not expected:
        return True
    return hmac.compare_digest((submitted or "").strip(), expected)


def _login_session(adventurer_id: int) -> None:
    session.clear()
    session["adventurer_id"] = adventurer_id
    session.permanent = True
    session["csrf"] = _csrf()


def _csrf() -> str:
    from . import csrf_token

    return csrf_token()


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("main.home", slug=FEATURED_SLUG)


def _you(viewer) -> dict | None:
    if viewer is None:
        return None
    return {"id": viewer["id"], "name": viewer["name"]}


def _text(value) -> str:
    if value is None:
        return ""
    return str(value)
