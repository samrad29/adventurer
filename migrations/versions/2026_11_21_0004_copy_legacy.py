"""Copy adventurers and logs into the Phase 1 tables."""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0004_copy_legacy"
down_revision = "0003_seed_everest"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0004_copy_legacy.sql")


def downgrade() -> None:
    execute_script(_DIR / "0004_copy_legacy_down.sql")
