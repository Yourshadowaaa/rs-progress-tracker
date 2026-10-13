"""Public, read-only dashboard + JSON API. Only GET routes exist."""

import csv
import html
import io
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError

from ..config import get_settings
from ..db import get_engine
from ..reference import ALL_SKILLS, OVERALL_ID
from . import queries as q

HERE = Path(__file__).parent
app = FastAPI(title="RS3 Progress Tracker", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
log = logging.getLogger("tracker.web")


def _db_target() -> str:
    """Host/port/database of DATABASE_URL, without credentials, for diagnostics."""
    try:
        u = make_url(get_settings().database_url)
        return f"{u.host or '?'}:{u.port or 5432}/{u.database or '?'}"
    except Exception:  # noqa: BLE001
        return "unparseable DATABASE_URL"


def diagnose(exc: DBAPIError) -> tuple[str, str]:
    """Map a database exception to (code, human hint). Never includes credentials."""
    msg = str(exc.orig) if exc.orig is not None else str(exc)
    if isinstance(exc, ProgrammingError) and "does not exist" in msg:
        return ("schema_missing",
                "The database is reachable but its tables do not exist yet. Redeploy the web service "
                "(it runs `alembic upgrade head` on start) or run `alembic upgrade head` in its shell.")
    if isinstance(exc, OperationalError):
        fix = ("On Railway, add the variable DATABASE_URL = ${{Postgres.DATABASE_URL}} to this service "
               "(and to the ingest service), then redeploy.")
        if "DATABASE_URL" not in os.environ:
            return ("database_url_missing", "The DATABASE_URL variable is not set. " + fix)
        return ("database_unreachable", f"Cannot connect to the database at {_db_target()}. " + fix)
    return ("database_error", f"Database error: {type(exc.orig or exc).__name__}. See the service logs.")


@app.exception_handler(DBAPIError)
def database_error(request: Request, exc: DBAPIError):
    code, hint = diagnose(exc)
    log.error("database error on %s: %s (%s)", request.url.path, code, exc.orig if exc.orig is not None else exc)
    if request.url.path.startswith(("/api", "/health")):
        return JSONResponse({"status": "error", "error": code, "hint": hint}, status_code=503)
    body = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Tracker unavailable</title><link rel=stylesheet href=/static/style.css>"
        f"<main><h1>Tracker unavailable</h1><div class=panel><p><span class=tag>{code}</span></p>"
        f"<p>{html.escape(hint)}</p></div></main>"
    )
    return HTMLResponse(body, status_code=503)


def get_conn() -> Iterator[Connection]:
    with get_engine().connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        yield conn


def ctx() -> tuple[str, str]:
    s = get_settings()
    return s.player_name, s.display_tz


def pid_or_none(conn: Connection) -> int | None:
    return q.player_id(conn, ctx()[0])


def require_pid(conn: Connection) -> int:
    pid = pid_or_none(conn)
    if pid is None:
        raise HTTPException(404, "no data yet: the first ingestion run has not completed")
    return pid


def skill_param(skill: int) -> int:
    if skill not in ALL_SKILLS:
        raise HTTPException(400, "unknown skill id")
    return skill


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"bad date {value!r}") from exc
    return d if d.tzinfo else d.replace(tzinfo=ZoneInfo(ctx()[1]))


def page(request: Request, name: str, conn: Connection, **extra) -> HTMLResponse:
    player, tz = ctx()
    pid = pid_or_none(conn)
    updated = q.last_updated(conn, pid) if pid else None
    health = q.health(conn, tz)
    return templates.TemplateResponse(request, name, {
        "player": player, "tz": tz, "hiscore_table": get_settings().hiscore_table, "has_data": pid is not None,
        "last_updated": q.iso_local(updated, tz) if updated else None,
        "health_status": health["status"], "profile_private": health["profile_private"],
        "skills": ALL_SKILLS, "ranges": list(q.RANGES), **extra,
    })


# ---------------------------------------------------------------- health


@app.get("/livez")
def livez() -> dict:
    """Liveness only (no database): used as Railway's deploy healthcheck so a database problem
    is reported on the site instead of silently blocking the deploy."""
    return {"status": "alive"}


@app.get("/health")
def health(conn: Connection = Depends(get_conn)) -> JSONResponse:
    # Always 200 while the app and database are reachable, so Railway's deploy healthcheck
    # passes before the first ingestion run; job freshness is reported in the body.
    player, tz = ctx()
    return JSONResponse({"player": player, **q.health(conn, tz)})


# ---------------------------------------------------------------- JSON API


@app.get("/api/levels")
def api_levels(conn: Connection = Depends(get_conn)):
    return [r.__dict__ for r in q.levels_table(conn, require_pid(conn))]


@app.get("/api/xp/series")
def api_xp_series(skill: int = OVERALL_ID, range: str = "7d", conn: Connection = Depends(get_conn)):
    return q.xp_series(conn, require_pid(conn), skill_param(skill), range, ctx()[1])


@app.get("/api/xp/gains")
def api_xp_gains(skill: int = OVERALL_ID, range: str = "30d", bucket: str = "day",
                 conn: Connection = Depends(get_conn)):
    return q.xp_gains(conn, require_pid(conn), skill_param(skill), range, bucket, ctx()[1])


@app.get("/api/xp/by-skill")
def api_xp_by_skill(range: str = "7d", conn: Connection = Depends(get_conn)):
    return [g for g in q.gains_by_skill(conn, require_pid(conn), range) if g["skill_id"] != OVERALL_ID]


@app.get("/api/xp/stats")
def api_xp_stats(skill: int = OVERALL_ID, range: str = "7d", conn: Connection = Depends(get_conn)):
    pid, tz = require_pid(conn), ctx()[1]
    skill = skill_param(skill)
    gained = next((g["xp"] for g in q.gains_by_skill(conn, pid, range) if g["skill_id"] == skill), 0)
    window = q.RANGES.get(range) or (datetime.now(UTC) - datetime(2000, 1, 1, tzinfo=UTC))
    return {"gained": gained, "xp_per_hour": q.rate_per_hour(conn, pid, skill, window),
            **q.daily_gain_stats(conn, pid, skill, tz)}


@app.get("/api/bosses/kills")
def api_boss_kills(range: str = "30d", bucket: str = "day", boss: str | None = None,
                   conn: Connection = Depends(get_conn)):
    return q.boss_kills(conn, require_pid(conn), range, bucket, ctx()[1], boss or None)


@app.get("/api/bosses/totals")
def api_boss_totals(range: str = "all", conn: Connection = Depends(get_conn)):
    return q.boss_totals(conn, require_pid(conn), range)


@app.get("/api/events")
def api_events(type: str | None = None, subject: str | None = None, start: str | None = None,
               end: str | None = None, limit: int = Query(100, le=5000), offset: int = 0,
               conn: Connection = Depends(get_conn)):
    return q.events(conn, require_pid(conn), ctx()[1], event_type=type or None, subject=subject or None,
                    start=parse_date(start), end=parse_date(end), limit=limit, offset=offset)


@app.get("/api/summary")
def api_summary(conn: Connection = Depends(get_conn)):
    return q.summary_cards(conn, require_pid(conn), ctx()[1])


def csv_response(filename: str, header: list[str], rows) -> StreamingResponse:
    def chunks() -> Iterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        for i, row in enumerate(rows, 1):
            writer.writerow(row)
            if i % 5000 == 0:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    return StreamingResponse(chunks(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/export/xp.csv")
def export_xp(skill: int | None = None, range: str = "all", conn: Connection = Depends(get_conn)):
    pid = require_pid(conn)
    rows = conn.execute(text("""
        SELECT x.captured_at_utc, s.name, x.xp, x.level, x.rank, x.source, x.flag
        FROM xp_snapshots x JOIN skills s ON s.id = x.skill_id
        WHERE x.player_id = :p AND (CAST(:s AS smallint) IS NULL OR x.skill_id = :s)
          AND (CAST(:start AS timestamptz) IS NULL OR x.captured_at_utc >= :start)
        ORDER BY x.captured_at_utc, x.skill_id"""),
        {"p": pid, "s": skill, "start": q.range_start(range)})
    data = rows.all()
    return csv_response("xp_snapshots.csv", ["captured_at_utc", "skill", "xp", "level", "rank", "source", "flag"],
                        ((r[0].isoformat(), *r[1:]) for r in data))


@app.get("/api/export/events.csv")
def export_events(type: str | None = None, conn: Connection = Depends(get_conn)):
    pid = require_pid(conn)
    rows = q.events(conn, pid, "UTC", event_type=type or None, limit=5000)
    cols = ["time", "event_type", "subject", "count", "value", "text", "details", "parser_version"]
    return csv_response("adventure_events.csv", cols, ([r[c] for c in cols] for r in rows))


# ---------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
def overview(request: Request, conn: Connection = Depends(get_conn)):
    pid = pid_or_none(conn)
    levels = q.levels_table(conn, pid) if pid else []
    cards = q.summary_cards(conn, pid, ctx()[1]) if pid else None
    return page(request, "overview.html", conn, levels=levels, cards=cards, active="overview")


@app.get("/xp", response_class=HTMLResponse)
def xp_page(request: Request, conn: Connection = Depends(get_conn)):
    return page(request, "xp.html", conn, active="xp")


@app.get("/bosses", response_class=HTMLResponse)
def bosses_page(request: Request, conn: Connection = Depends(get_conn)):
    return page(request, "bosses.html", conn, active="bosses")


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request, conn: Connection = Depends(get_conn)):
    pid = pid_or_none(conn)
    return page(request, "events.html", conn, active="events", event_types=q.event_types(conn, pid) if pid else [])


@app.get("/admin/unparsed", response_class=HTMLResponse)
def unparsed_page(request: Request, conn: Connection = Depends(get_conn)):
    pid = pid_or_none(conn)
    report = q.unparsed_report(conn, pid, ctx()[1]) if pid else None
    return page(request, "unparsed.html", conn, active="unparsed", report=report)


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request, conn: Connection = Depends(get_conn)):
    tz = ctx()[1]
    return page(request, "status.html", conn, active="status", health=q.health(conn, tz), runs=q.run_history(conn, tz))
