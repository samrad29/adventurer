"""Reject an eligibility change on an activity assigned to a completed journey.

Downgrade removes the trigger. It does not rewrite activities or credits.
"""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0014_completed_eligibility"
down_revision = "0013_activity_policy"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0014_completed_activity_eligibility.sql")


def downgrade() -> None:
    execute_script(_DIR / "0014_completed_activity_eligibility_down.sql")
