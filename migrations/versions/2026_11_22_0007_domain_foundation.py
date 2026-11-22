"""Add the journey domain and retire per-expedition activity credits.

There is no live expedition progress to keep. Participations and
activity_credits are removed rather than copied onto journeys.
"""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0007_domain_foundation"
down_revision = "0006_invitation_expired"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0007_domain_foundation.sql")


def downgrade() -> None:
    execute_script(_DIR / "0007_domain_foundation_down.sql")
