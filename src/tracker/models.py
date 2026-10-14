"""SQLAlchemy models. The schema is created by Alembic migrations, not by create_all()."""

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, SmallInteger, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Player(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Skill(Base):
    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, unique=True)


class XpSnapshot(Base):
    __tablename__ = "xp_snapshots"
    __table_args__ = (
        UniqueConstraint("player_id", "skill_id", "captured_at_utc", name="uq_xp_snapshot"),
        Index("ix_xp_player_skill_time", "player_id", "skill_id", "captured_at_utc"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"))
    captured_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    xp: Mapped[int] = mapped_column(BigInteger)
    level: Mapped[int] = mapped_column(Integer)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(Text)
    flag: Mapped[str | None] = mapped_column(Text, nullable=True)


class AdventureLogRaw(Base):
    __tablename__ = "adventure_log_raw"
    __table_args__ = (
        UniqueConstraint("player_id", "entry_key", "occurrence", name="uq_log_entry"),
        Index("ix_log_player_time", "player_id", "entry_time_utc"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    entry_time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    text: Mapped[str] = mapped_column(Text)
    details: Mapped[str] = mapped_column(Text)
    entry_key: Mapped[str] = mapped_column(Text)
    occurrence: Mapped[int] = mapped_column(SmallInteger)
    first_seen_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class AdventureEvent(Base):
    __tablename__ = "adventure_events"
    __table_args__ = (
        UniqueConstraint("raw_id", "event_type", "subject", name="uq_event"),
        Index("ix_events_lookup", "player_id", "event_type", "subject", "event_time_utc"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    raw_id: Mapped[int] = mapped_column(ForeignKey("adventure_log_raw.id", ondelete="CASCADE"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    event_time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_type: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    count: Mapped[int] = mapped_column(Integer, default=1)
    value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    known_subject: Mapped[bool] = mapped_column(default=True)
    parser_version: Mapped[str] = mapped_column(Text)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (Index("ix_runs_source_started", "source", "started_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(Text)  # xp | adventure_log | backup
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text)  # running | ok | partial | failed | skipped
    trigger: Mapped[str] = mapped_column(Text, default="cron")  # cron | manual | cli
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    warning: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class WikiPage(Base):
    """Last fetched rendered HTML of a wiki page; requirements are derived from it and can be rebuilt."""
    __tablename__ = "wiki_pages"
    title: Mapped[str] = mapped_column(Text, primary_key=True)
    revid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    html: Mapped[str] = mapped_column(Text)


class CompRequirement(Base):
    """One completionist cape component. Never deleted: dropped from the wiki page => active = false."""
    __tablename__ = "comp_requirements"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    category: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CompCompletion(Base):
    """A component the user ticked off by hand on /admin/comp."""
    __tablename__ = "comp_completions"
    requirement_key: Mapped[str] = mapped_column(ForeignKey("comp_requirements.key", ondelete="CASCADE"),
                                                 primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
