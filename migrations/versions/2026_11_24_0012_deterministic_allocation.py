"""Date a first credit at assignment time and keep that origin fixed.

Downgrade restores the credit-row clock for new origins and drops the
immutability trigger. It does not rewrite stored origins or credits.
"""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0012_deterministic_allocation"
down_revision = "0011_activity_foundations"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0012_deterministic_allocation.sql")


def downgrade() -> None:
    execute_script(_DIR / "0012_deterministic_allocation_down.sql")
