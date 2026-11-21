"""Create the Phase 1 tables."""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0002_phase1_schema"
down_revision = "0001_legacy_baseline"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0002_phase1_schema.sql")


def downgrade() -> None:
    execute_script(_DIR / "0002_phase1_schema_down.sql")
