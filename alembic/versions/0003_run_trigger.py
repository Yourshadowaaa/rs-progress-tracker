"""Record what started each ingestion run (cron, manual force update, CLI).

Revision ID: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingestion_runs", sa.Column("trigger", sa.Text, nullable=False, server_default="cron"))


def downgrade() -> None:
    op.drop_column("ingestion_runs", "trigger")
