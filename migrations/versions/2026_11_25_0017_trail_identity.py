"""Add account-wide trail identities and historical name presentation."""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0017_trail_identity"
down_revision = "0016_achievement_foundation"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade():
    execute_script(SQL / "0017_trail_identity.sql")


def downgrade():
    execute_script(SQL / "0017_trail_identity_down.sql")
