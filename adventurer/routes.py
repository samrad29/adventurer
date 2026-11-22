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

from .challenges import FEATURED_SLUG, display_name
from .progress import clean_entry, format_feet, join_names, opened_between, view_model
from .db import get_db
from .photos import credit_for
from .platform import (
    PlatformError,
    accept_token,
    accept_username_invitation,
    add_full_credit,
    archive_expedition,
    create_activity,
    create_private_expedition,
    creditable_expeditions,
    create_share_link,
    create_username_invitation,
    decline_token,
    decline_username_invitation,
    delete_activity,
    incoming_invitations,
    join_public,
    list_activities,
    list_invitations,
    list_public_expeditions,
    list_published_routes,
    preview_invitation,
    published_route,
    remove_credit,
    revoke_invitation,
    update_activity,
    update_expedition,
    withdraw,
)
from .services import (
    AccountError,
    add_log,
    authenticate,
    create_adventurer,
    current_viewer,
    delete_log,
    leaderboard,
    list_logs,
    list_routes,
    load_route,
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


@bp.get("/health")
def health():
    get_db().execute("SELECT 1")
    return jsonify(ok=True)


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
        photo_credit=credit_for(stop_slug) if chosen["unlocked"] else None,
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
    if not challenge.get("can_log"):
        flash("Join this expedition before logging a climb.")
        return redirect(url_for("main.home", slug=slug))
    form = {
        "elevation_ft": "",
        "distance_km": "",
        "happened_on": date.today().isoformat(),
        "note": "",
    }
    if request.method == "POST":
        form = {key: request.form.get(key, "") for key in form}
        try:
            entry = clean_entry(
                form["elevation_ft"],
                form["distance_km"],
                form["happened_on"],
                form["note"],
                date.today(),
            )
        except ValueError as exc:
            return render_template("log.html", model=_model(challenge), form=form, error=str(exc)), 400
        before = total_elevation(viewer["id"], challenge["slug"])
        try:
            after = add_log(viewer["id"], challenge["slug"], entry)
        except PlatformError as exc:
            return render_template(
                "log.html", model=_model(challenge), form=form, error=str(exc)
            ), exc.status
        opened = opened_between(challenge, before, after)
        if opened:
            opened_names = [
                display_name(item, viewer["id"], unlocked=True) for item in opened
            ]
            flash(f"Opened {join_names(opened_names)}.")
            return redirect(url_for("main.stop", slug=slug, stop_slug=opened[-1]["slug"]))
        updated = view_model(challenge, after, viewer["id"])
        logged = format_feet(entry["elevation_ft"])
        if updated["next"] is not None:
            flash(f"Logged {logged}. {format_feet(updated['to_next_ft'])} to {updated['next']['name']}.")
        else:
            flash(f"Logged {logged}. You are on the summit, and extra feet still count.")
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
    flash("Removed from this expedition. The outing is still in your activity library.")
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
                pending_invite = session.get("invite_token")
                _login_session(created["id"])
                if pending_invite:
                    session["invite_token"] = pending_invite
                    flash("You're on the list. Accept the invitation when you're ready.")
                    return redirect(url_for("main.invitation_continue"))
                try:
                    join_public(created["id"], FEATURED_SLUG)
                except (KeyError, PlatformError):
                    flash("Your account is ready, but the public expedition could not be joined.")
                    return redirect(url_for("main.home", slug=FEATURED_SLUG))
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
        pending_invite = session.get("invite_token")
        _login_session(person["id"])
        if pending_invite:
            session["invite_token"] = pending_invite
            return redirect(url_for("main.invitation_continue"))
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
    return jsonify(list_routes())


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
            _text(payload.get("elevation_ft")),
            _text(payload.get("distance_km")),
            _text(payload.get("happened_on")),
            _text(payload.get("note")),
            date.today(),
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    before = total_elevation(viewer["id"], challenge["slug"])
    try:
        after = add_log(viewer["id"], challenge["slug"], entry)
    except KeyError:
        return jsonify(error="Not found"), 404
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    opened = opened_between(challenge, before, after)
    return jsonify(
        progress=view_model(challenge, after, viewer["id"]),
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
    return jsonify(progress=view_model(challenge, total, viewer["id"]))


@bp.get("/routes")
def route_catalog():
    return render_template("routes.html", routes=list_published_routes())


@bp.get("/routes/<slug>")
def route_detail(slug: str):
    try:
        chosen = published_route(slug)
    except KeyError:
        abort(404)
    return render_template("route.html", route=chosen)


@bp.route("/expeditions/new", methods=["GET", "POST"])
def expedition_new():
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    choices = list_published_routes()
    form = {
        "name": "",
        "description": "",
        "route_slug": choices[0]["slug"] if choices else "",
    }
    if request.method == "POST":
        form = {key: request.form.get(key, "") for key in form}
        try:
            slug = create_private_expedition(
                viewer["id"], form["route_slug"], form["name"], form["description"]
            )
        except PlatformError as exc:
            return render_template(
                "expedition_new.html", routes=choices, form=form, error=str(exc)
            ), exc.status
        return redirect(url_for("main.expedition_manage", slug=slug))
    return render_template("expedition_new.html", routes=choices, form=form, error=None)


@bp.get("/expeditions/<slug>/manage")
def expedition_manage(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    try:
        challenge = load_route(slug)
        invitations = list_invitations(viewer["id"], slug)
    except KeyError:
        abort(404)
    except PlatformError as exc:
        flash(str(exc))
        return redirect(url_for("main.home", slug=slug))
    return render_template(
        "manage.html",
        model=_model(challenge),
        invitations=invitations,
        fresh_link=None,
        error=None,
    )


@bp.post("/expeditions/<slug>/settings")
def expedition_settings(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    try:
        update_expedition(
            viewer["id"],
            slug,
            name=request.form.get("name"),
            description=request.form.get("description"),
            owner_username=request.form.get("owner_username") or None,
        )
    except KeyError:
        abort(404)
    except PlatformError as exc:
        flash(str(exc))
        return redirect(url_for("main.expedition_manage", slug=slug))
    flash("Expedition updated.")
    return redirect(url_for("main.expedition_manage", slug=slug))


@bp.post("/expeditions/<slug>/archive")
def expedition_archive(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    try:
        archive_expedition(viewer["id"], slug)
    except KeyError:
        abort(404)
    except PlatformError as exc:
        flash(str(exc))
        return redirect(url_for("main.expedition_manage", slug=slug))
    flash("Expedition archived. It stays readable and closed to new climbs.")
    return redirect(url_for("main.home", slug=slug))


@bp.post("/expeditions/<slug>/join")
def join_expedition(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    try:
        join_public(viewer["id"], slug)
    except KeyError:
        abort(404)
    except PlatformError as exc:
        flash(str(exc))
        return redirect(url_for("main.home", slug=slug))
    flash("You're on this expedition.")
    return redirect(url_for("main.home", slug=slug))


@bp.post("/expeditions/<slug>/leave")
def leave_expedition(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    try:
        withdraw(viewer["id"], slug)
    except KeyError:
        abort(404)
    except PlatformError as exc:
        flash(str(exc))
        return redirect(url_for("main.home", slug=slug))
    flash("You left the expedition. Your outings stay in your activity library.")
    try:
        load_route(slug)
    except KeyError:
        return redirect(url_for("main.route_catalog"))
    return redirect(url_for("main.home", slug=slug))


@bp.post("/expeditions/<slug>/invitations")
def expedition_invite(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    username = (request.form.get("username") or "").strip()
    try:
        if username:
            create_username_invitation(viewer["id"], slug, username)
            flash(f"Invitation ready for {username}.")
            return redirect(url_for("main.expedition_manage", slug=slug))
        created = create_share_link(viewer["id"], slug)
    except KeyError:
        abort(404)
    except PlatformError as exc:
        flash(str(exc))
        return redirect(url_for("main.expedition_manage", slug=slug))
    challenge = load_route(slug)
    return render_template(
        "manage.html",
        model=_model(challenge),
        invitations=list_invitations(viewer["id"], slug),
        fresh_link=created["token"],
        error=None,
    )


@bp.post("/expeditions/<slug>/invitations/<int:invitation_id>/revoke")
def expedition_revoke(slug: str, invitation_id: int):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    try:
        revoke_invitation(viewer["id"], slug, invitation_id)
    except KeyError:
        abort(404)
    except PlatformError as exc:
        flash(str(exc))
    else:
        flash("Invitation revoked.")
    return redirect(url_for("main.expedition_manage", slug=slug))


@bp.get("/i/<token>")
def invitation_open(token: str):
    session["invite_token"] = token
    return redirect(url_for("main.invitation_continue"))


@bp.route("/i", methods=["GET", "POST"])
def invitation_continue():
    token = session.get("invite_token")
    if not token:
        abort(404)
    preview = preview_invitation(token)
    if preview is None:
        session.pop("invite_token", None)
        abort(404)
    viewer = current_viewer()
    if request.method == "POST":
        if viewer is None:
            return redirect(url_for("main.login", next=url_for("main.invitation_continue")))
        action = request.form.get("action") or "accept"
        try:
            if action == "decline":
                decline_token(viewer["id"], token)
                session.pop("invite_token", None)
                flash("Invitation declined.")
                return redirect(url_for("main.home", slug=FEATURED_SLUG))
            slug = accept_token(viewer["id"], token)
        except PlatformError as exc:
            flash(str(exc))
            return redirect(url_for("main.invitation_continue"))
        session.pop("invite_token", None)
        flash("You're on this expedition.")
        return redirect(url_for("main.home", slug=slug))
    return render_template("invitation.html", preview=preview, viewer=viewer)


@bp.post("/invitations/<int:invitation_id>/accept")
def accept_named(invitation_id: int):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    try:
        slug = accept_username_invitation(viewer["id"], invitation_id)
    except PlatformError as exc:
        if exc.status == 404:
            abort(404)
        flash(str(exc))
        return redirect(url_for("main.activities"))
    flash("You're on this expedition.")
    return redirect(url_for("main.home", slug=slug))


@bp.post("/invitations/<int:invitation_id>/decline")
def decline_named(invitation_id: int):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    try:
        decline_username_invitation(viewer["id"], invitation_id)
    except PlatformError as exc:
        flash(str(exc))
    else:
        flash("Invitation declined.")
    return redirect(url_for("main.activities"))


@bp.route("/activities", methods=["GET", "POST"])
def activities():
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    form = {
        "elevation_ft": "",
        "distance_km": "",
        "happened_on": date.today().isoformat(),
        "note": "",
        "expedition_slug": "",
    }
    error = None
    status = 200
    if request.method == "POST":
        form = {key: request.form.get(key, "") for key in form}
        try:
            entry = clean_entry(
                form["elevation_ft"],
                form["distance_km"],
                form["happened_on"],
                form["note"],
                date.today(),
            )
            create_activity(viewer["id"], entry, form["expedition_slug"] or None)
        except ValueError as exc:
            error = str(exc)
            status = 400
        except PlatformError as exc:
            error = str(exc)
            status = exc.status
        except KeyError:
            abort(404)
        else:
            flash("Outing saved.")
            return redirect(url_for("main.activities"))
    return render_template(
        "activities.html",
        activities=list_activities(viewer["id"]),
        expeditions=creditable_expeditions(viewer["id"]),
        invitations=incoming_invitations(viewer["id"]),
        form=form,
        error=error,
    ), status


@bp.post("/activities/<int:activity_id>/edit")
def activity_edit(activity_id: int):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    try:
        entry = clean_entry(
            request.form.get("elevation_ft", ""),
            request.form.get("distance_km", ""),
            request.form.get("happened_on", ""),
            request.form.get("note", ""),
            date.today(),
        )
        update_activity(viewer["id"], activity_id, entry)
    except KeyError:
        abort(404)
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("main.activities"))
    except PlatformError as exc:
        flash(str(exc))
        return redirect(url_for("main.activities"))
    flash("Outing updated. Every expedition credit uses the new elevation.")
    return redirect(url_for("main.activities"))


@bp.post("/activities/<int:activity_id>/delete")
def activity_delete(activity_id: int):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    if request.form.get("confirm") != "delete":
        flash("Type delete to remove the outing and every credit.")
        return redirect(url_for("main.activities"))
    try:
        if not delete_activity(viewer["id"], activity_id):
            abort(404)
    except KeyError:
        abort(404)
    flash("Outing deleted, including its credits.")
    return redirect(url_for("main.activities"))


@bp.post("/activities/<int:activity_id>/credits")
def activity_credit(activity_id: int):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    try:
        add_full_credit(viewer["id"], activity_id, request.form.get("expedition_slug", ""))
    except KeyError:
        abort(404)
    except PlatformError as exc:
        flash(str(exc))
        return redirect(url_for("main.activities"))
    flash("The full elevation now counts on that expedition.")
    return redirect(url_for("main.activities"))


@bp.post("/activities/<int:activity_id>/credits/<expedition_slug>/remove")
def activity_uncredit(activity_id: int, expedition_slug: str):
    viewer = current_viewer()
    if viewer is None:
        return redirect(url_for("main.login", next=request.path))
    if not remove_credit(viewer["id"], activity_id, expedition_slug):
        abort(404)
    flash("Removed from that expedition. The outing is still in your library.")
    return redirect(url_for("main.activities"))


@bp.get("/api/v1/routes")
def api_routes():
    return jsonify(routes=[_route_json(row) for row in list_published_routes()])


@bp.get("/api/v1/routes/<slug>")
def api_route(slug: str):
    try:
        chosen = published_route(slug)
    except KeyError:
        return jsonify(error="Not found"), 404
    return jsonify(_route_json(chosen, detail=True))


@bp.get("/api/v1/expeditions")
def api_expeditions():
    rows = []
    for row in list_public_expeditions():
        rows.append(
            {
                "slug": row["slug"],
                "name": row["name"],
                "description": row["description"],
                "visibility": "public",
                "status": "active",
                "route": {
                    "slug": row["route_slug"],
                    "name": row["route_name"],
                    "region": row["region"],
                    "summit_ft": row["summit_ft"],
                },
            }
        )
    return jsonify(expeditions=rows)


@bp.get("/api/v1/expeditions/<slug>")
def api_expedition(slug: str):
    challenge = _challenge_or_404(slug)
    return jsonify(_expedition_json(challenge, detail=True))


@bp.post("/api/v1/expeditions")
def api_create_expedition():
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    payload = request.get_json(silent=True) or {}
    if payload.get("visibility", "private") != "private":
        return jsonify(error="User-created expeditions are private."), 400
    try:
        slug = create_private_expedition(
            viewer["id"],
            _text(payload.get("route_slug")),
            _text(payload.get("name")),
            _text(payload.get("description")),
        )
        challenge = load_route(slug)
    except KeyError:
        return jsonify(error="Not found"), 404
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(_expedition_json(challenge, detail=True)), 201


@bp.patch("/api/v1/expeditions/<slug>")
def api_update_expedition(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    payload = request.get_json(silent=True) or {}
    try:
        update_expedition(
            viewer["id"],
            slug,
            name=payload.get("name"),
            description=payload.get("description"),
            owner_username=payload.get("owner_username"),
        )
        challenge = load_route(slug)
    except KeyError:
        return jsonify(error="Not found"), 404
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(_expedition_json(challenge))


@bp.post("/api/v1/expeditions/<slug>/archive")
def api_archive_expedition(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    try:
        archive_expedition(viewer["id"], slug)
        challenge = load_route(slug)
    except KeyError:
        return jsonify(error="Not found"), 404
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(_expedition_json(challenge))


@bp.post("/api/v1/expeditions/<slug>/participation")
def api_join(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    try:
        challenge = join_public(viewer["id"], slug)
    except KeyError:
        return jsonify(error="Not found"), 404
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(_expedition_json(challenge))


@bp.delete("/api/v1/expeditions/<slug>/participation")
def api_leave(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    try:
        withdraw(viewer["id"], slug)
    except KeyError:
        return jsonify(error="Not found"), 404
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(ok=True)


@bp.get("/api/v1/expeditions/<slug>/leaderboard")
def api_expedition_leaderboard(slug: str):
    challenge = _challenge_or_404(slug)
    return jsonify(leaderboard=leaderboard(challenge))


@bp.post("/api/v1/expeditions/<slug>/invitations")
def api_create_invitation(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    payload = request.get_json(silent=True) or {}
    username = _text(payload.get("username")).strip()
    kind = _text(payload.get("kind")).strip()
    try:
        if username and kind == "link":
            return jsonify(error="Send a username or a share link, not both."), 400
        if username:
            return jsonify(create_username_invitation(viewer["id"], slug, username)), 201
        if kind == "link":
            return jsonify(create_share_link(viewer["id"], slug)), 201
    except KeyError:
        return jsonify(error="Not found"), 404
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(error="Send a username or kind link."), 400


@bp.get("/api/v1/expeditions/<slug>/invitations")
def api_list_invitations(slug: str):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    try:
        rows = list_invitations(viewer["id"], slug)
    except KeyError:
        return jsonify(error="Not found"), 404
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(invitations=rows)


@bp.delete("/api/v1/expeditions/<slug>/invitations/<int:invitation_id>")
def api_revoke_invitation(slug: str, invitation_id: int):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    try:
        revoke_invitation(viewer["id"], slug, invitation_id)
    except KeyError:
        return jsonify(error="Not found"), 404
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(ok=True)


@bp.post("/api/v1/invitations/<token>/accept")
def api_accept_invitation(token: str):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    try:
        slug = accept_token(viewer["id"], token)
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(slug=slug)


@bp.post("/api/v1/invitations/<token>/decline")
def api_decline_invitation(token: str):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    try:
        decline_token(viewer["id"], token)
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(ok=True)


@bp.get("/api/v1/activities")
def api_activities():
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    return jsonify(activities=list_activities(viewer["id"]))


@bp.post("/api/v1/activities")
def api_create_activity():
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    payload = request.get_json(silent=True) or {}
    try:
        entry = clean_entry(
            _text(payload.get("elevation_ft")),
            _text(payload.get("distance_km")),
            _text(payload.get("happened_on")),
            _text(payload.get("note")),
            date.today(),
        )
        activity_id = create_activity(
            viewer["id"], entry, _text(payload.get("expedition_slug")).strip() or None
        )
    except KeyError:
        return jsonify(error="Not found"), 404
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(id=activity_id), 201


@bp.patch("/api/v1/activities/<int:activity_id>")
def api_update_activity(activity_id: int):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    payload = request.get_json(silent=True) or {}
    try:
        entry = clean_entry(
            _text(payload.get("elevation_ft")),
            _text(payload.get("distance_km")),
            _text(payload.get("happened_on")),
            _text(payload.get("note")),
            date.today(),
        )
        update_activity(viewer["id"], activity_id, entry)
    except KeyError:
        return jsonify(error="Not found"), 404
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(ok=True)


@bp.delete("/api/v1/activities/<int:activity_id>")
def api_delete_activity(activity_id: int):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    if not delete_activity(viewer["id"], activity_id):
        return jsonify(error="Not found"), 404
    return jsonify(ok=True)


@bp.post("/api/v1/activities/<int:activity_id>/credits")
def api_add_credit(activity_id: int):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    payload = request.get_json(silent=True) or {}
    try:
        add_full_credit(viewer["id"], activity_id, _text(payload.get("expedition_slug")))
    except KeyError:
        return jsonify(error="Not found"), 404
    except PlatformError as exc:
        return jsonify(error=str(exc)), exc.status
    return jsonify(ok=True)


@bp.delete("/api/v1/activities/<int:activity_id>/credits/<expedition_slug>")
def api_remove_credit(activity_id: int, expedition_slug: str):
    viewer = current_viewer()
    if viewer is None:
        return jsonify(error="Log in first."), 401
    if not remove_credit(viewer["id"], activity_id, expedition_slug):
        return jsonify(error="Not found"), 404
    return jsonify(ok=True)


def _challenge_or_404(slug: str) -> dict:
    try:
        return load_route(slug)
    except KeyError:
        abort(404)


def _model(challenge: dict) -> dict:
    viewer = current_viewer()
    total = total_elevation(viewer["id"], challenge["slug"]) if viewer else 0
    return view_model(challenge, total, viewer["id"] if viewer else None)


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


def _route_json(route: dict, *, detail: bool = False) -> dict:
    body = {
        "slug": route["slug"],
        "name": route["name"],
        "region": route["region"],
        "summary": route["summary"],
        "summit_ft": route["summit_ft"],
    }
    if detail:
        body["checkpoints"] = [
            {
                "slug": stop["slug"],
                "name": stop["name"],
                "elevation_ft": stop["elevation_ft"],
                "place": stop["place"],
            }
            for stop in route["stops"]
        ]
        body["expeditions"] = [
            {"slug": item["slug"], "name": item["name"], "description": item["description"]}
            for item in route["expeditions"]
        ]
    return body


def _expedition_json(challenge: dict, *, detail: bool = False) -> dict:
    body = {
        "slug": challenge["slug"],
        "name": challenge["name"],
        "description": challenge["description"],
        "visibility": challenge["visibility"],
        "status": challenge["status"],
        "starts_at": _iso(challenge.get("starts_at")),
        "ends_at": _iso(challenge.get("ends_at")),
        "route": {
            "slug": challenge["route_slug"],
            "name": challenge["route_name"],
            "region": challenge["region"],
            "summit_ft": challenge["summit_ft"],
        },
        "participation_status": challenge["participation_status"],
        "is_owner": challenge["is_owner"],
        "can_join": challenge["can_join"],
        "can_log": challenge["can_log"],
        "can_manage": challenge["can_manage"],
    }
    if detail:
        body["checkpoints"] = [
            {
                "slug": stop["slug"],
                "name": stop["name"],
                "elevation_ft": stop["elevation_ft"],
            }
            for stop in challenge["stops"]
        ]
        body["progress"] = _model(challenge)
        body["you"] = _you(current_viewer())
    return body


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _you(viewer) -> dict | None:
    if viewer is None:
        return None
    return {"id": viewer["id"], "name": viewer["name"]}


def _text(value) -> str:
    if value is None:
        return ""
    return str(value)
