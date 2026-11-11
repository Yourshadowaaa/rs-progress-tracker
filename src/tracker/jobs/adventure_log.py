"""Hourly adventure log ingestion: raw storage with dedupe, gap detection, event parsing.

Dedupe: entry_key = sha256(time floored to the minute, UTC | text | details). Genuinely identical entries in the same minute
are kept apart by an occurrence number. If a fetch contains n copies of a key and the DB already
holds m, occurrences m+1..n are inserted. Re-fetching the same window therefore inserts nothing,
and two real identical entries are never collapsed into one.

The key uses minute precision because legacy RuneMetrics rows (before 10 Nov 2026) only had minutes;
the Adventurer's Log API adds seconds and a real offset, which are kept in entry_time_utc. Legacy rows
may also be off by whole hours if the old log was not UTC, so a one-time cutover guard skips a new
entry that matches an unmatched legacy row (same text and details, within LEGACY_WINDOW).
"""

import hashlib
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..clients.adventurers_log import Activity, fetch_events
from ..config import Settings
from ..db import get_or_create_player, session_scope
from ..models import AdventureEvent, AdventureLogRaw, IngestionRun
from ..parser.engine import PARSER_VERSION, parse_entry
from .runs import RunResult

log = logging.getLogger(__name__)


# Legacy rows may be shifted by whole hours (log timezone was never confirmed); max offset tolerated.
LEGACY_WINDOW = timedelta(hours=14)
# Successful runs that finished before this were on RuneMetrics, which went offline on 10 Nov 2026.
RUNEMETRICS_SHUTDOWN = datetime(2026, 11, 11, tzinfo=UTC)


def entry_key(a: Activity) -> str:
    minute = a.time_utc.astimezone(UTC).replace(second=0, microsecond=0)
    return hashlib.sha256(f"{minute.isoformat()}|{a.text}|{a.details}".encode()).hexdigest()


def _legacy_matches(s: Session, player_id: int, excess: dict[str, int], keys: dict[str, Activity]) -> Counter:
    """Cutover guard: how many excess fetched copies per key correspond to a legacy RuneMetrics row
    stored under a different key (same text and details, time within LEGACY_WINDOW). Deterministic,
    so re-runs skip the same copies and never insert them later."""
    pending = {k: n for k, n in excess.items() if n > 0}
    if not pending:
        return Counter()
    times = [keys[k].time_utc for k in pending]
    lo, hi = min(times) - LEGACY_WINDOW, max(times) + LEGACY_WINDOW
    legacy = s.execute(
        select(AdventureLogRaw.id, AdventureLogRaw.entry_time_utc, AdventureLogRaw.text, AdventureLogRaw.details,
               AdventureLogRaw.entry_key)
        .where(AdventureLogRaw.player_id == player_id, ~AdventureLogRaw.payload.has_key("type"),
               AdventureLogRaw.entry_time_utc.between(lo, hi))
    ).all()
    if not legacy:
        return Counter()
    cutoff = max(r.entry_time_utc for r in legacy) + LEGACY_WINDOW
    candidates = [r for r in legacy if r.entry_key not in keys]
    pairs = []
    for k in pending:
        a = keys[k]
        if a.time_utc > cutoff:
            continue
        for r in candidates:
            if r.text == a.text and r.details == a.details and abs(r.entry_time_utc - a.time_utc) <= LEGACY_WINDOW:
                pairs.append((abs(r.entry_time_utc - a.time_utc), a.time_utc, k, r.id, r.entry_time_utc))
    matched: Counter = Counter()
    used: set[int] = set()
    for _, t, k, raw_id, legacy_t in sorted(pairs):
        if raw_id in used or matched[k] >= pending[k]:
            continue
        used.add(raw_id)
        matched[k] += 1
        log.info("cutover: %s %r matches legacy row %s at %s; not inserted", t.isoformat(), keys[k].text,
                 raw_id, legacy_t.isoformat())
    return matched


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

        skipped = _legacy_matches(
            s, player_id, {k: n - stored_counts.get(k, 0) for k, n in fetched_counts.items()}, keys)

        values = []
        for key, n in fetched_counts.items():
            a = keys[key]
            for occurrence in range(stored_counts.get(key, 0) + 1, n - skipped[key] + 1):
                values.append(dict(player_id=player_id, entry_time_utc=a.time_utc, text=a.text, details=a.details,
                                   entry_key=key, occurrence=occurrence, first_seen_at_utc=seen_at, payload=a.raw))
        new_ids: list[int] = []
        if values:
            stmt = insert(AdventureLogRaw).values(values).on_conflict_do_nothing(constraint="uq_log_entry")
            new_ids = [row[0] for row in s.execute(stmt.returning(AdventureLogRaw.id)).all()]
        result.rows_inserted += len(new_ids)

        if skipped:
            result.warn(f"cutover: {sum(skipped.values())} fetched entries matched legacy RuneMetrics rows "
                        "under a different timestamp and were not inserted again")
        if had_history and activities and not stored_counts and not skipped:
            message = (f"possible gap: none of the {len(activities)} fetched entries were seen before; "
                       "entries between polls may have been missed")
            last_ok = s.scalar(select(func.max(IngestionRun.finished_at)).where(
                IngestionRun.source == "adventure_log", IngestionRun.status.in_(("ok", "partial"))))
            if last_ok is not None and last_ok < RUNEMETRICS_SHUTDOWN:
                message += (f" (expected once: the last successful poll was {last_ok:%Y-%m-%d %H:%M} UTC, before "
                            "RuneMetrics went offline on 10 Nov 2026; events logged in between were not polled)")
            result.warn(message)
        if new_ids:
            raws = s.scalars(select(AdventureLogRaw).where(AdventureLogRaw.id.in_(new_ids))).all()
            insert_events_for(s, list(raws))
        return new_ids


def run_log_job(engine: Engine, settings: Settings, client: httpx.Client, result: RunResult) -> None:
    store_activities(engine, settings, fetch_events(client, settings.player_name), result)


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
