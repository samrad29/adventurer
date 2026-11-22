"""Separate curated routes from expedition instances."""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0005_routes_and_expeditions"
down_revision = "0004_copy_legacy"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0005_routes_and_expeditions.sql")


def downgrade() -> None:
    execute_script(_DIR / "0005_routes_and_expeditions_down.sql")
