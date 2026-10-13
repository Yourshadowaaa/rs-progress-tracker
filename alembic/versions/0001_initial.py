"""Initial schema.

Revision ID: 0001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "players",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("display_name", sa.Text, nullable=False, unique=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "skills",
        sa.Column("id", sa.SmallInteger, primary_key=True, autoincrement=False),
        sa.Column("name", sa.Text, nullable=False, unique=True),
    )
    op.create_table(
        "xp_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("player_id", sa.Integer, sa.ForeignKey("players.id"), nullable=False),
        sa.Column("skill_id", sa.SmallInteger, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("captured_at_utc", TS, nullable=False),
        sa.Column("xp", sa.BigInteger, nullable=False),
        sa.Column("level", sa.Integer, nullable=False),
        sa.Column("rank", sa.Integer),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("flag", sa.Text),
        sa.UniqueConstraint("player_id", "skill_id", "captured_at_utc", name="uq_xp_snapshot"),
    )
    op.create_index("ix_xp_player_skill_time", "xp_snapshots", ["player_id", "skill_id", "captured_at_utc"])
    op.create_table(
        "adventure_log_raw",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("player_id", sa.Integer, sa.ForeignKey("players.id"), nullable=False),
        sa.Column("entry_time_utc", TS, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("details", sa.Text, nullable=False),
        sa.Column("entry_key", sa.Text, nullable=False),
        sa.Column("occurrence", sa.SmallInteger, nullable=False),
        sa.Column("first_seen_at_utc", TS, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.UniqueConstraint("player_id", "entry_key", "occurrence", name="uq_log_entry"),
    )
    op.create_index("ix_log_player_time", "adventure_log_raw", ["player_id", "entry_time_utc"])
    op.create_table(
        "adventure_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("raw_id", sa.BigInteger, sa.ForeignKey("adventure_log_raw.id", ondelete="CASCADE"), nullable=False),
        sa.Column("player_id", sa.Integer, sa.ForeignKey("players.id"), nullable=False),
        sa.Column("event_time_utc", TS, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("value", sa.BigInteger),
        sa.Column("known_subject", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("parser_version", sa.Text, nullable=False),
        sa.UniqueConstraint("raw_id", "event_type", "subject", name="uq_event"),
    )
    op.create_index("ix_events_lookup", "adventure_events", ["player_id", "event_type", "subject", "event_time_utc"])
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("rows_inserted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("warning", sa.Text),
        sa.Column("error", sa.Text),
    )
    op.create_index("ix_runs_source_started", "ingestion_runs", ["source", "started_at"])
    # XP gained between consecutive snapshots of the same skill. Summing deltas over a period
    # equals last - first snapshot in that period (plus the step into the first snapshot).
    op.execute(
        """
        CREATE VIEW xp_deltas AS
        SELECT id, player_id, skill_id, captured_at_utc, xp,
               LAG(captured_at_utc) OVER w AS prev_captured_at_utc,
               xp - LAG(xp) OVER w AS delta
        FROM xp_snapshots
        WINDOW w AS (PARTITION BY player_id, skill_id ORDER BY captured_at_utc)
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS xp_deltas")
    for table in ("ingestion_runs", "adventure_events", "adventure_log_raw", "xp_snapshots", "skills", "players"):
        op.drop_table(table)
