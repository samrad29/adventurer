"""Record route finishes and add managed achievement issuance."""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0018_achievement_engine"
down_revision = "0017_trail_identity"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade():
    execute_script(SQL / "0018_achievement_engine.sql")


def downgrade():
    execute_script(SQL / "0018_achievement_engine_down.sql")
