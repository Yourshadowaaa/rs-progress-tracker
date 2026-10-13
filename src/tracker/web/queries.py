"""Read-only analytics queries for the dashboard. All bucketing happens in the display timezone."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..reference import ALL_SKILLS, MAX_VIRTUAL_LEVEL, OVERALL_ID, XP_TABLE, level_progress

RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30),
          "90d": timedelta(days=90), "1y": timedelta(days=365), "all": None}
BUCKETS = ("hour", "day", "week", "month")
MAX_STACKED_BOSSES = 8  # categorical palette size; the rest folds into "Other"


def range_start(range_key: str, now: datetime | None = None) -> datetime | None:
    delta = RANGES.get(range_key, RANGES["7d"])
    return None if delta is None else (now or datetime.now(UTC)) - delta


def player_id(conn: Connection, name: str) -> int | None:
    return conn.execute(text("SELECT id FROM players WHERE display_name = :n"), {"n": name}).scalar()


def iso_local(ts: datetime, tz: str) -> str:
    return ts.astimezone(ZoneInfo(tz)).isoformat(timespec="minutes")


def last_updated(conn: Connection, pid: int) -> datetime | None:
    return conn.execute(text("SELECT max(captured_at_utc) FROM xp_snapshots WHERE player_id = :p"), {"p": pid}).scalar()


def xp_series(conn: Connection, pid: int, skill_id: int, range_key: str, tz: str) -> list[dict]:
    """Hourly points up to 7 days, otherwise the last snapshot of each local day."""
    start = range_start(range_key)
    params = {"p": pid, "s": skill_id, "start": start, "tz": tz}
    where = "player_id = :p AND skill_id = :s AND (CAST(:start AS timestamptz) IS NULL OR captured_at_utc >= :start)"
    if range_key in ("24h", "7d"):
        sql = f"SELECT captured_at_utc, xp, level FROM xp_snapshots WHERE {where} ORDER BY captured_at_utc"
    else:
        sql = f"""SELECT DISTINCT ON (date_trunc('day', captured_at_utc AT TIME ZONE :tz))
                         captured_at_utc, xp, level
                  FROM xp_snapshots WHERE {where}
                  ORDER BY date_trunc('day', captured_at_utc AT TIME ZONE :tz), captured_at_utc DESC"""
    return [{"t": iso_local(t, tz), "xp": xp, "level": lvl} for t, xp, lvl in conn.execute(text(sql), params)]


def xp_gains(conn: Connection, pid: int, skill_id: int, range_key: str, bucket: str, tz: str) -> list[dict]:
    """XP gained per bucket = sum of consecutive-snapshot deltas landing in the bucket."""
    bucket = bucket if bucket in BUCKETS else "day"
    if bucket == "hour" and range_key not in ("24h", "7d", "30d"):
        bucket = "day"  # keep responses small: hourly bars only up to 30 days
    rows = conn.execute(text("""
        SELECT date_trunc(:b, captured_at_utc AT TIME ZONE :tz) AS bucket, COALESCE(SUM(delta), 0)
        FROM xp_deltas
        WHERE player_id = :p AND skill_id = :s AND delta IS NOT NULL
          AND (CAST(:start AS timestamptz) IS NULL OR captured_at_utc >= :start)
        GROUP BY 1 ORDER BY 1"""), {"b": bucket, "tz": tz, "p": pid, "s": skill_id, "start": range_start(range_key)})
    out, window = [], []
    for b, gain in rows:
        window = (window + [int(gain)])[-7:]
        out.append({"bucket": b.date().isoformat() if bucket != "hour" else b.isoformat(timespec="minutes"),
                    "xp": int(gain), "ma7": round(sum(window) / len(window))})
    return out


def gains_by_skill(conn: Connection, pid: int, range_key: str) -> list[dict]:
    """Gain per skill over the range = latest XP - last XP at or before the range start
    (or the first snapshot when the range predates tracking)."""
    start = range_start(range_key)
    rows = conn.execute(text("""
        WITH latest AS (
            SELECT DISTINCT ON (skill_id) skill_id, xp FROM xp_snapshots
            WHERE player_id = :p ORDER BY skill_id, captured_at_utc DESC),
        base AS (
            SELECT DISTINCT ON (skill_id) skill_id, xp FROM xp_snapshots
            WHERE player_id = :p AND (CAST(:start AS timestamptz) IS NULL OR captured_at_utc <= :start)
            ORDER BY skill_id, captured_at_utc DESC),
        first AS (
            SELECT DISTINCT ON (skill_id) skill_id, xp FROM xp_snapshots
            WHERE player_id = :p ORDER BY skill_id, captured_at_utc ASC)
        SELECT l.skill_id, l.xp - COALESCE(b.xp, f.xp) AS gained
        FROM latest l JOIN first f USING (skill_id) LEFT JOIN base b USING (skill_id)
        ORDER BY gained DESC, l.skill_id"""), {"p": pid, "start": start})
    return [{"skill_id": sid, "skill": ALL_SKILLS[sid], "xp": int(g)} for sid, g in rows]


def daily_gain_stats(conn: Connection, pid: int, skill_id: int, tz: str) -> dict:
    days = xp_gains(conn, pid, skill_id, "all", "day", tz)
    best = max(days, key=lambda d: d["xp"], default=None)
    streak = 0
    today = datetime.now(ZoneInfo(tz)).date()
    by_day = {d["bucket"]: d["xp"] for d in days}
    cursor = today if by_day.get(today.isoformat(), 0) > 0 else today - timedelta(days=1)
    while by_day.get(cursor.isoformat(), 0) > 0:
        streak += 1
        cursor -= timedelta(days=1)
    return {"best_day": best if best and best["xp"] > 0 else None, "streak_days": streak}


def rate_per_hour(conn: Connection, pid: int, skill_id: int, window: timedelta) -> float | None:
    row = conn.execute(text("""
        WITH pts AS (SELECT captured_at_utc t, xp FROM xp_snapshots
                     WHERE player_id = :p AND skill_id = :s AND captured_at_utc >= now() - CAST(:w AS interval))
        SELECT (SELECT xp FROM pts ORDER BY t DESC LIMIT 1) - (SELECT xp FROM pts ORDER BY t ASC LIMIT 1),
               EXTRACT(EPOCH FROM max(t) - min(t)) / 3600 FROM pts"""),
        {"p": pid, "s": skill_id, "w": f"{int(window.total_seconds())} seconds"}).one()
    gained, hours = row
    if gained is None or not hours:
        return None
    return float(gained) / float(hours)


def projection_rates(conn: Connection, pid: int) -> dict[int, float]:
    """XP/hour per skill over the trailing 7 days, falling back to 30 days when 7 days is flat."""
    rows = conn.execute(text("""
        WITH w AS (SELECT skill_id, xp, captured_at_utc t, captured_at_utc >= now() - interval '7 days' AS recent
                   FROM xp_snapshots WHERE player_id = :p AND captured_at_utc >= now() - interval '30 days')
        SELECT skill_id,
               (max(xp) FILTER (WHERE recent) - min(xp) FILTER (WHERE recent))
                 / NULLIF(EXTRACT(EPOCH FROM max(t) FILTER (WHERE recent) - min(t) FILTER (WHERE recent)) / 3600, 0),
               (max(xp) - min(xp)) / NULLIF(EXTRACT(EPOCH FROM max(t) - min(t)) / 3600, 0)
        FROM w GROUP BY skill_id"""), {"p": pid})
    rates = {}
    for sid, r7, r30 in rows:
        rate = float(r7) if r7 else (float(r30) if r30 else None)
        if rate:
            rates[sid] = rate
    return rates


@dataclass
class LevelRow:
    skill_id: int
    skill: str
    level: int
    virtual_level: int | None
    xp: int
    rank: int | None
    next_level: int | None
    xp_to_next: int | None
    progress_pct: float | None
    gained_7d: int
    eta_next: str | None
    eta_99: str | None
    eta_120: str | None


def _eta(xp_needed: int | None, per_hour: float | None) -> str | None:
    if xp_needed is None or xp_needed <= 0 or not per_hour or per_hour <= 0:
        return None
    return (datetime.now(UTC) + timedelta(hours=xp_needed / per_hour)).date().isoformat()


def levels_table(conn: Connection, pid: int) -> list[LevelRow]:
    latest = conn.execute(text("""
        SELECT DISTINCT ON (skill_id) skill_id, level, xp, rank FROM xp_snapshots
        WHERE player_id = :p ORDER BY skill_id, captured_at_utc DESC"""), {"p": pid}).all()
    gains = {g["skill_id"]: g["xp"] for g in gains_by_skill(conn, pid, "7d")}
    rates = projection_rates(conn, pid)
    rows = []
    for sid, level, xp, rank in latest:
        prog = level_progress(sid, xp)
        rate = rates.get(sid)
        rows.append(LevelRow(
            skill_id=sid, skill=ALL_SKILLS[sid], level=level, virtual_level=prog.level if prog else None, xp=xp,
            rank=rank, next_level=prog.next_level if prog else None, xp_to_next=prog.xp_to_next if prog else None,
            progress_pct=prog.progress_pct if prog else None, gained_7d=gains.get(sid, 0),
            eta_next=_eta(prog.xp_to_next, rate) if prog else None,
            eta_99=_eta(XP_TABLE[99] - xp, rate) if prog else None,
            eta_120=_eta(XP_TABLE[MAX_VIRTUAL_LEVEL] - xp, rate) if prog else None,
        ))
    rows.sort(key=lambda r: (r.skill_id != OVERALL_ID, r.skill_id))
    return rows


def boss_order(conn: Connection, pid: int) -> list[str]:
    """Bosses ordered by all-time kills. Used to give each boss a fixed colour slot that does
    not change with the selected range or filters."""
    return [r[0] for r in conn.execute(text("""
        SELECT subject FROM adventure_events WHERE player_id = :p AND event_type = 'boss_kill'
        GROUP BY subject ORDER BY SUM(count) DESC, subject"""), {"p": pid})]


def boss_kills(conn: Connection, pid: int, range_key: str, bucket: str, tz: str, boss: str | None) -> dict:
    bucket = bucket if bucket in ("day", "week", "month") else "day"
    rows = conn.execute(text("""
        SELECT date_trunc(:b, event_time_utc AT TIME ZONE :tz) AS bucket, subject, SUM(count)
        FROM adventure_events
        WHERE player_id = :p AND event_type = 'boss_kill'
          AND (CAST(:start AS timestamptz) IS NULL OR event_time_utc >= :start)
          AND (CAST(:boss AS text) IS NULL OR subject = :boss)
        GROUP BY 1, 2 ORDER BY 1, 2"""),
        {"b": bucket, "tz": tz, "p": pid, "start": range_start(range_key), "boss": boss})
    order = boss_order(conn, pid)
    named = order[:MAX_STACKED_BOSSES] if len(order) <= MAX_STACKED_BOSSES else order[:MAX_STACKED_BOSSES - 1]
    buckets: dict[str, dict[str, int]] = {}
    for b, subject, kills in rows:
        series = subject if subject in named else "Other"
        day = b.date().isoformat()
        buckets.setdefault(day, {})
        buckets[day][series] = buckets[day].get(series, 0) + int(kills)
    present = {s for v in buckets.values() for s in v}
    series = [s for s in named if s in present] + (["Other"] if "Other" in present else [])
    return {
        "buckets": sorted(buckets),
        "series": [{"name": s, "slot": (named.index(s) if s in named else MAX_STACKED_BOSSES - 1),
                    "data": [buckets[b].get(s, 0) for b in sorted(buckets)]} for s in series],
        "all_bosses": order,
    }


def boss_totals(conn: Connection, pid: int, range_key: str) -> list[dict]:
    return [{"boss": s, "kills": int(k), "known": bool(known)} for s, k, known in conn.execute(text("""
        SELECT subject, SUM(count), bool_and(known_subject) FROM adventure_events
        WHERE player_id = :p AND event_type = 'boss_kill'
          AND (CAST(:start AS timestamptz) IS NULL OR event_time_utc >= :start)
        GROUP BY subject ORDER BY 2 DESC, 1"""), {"p": pid, "start": range_start(range_key)})]


def summary_cards(conn: Connection, pid: int, tz: str) -> dict:
    row = conn.execute(text("""
        WITH bounds AS (SELECT (date_trunc('week', now() AT TIME ZONE :tz) AT TIME ZONE :tz) AS week_start,
                               (date_trunc('month', now() AT TIME ZONE :tz) AT TIME ZONE :tz) AS month_start)
        SELECT
          (SELECT COALESCE(SUM(count), 0) FROM adventure_events, bounds
             WHERE player_id = :p AND event_type = 'boss_kill' AND event_time_utc >= week_start),
          (SELECT subject FROM adventure_events, bounds
             WHERE player_id = :p AND event_type = 'boss_kill' AND event_time_utc >= week_start
             GROUP BY subject ORDER BY SUM(count) DESC, subject LIMIT 1),
          (SELECT COUNT(*) FROM adventure_events, bounds
             WHERE player_id = :p AND event_type = 'level_up' AND event_time_utc >= month_start),
          (SELECT COUNT(*) FROM adventure_events WHERE player_id = :p AND event_type = 'unparsed')
        """), {"p": pid, "tz": tz}).one()
    return {"kills_this_week": int(row[0]), "top_boss_this_week": row[1],
            "level_ups_this_month": int(row[2]), "unparsed_total": int(row[3])}


def events(conn: Connection, pid: int, tz: str, *, event_type: str | None = None, subject: str | None = None,
           start: datetime | None = None, end: datetime | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    rows = conn.execute(text("""
        SELECT e.id, e.event_time_utc, e.event_type, e.subject, e.count, e.value, e.known_subject,
               e.parser_version, r.id, r.text, r.details, r.first_seen_at_utc
        FROM adventure_events e JOIN adventure_log_raw r ON r.id = e.raw_id
        WHERE e.player_id = :p
          AND (CAST(:type AS text) IS NULL OR e.event_type = :type)
          AND (CAST(:subject AS text) IS NULL OR e.subject = :subject)
          AND (CAST(:start AS timestamptz) IS NULL OR e.event_time_utc >= :start)
          AND (CAST(:end AS timestamptz) IS NULL OR e.event_time_utc < :end)
        ORDER BY e.event_time_utc DESC, r.id DESC LIMIT :limit OFFSET :offset"""),
        {"p": pid, "type": event_type, "subject": subject, "start": start, "end": end,
         "limit": min(max(limit, 1), 5000), "offset": max(offset, 0)})
    return [{"id": r[0], "time": iso_local(r[1], tz), "event_type": r[2], "subject": r[3], "count": r[4],
             "value": r[5], "known_subject": r[6], "parser_version": r[7], "raw_id": r[8], "text": r[9],
             "details": r[10], "first_seen": iso_local(r[11], tz)} for r in rows]


def event_types(conn: Connection, pid: int) -> list[tuple[str, int]]:
    return [(t, int(n)) for t, n in conn.execute(text("""
        SELECT event_type, COUNT(*) FROM adventure_events WHERE player_id = :p GROUP BY 1 ORDER BY 2 DESC"""),
        {"p": pid})]


def unparsed_report(conn: Connection, pid: int, tz: str) -> dict:
    unparsed = conn.execute(text("""
        SELECT r.text, MIN(r.details), COUNT(*), MAX(r.entry_time_utc)
        FROM adventure_events e JOIN adventure_log_raw r ON r.id = e.raw_id
        WHERE e.player_id = :p AND e.event_type = 'unparsed'
        GROUP BY r.text ORDER BY 3 DESC, 4 DESC"""), {"p": pid}).all()
    unknown = conn.execute(text("""
        SELECT event_type, subject, COUNT(*), SUM(count) FROM adventure_events
        WHERE player_id = :p AND NOT known_subject AND event_type <> 'unparsed'
        GROUP BY 1, 2 ORDER BY 3 DESC"""), {"p": pid}).all()
    total = conn.execute(text("SELECT COUNT(*) FROM adventure_events WHERE player_id = :p"), {"p": pid}).scalar()
    n_unparsed = sum(r[2] for r in unparsed)
    return {
        "unparsed": [{"text": t, "details": d, "occurrences": n, "last_seen": iso_local(ts, tz)} for t, d, n, ts in unparsed],
        "unknown_subjects": [{"event_type": et, "subject": s, "entries": n, "total_count": int(c)} for et, s, n, c in unknown],
        "total_events": int(total or 0),
        "unparsed_rate": round(n_unparsed / total, 4) if total else 0.0,
    }


def health(conn: Connection, tz: str) -> dict:
    jobs = {}
    for source in ("xp", "adventure_log", "backup"):
        last = conn.execute(text("""
            SELECT started_at, finished_at, status, rows_inserted, warning, error FROM ingestion_runs
            WHERE source = :s ORDER BY started_at DESC LIMIT 1"""), {"s": source}).first()
        last_ok = conn.execute(text("""
            SELECT max(finished_at) FROM ingestion_runs WHERE source = :s AND status IN ('ok', 'partial')"""),
            {"s": source}).scalar()
        if last is None and source == "backup":
            continue
        age_h = (datetime.now(UTC) - last_ok).total_seconds() / 3600 if last_ok else None
        jobs[source] = {
            "last_success": iso_local(last_ok, tz) if last_ok else None,
            "hours_since_success": round(age_h, 2) if age_h is not None else None,
            "stale": age_h is None or age_h > (26 if source == "backup" else 2),
            "failing": last is not None and last[2] == "failed",
            "last_run": None if last is None else {
                "started": iso_local(last[0], tz), "finished": iso_local(last[1], tz) if last[1] else None,
                "status": last[2], "rows": last[3], "warning": last[4], "error": last[5],
            },
        }
    profile_private = bool(conn.execute(text("""
        SELECT error LIKE '%PROFILE_PRIVATE%' FROM ingestion_runs WHERE source = 'adventure_log'
        ORDER BY started_at DESC LIMIT 1""")).scalar())
    return {"jobs": jobs, "profile_private": profile_private,
            "status": "ok" if jobs and not any(j["stale"] or j["failing"] for j in jobs.values()) else "degraded"}


def run_history(conn: Connection, tz: str, limit: int = 100) -> list[dict]:
    return [{"source": r[0], "started": iso_local(r[1], tz), "finished": iso_local(r[2], tz) if r[2] else None,
             "status": r[3], "rows": r[4], "warning": r[5], "error": r[6], "trigger": r[7]}
            for r in conn.execute(text("""
                SELECT source, started_at, finished_at, status, rows_inserted, warning, error, trigger
                FROM ingestion_runs ORDER BY started_at DESC, id DESC LIMIT :l"""), {"l": limit})]
