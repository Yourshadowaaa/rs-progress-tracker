"""Hourly adventure log ingestion: raw storage with dedupe, gap detection, event parsing.

Dedupe: entry_key = sha256(time | text | details). Genuinely identical entries in the same minute
are kept apart by an occurrence number. If a fetch contains n copies of a key and the DB already
holds m, occurrences m+1..n are inserted. Re-fetching the same window therefore inserts nothing,
and two real identical entries are never collapsed into one.
"""

import hashlib
import logging
from collections import Counter
from datetime import UTC, datetime

import httpx
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..clients.runemetrics import Activity, fetch_profile
from ..config import Settings
from ..db import get_or_create_player, session_scope
from ..models import AdventureEvent, AdventureLogRaw
from ..parser.engine import PARSER_VERSION, parse_entry
from .runs import RunResult

log = logging.getLogger(__name__)


def entry_key(a: Activity) -> str:
    return hashlib.sha256(f"{a.time_utc.isoformat()}|{a.text}|{a.details}".encode()).hexdigest()


def insert_events_for(session: Session, raw_rows: list[AdventureLogRaw]) -> int:
    values = []
    for raw in raw_rows:
        for ev in parse_entry(raw.text, raw.details):
            values.append(dict(raw_id=raw.id, player_id=raw.player_id, event_time_utc=raw.entry_time_utc,
                               event_type=ev.event_type, subject=ev.subject, count=ev.count, value=ev.value,
                               known_subject=ev.known_subject, parser_version=PARSER_VERSION))
    if not values:
        return 0
    stmt = insert(AdventureEvent).values(values).on_conflict_do_nothing(constraint="uq_event")
    return len(session.execute(stmt.returning(AdventureEvent.id)).all())


def store_activities(engine: Engine, settings: Settings, activities: list[Activity], result: RunResult,
                     now: datetime | None = None) -> list[int]:
    seen_at = now or datetime.now(UTC)
    with session_scope(engine) as s:
        player_id = get_or_create_player(s, settings.player_name)
        keys = {entry_key(a): a for a in activities}
        fetched_counts = Counter(entry_key(a) for a in activities)
        stored_counts = dict(
            s.execute(
                select(AdventureLogRaw.entry_key, func.count())
                .where(AdventureLogRaw.player_id == player_id, AdventureLogRaw.entry_key.in_(list(keys)))
                .group_by(AdventureLogRaw.entry_key)
            ).all()
        ) if keys else {}
        had_history = s.scalar(
            select(AdventureLogRaw.id).where(AdventureLogRaw.player_id == player_id).limit(1)
        ) is not None

        values = []
        for key, n in fetched_counts.items():
            a = keys[key]
            for occurrence in range(stored_counts.get(key, 0) + 1, n + 1):
                values.append(dict(player_id=player_id, entry_time_utc=a.time_utc, text=a.text, details=a.details,
                                   entry_key=key, occurrence=occurrence, first_seen_at_utc=seen_at, payload=a.raw))
        new_ids: list[int] = []
        if values:
            stmt = insert(AdventureLogRaw).values(values).on_conflict_do_nothing(constraint="uq_log_entry")
            new_ids = [row[0] for row in s.execute(stmt.returning(AdventureLogRaw.id)).all()]
        result.rows_inserted += len(new_ids)

        if had_history and activities and not stored_counts:
            result.warn(f"possible gap: none of the {len(activities)} fetched entries were seen before; "
                        "entries between polls may have been missed")
        if new_ids:
            raws = s.scalars(select(AdventureLogRaw).where(AdventureLogRaw.id.in_(new_ids))).all()
            insert_events_for(s, list(raws))
        return new_ids


def run_log_job(engine: Engine, settings: Settings, client: httpx.Client, result: RunResult) -> None:
    profile = fetch_profile(client, settings.player_name)
    store_activities(engine, settings, profile.activities, result)


def reparse(engine: Engine, everything: bool = False, batch: int = 1000) -> tuple[int, int]:
    """Rebuild events from raw entries whose events were produced by another parser version
    (or all of them). Delete-then-insert per raw entry in one transaction, so it is deterministic
    and never duplicates. Returns (raw entries reparsed, events written)."""
    with session_scope(engine) as s:
        q = select(AdventureLogRaw)
        if not everything:
            stale = select(AdventureEvent.raw_id).where(AdventureEvent.parser_version != PARSER_VERSION)
            missing = select(AdventureLogRaw.id).where(
                ~select(AdventureEvent.id).where(AdventureEvent.raw_id == AdventureLogRaw.id).exists()
            )
            q = q.where(AdventureLogRaw.id.in_(stale.union(missing)))
        raws = list(s.scalars(q.order_by(AdventureLogRaw.id)).all())
        written = 0
        for i in range(0, len(raws), batch):
            chunk = raws[i:i + batch]
            s.execute(delete(AdventureEvent).where(AdventureEvent.raw_id.in_([r.id for r in chunk])))
            written += insert_events_for(s, chunk)
        return len(raws), written
