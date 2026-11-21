"""Baseline the existing adventurers and logs tables."""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0001_legacy_baseline"
down_revision = None
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql" / "0001_legacy_baseline.sql"


def upgrade() -> None:
    execute_script(_SQL)


def downgrade() -> None:
    # Legacy tables stay. Rolling back must not drop accounts or logs.
    pass
