"""Seed skills reference data.

Revision ID: 0002
"""
import sqlalchemy as sa
from alembic import op

from tracker.reference import ALL_SKILLS

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    skills = sa.table("skills", sa.column("id", sa.SmallInteger), sa.column("name", sa.Text))
    op.bulk_insert(skills, [{"id": sid, "name": name} for sid, name in sorted(ALL_SKILLS.items())])


def downgrade() -> None:
    op.execute("DELETE FROM skills")
