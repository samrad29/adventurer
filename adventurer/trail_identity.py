"""Account trail names and the shared social name projection."""

from secrets import choice


class TrailIdentityError(Exception):
    def __init__(self, code, message, *, status):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = {}


def _pool_unavailable():
    return TrailIdentityError(
        "trail_name_pool_unavailable", "The trail name pool is unavailable.", status=503
    )


def _lock_pool_for_draw(database):
    database.execute("SELECT pg_advisory_xact_lock_shared(211701, 1)")


def _draw(database, excluded_id=None):
    choices = database.execute(
        "SELECT id, name FROM trail_name_pool WHERE active AND id IS DISTINCT FROM ?",
        (excluded_id,),
    ).fetchall()
    if len(choices) < (2 if excluded_id is None else 1):
        raise _pool_unavailable()
    return choice(choices)


def assign_at_checkpoint(database, user_id, reached):
    """Run under the progress writer's user lock, before feed capture."""
    if not reached or database.execute(
        "SELECT 1 FROM user_trail_identities WHERE user_id = ?", (user_id,)
    ).fetchone():
        return None
    _lock_pool_for_draw(database)
    checkpoint = database.execute(
        """
        SELECT route.slug AS route_slug, checkpoint.slug AS checkpoint_slug
        FROM trail_identity_checkpoints AS qualifying
        JOIN route_checkpoints AS checkpoint ON checkpoint.id = qualifying.checkpoint_id
        JOIN routes AS route ON route.id = checkpoint.route_id
        WHERE checkpoint.id = ANY(?)
        ORDER BY checkpoint.position, checkpoint.id
        LIMIT 1
        """,
        ([mark.id for mark in reached],),
    ).fetchone()
    if checkpoint is None:
        return None
    picked = _draw(database)
    database.execute(
        """
        INSERT INTO user_trail_identities
            (user_id, original_pool_id, current_pool_id, name, route_slug, checkpoint_slug)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, picked["id"], picked["id"], picked["name"],
         checkpoint["route_slug"], checkpoint["checkpoint_slug"]),
    )
    return {"name": picked["name"], **checkpoint}


def owner_identity(database, user_id):
    row = database.execute(
        """
        SELECT profile.trail_name_display, identity.name, identity.assigned_at,
               identity.rerolled_at, identity.revealed_at, identity.route_slug, identity.checkpoint_slug
        FROM user_profiles AS profile
        LEFT JOIN user_trail_identities AS identity USING (user_id)
        WHERE profile.user_id = ?
        """,
        (user_id,),
    ).fetchone()
    identity = reveal = None
    if row["assigned_at"] is not None:
        identity = {
            "name": row["name"], "assigned_at": row["assigned_at"],
            "reroll_available": row["rerolled_at"] is None,
            "reveal_acknowledged_at": row["revealed_at"],
        }
        if row["revealed_at"] is None:
            reveal = {key: row[key] for key in ("name", "route_slug", "checkpoint_slug")}
    return {
        "trail_name_display": row["trail_name_display"],
        "trail_identity": identity,
        "trail_name_reveal": reveal,
    }


def change_identity(database, user_id, *, preference=None, request_key=None, acknowledge=False):
    try:
        account = database.execute(
            "SELECT deletion_state FROM users WHERE id = ? FOR UPDATE", (user_id,)
        ).fetchone()
        if account is None or account["deletion_state"] is not None:
            raise TrailIdentityError("unauthenticated", "Sign in to continue.", status=401)
        if preference is not None:
            database.execute(
                """
                UPDATE user_profiles SET trail_name_display = ?
                WHERE user_id = ? AND trail_name_display <> ?
                """,
                (preference, user_id, preference),
            )
        else:
            identity = database.execute(
                """
                SELECT current_pool_id, rerolled_at, reroll_request_key
                FROM user_trail_identities WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if identity is None:
                raise TrailIdentityError("trail_identity_locked", "Your trail name is still locked.", status=409)
            if acknowledge:
                database.execute(
                    """
                    UPDATE user_trail_identities SET revealed_at = clock_timestamp()
                    WHERE user_id = ? AND revealed_at IS NULL
                    """,
                    (user_id,),
                )
            elif identity["rerolled_at"] is None:
                _lock_pool_for_draw(database)
                picked = _draw(database, identity["current_pool_id"])
                database.execute(
                    """
                    UPDATE user_trail_identities SET current_pool_id = ?, name = ?,
                        rerolled_at = clock_timestamp(), reroll_request_key = ?
                    WHERE user_id = ?
                    """,
                    (picked["id"], picked["name"], request_key, user_id),
                )
            elif identity["reroll_request_key"] != request_key:
                raise TrailIdentityError("trail_reroll_used", "You already rerolled your trail name.", status=409)
        result = owner_identity(database, user_id)
        database.commit()
        return result
    except Exception:
        database.rollback()
        raise


def social_names(database, user_ids):
    if not user_ids:
        return {}
    rows = database.execute(
        """
        SELECT profile.user_id, profile.display_name, profile.trail_name_display, identity.name
        FROM user_profiles AS profile
        LEFT JOIN user_trail_identities AS identity USING (user_id)
        WHERE profile.user_id = ANY(?)
        """,
        (user_ids,),
    ).fetchall()
    return {row["user_id"]: row for row in rows}


def display_names(current, *, captured=None):
    if current is None:
        profile_name = trail_name = None
    else:
        profile_name, trail_name = (
            (current["display_name"], current["name"]) if captured is None else captured
        )
        if current["name"] is not None and current["trail_name_display"] == "trail_only":
            profile_name = None
        if current["trail_name_display"] == "hidden":
            trail_name = None
    label = " · ".join(name for name in (profile_name, trail_name) if name)
    return {"profile_name": profile_name, "trail_name": trail_name, "display_name": label or "Adventurer"}
