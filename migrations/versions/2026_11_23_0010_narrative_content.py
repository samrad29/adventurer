"""Add the shared narrative catalog without changing progress.

Downgrade drops only the tables, triggers, and checkpoint column this
revision adds. It does not rebuild or alter journey credits.
"""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0010_narrative_content"
down_revision = "0009_multi_metric_progress"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0010_narrative_content.sql")


def downgrade() -> None:
    execute_script(_DIR / "0010_narrative_content_down.sql")
