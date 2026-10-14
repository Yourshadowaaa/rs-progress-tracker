"""Completionist cape checklist: requirements synced from the RuneScape Wiki, plus manual completions.

Revision ID: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wiki_pages",
        sa.Column("title", sa.Text, primary_key=True),
        sa.Column("revid", sa.BigInteger, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("html", sa.Text, nullable=False),
    )
    op.create_table(
        "comp_requirements",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "comp_completions",
        sa.Column("requirement_key", sa.Text, sa.ForeignKey("comp_requirements.key", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("comp_completions")
    op.drop_table("comp_requirements")
    op.drop_table("wiki_pages")
