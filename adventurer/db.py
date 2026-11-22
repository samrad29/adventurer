"""Postgres connection for the climb. The tables live in the adventurer schema.

Schema changes belong to Alembic. This module opens a connection and does
not create or alter tables.
"""

from __future__ import annotations

from flask import current_app, g


class DB:
    def __init__(self, raw) -> None:
        self.raw = raw

    def execute(self, sql: str, params: tuple = ()):
        return self.raw.execute(sql.replace("?", "%s"), params)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def is_unique_violation(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == "23505"


def get_db() -> DB:
    if "db" not in g:
        url = (current_app.config.get("DATABASE_URL") or "").strip()
        if not url:
            raise RuntimeError("Set DATABASE_URL to the Supabase Postgres connection string.")
        g.db = _connect(url)
    return g.db


def close_db(_error=None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)


def _connect(url: str) -> DB:
    import psycopg
    from psycopg.rows import dict_row

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    connection = psycopg.connect(
        url,
        row_factory=dict_row,
        prepare_threshold=None,
        connect_timeout=15,
        options="-c search_path=adventurer",
    )
    return DB(connection)
