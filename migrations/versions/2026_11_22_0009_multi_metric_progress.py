"""Replace elevation-only progress with metric requirements and credits.

Downgrade restores the elevation columns only when no new-model progress
exists. It does not rebuild discarded journey credits or expedition snapshots.
"""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0009_multi_metric_progress"
down_revision = "0008_privacy_removals"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0009_multi_metric_progress.sql")


def downgrade() -> None:
    execute_script(_DIR / "0009_multi_metric_progress_down.sql")
