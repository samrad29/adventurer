"""Alembic environment.

The URL comes only from TEST_DATABASE_URL. DATABASE_URL is the shared
Supabase database and is never read here.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from urllib.parse import urlparse

from alembic import context
from sqlalchemy import create_engine, pool, text

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def migration_url() -> str:
    raw = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not raw:
        raise RuntimeError(
            "Set TEST_DATABASE_URL to an isolated Postgres database named "
            "adventurer_migration_test. This project does not read DATABASE_URL."
        )
    if "supabase" in raw.lower() or "render.com" in raw.lower():
        raise RuntimeError("Refusing a hosted database URL.")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(
            "Refusing database host "
            f"{host!r}. Phase 1 migrations run only against localhost."
        )
    database = parsed.path.lstrip("/").split("/", 1)[0]
    if database != "adventurer_migration_test":
        raise RuntimeError(
            "Refusing database "
            f"{database!r}. Use the isolated database adventurer_migration_test."
        )
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    if raw.startswith("postgresql+psycopg://"):
        return raw
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw[len("postgresql://") :]
    raise RuntimeError("TEST_DATABASE_URL must be a PostgreSQL URL.")


def run_migrations_offline() -> None:
    context.configure(
        url=migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="alembic_version",
        version_table_schema="adventurer",
        transaction_per_migration=True,
    )
    context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(migration_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS adventurer"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="alembic_version",
            version_table_schema="adventurer",
            transaction_per_migration=True,
        )
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
