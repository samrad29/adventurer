"""Remember a privacy removal without storing it as an invitation.

The row is the pair of user and expedition. Rejoin consumes it and starts
the board clock. Downgrade drops only that table.
"""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0008_privacy_removals"
down_revision = "0007_domain_foundation"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0008_privacy_removals.sql")


def downgrade() -> None:
    execute_script(_DIR / "0008_privacy_removals_down.sql")
