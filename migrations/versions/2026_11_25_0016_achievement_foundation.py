"""Add achievement issuance and secure route recommendations."""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0016_achievement_foundation"
down_revision = "0015_route_recommendations"
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_SQL / "0016_achievement_foundation.sql")


def downgrade() -> None:
    execute_script(_SQL / "0016_achievement_foundation_down.sql")
