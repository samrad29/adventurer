"""Store expedition and journey activity rules so eligibility has one ledger.

Downgrade removes the policy tables and columns. It does not rewrite credits.
"""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0013_activity_policy"
down_revision = "0012_deterministic_allocation"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0013_activity_policy.sql")


def downgrade() -> None:
    execute_script(_DIR / "0013_activity_policy_down.sql")
