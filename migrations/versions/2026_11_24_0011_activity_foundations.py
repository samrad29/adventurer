"""Add cycling types and stable credit origins without changing totals.

Downgrade drops the origin table and restores the previous activity-type
check and completed-journey activity guard. It does not rewrite credits.
"""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0011_activity_foundations"
down_revision = "0010_narrative_content"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0011_activity_foundations.sql")


def downgrade() -> None:
    execute_script(_DIR / "0011_activity_foundations_down.sql")
