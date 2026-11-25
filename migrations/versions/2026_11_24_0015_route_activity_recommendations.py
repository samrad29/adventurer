"""Store ordered editorial activity recommendations for routes."""

from pathlib import Path

from alembic import op

from migrations.sql_exec import execute_script

revision = "0015_route_recommendations"
down_revision = "0014_completed_eligibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_script(Path(__file__).resolve().parents[1] / "sql" / "0015_route_activity_recommendations.sql")


def downgrade() -> None:
    op.execute("DROP TABLE adventurer.route_activity_recommendations")
