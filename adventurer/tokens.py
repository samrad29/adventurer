"""Bearer tokens for the versioned API.

The raw token is returned once. The database stores only its SHA-256 hash.
A cookie session is a separate credential and is not accepted as a token.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .db import get_db

TOKEN_DAYS = 30


def issue_token(user_id: int, *, client_label: str | None = None) -> str:
    raw = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=TOKEN_DAYS)
    database = get_db()
    database.execute(
        """
        INSERT INTO api_tokens (user_id, token_hash, expires_at, client_label)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, _hash(raw), expires, client_label),
    )
    database.commit()
    return raw


def authenticate_bearer(raw: str) -> int | None:
    if not raw or len(raw) > 200:
        return None
    database = get_db()
    row = database.execute(
        """
        SELECT token.id, token.user_id
        FROM api_tokens AS token
        JOIN users AS account ON account.id = token.user_id
        WHERE token.token_hash = ?
          AND token.revoked_at IS NULL
          AND token.expires_at > now()
          AND account.deletion_state IS NULL
        """,
        (_hash(raw),),
    ).fetchone()
    if row is None:
        database.rollback()
        return None
    database.execute(
        "UPDATE api_tokens SET last_used_at = now() WHERE id = ?",
        (row["id"],),
    )
    database.commit()
    return row["user_id"]


def revoke_token(raw: str, user_id: int) -> bool:
    database = get_db()
    row = database.execute(
        """
        UPDATE api_tokens
        SET revoked_at = COALESCE(revoked_at, now())
        WHERE token_hash = ? AND user_id = ?
        RETURNING id
        """,
        (_hash(raw), user_id),
    ).fetchone()
    database.commit()
    return row is not None


def revoke_all(user_id: int) -> None:
    database = get_db()
    database.execute(
        """
        UPDATE api_tokens
        SET revoked_at = COALESCE(revoked_at, now())
        WHERE user_id = ? AND revoked_at IS NULL
        """,
        (user_id,),
    )
    database.commit()


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
