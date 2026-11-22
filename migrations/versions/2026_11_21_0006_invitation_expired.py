"""Record expired invitations separately from pending ones."""

from pathlib import Path

from migrations.sql_exec import execute_script

revision = "0006_invitation_expired"
down_revision = "0005_routes_and_expeditions"
branch_labels = None
depends_on = None

_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    execute_script(_DIR / "0006_invitation_expired.sql")


def downgrade() -> None:
    execute_script(_DIR / "0006_invitation_expired_down.sql")
