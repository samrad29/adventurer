"""Seed the Everest expedition and its checkpoints."""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0003_seed_everest"
down_revision = "0002_phase1_schema"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0003_seed_everest.sql")


def downgrade() -> None:
    execute_script(_DIR / "0003_seed_everest_down.sql")
