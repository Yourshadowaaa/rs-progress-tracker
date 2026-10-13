"""Job entrypoint. Railway cron runs `python -m tracker.jobs run-all` every hour and the process exits.

Commands:
  run-all            XP snapshot + adventure log (+ daily backup at BACKUP_HOUR_UTC, if configured)
  xp | log | backup  run a single job
  reparse [--all]    rebuild adventure_events from adventure_log_raw
  probe [--out DIR]  verify endpoints and save raw responses
  export-corpus      print stored distinct log entries as JSONL (to grow the parser test corpus)
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, select

from ..clients.http import make_client
from ..config import get_settings
from ..db import get_engine, session_scope
from ..models import AdventureLogRaw
from .adventure_log import reparse, run_log_job
from .backup import backup_configured, run_backup
from .runs import configure_logging, log, recent_successful_run, tracked_run
from .xp_snapshot import run_xp_job


def check_schema(engine: Engine) -> None:
    """Fail loudly if the database is behind the code's migrations (web pre-deploy runs them)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    if current != head:
        raise SystemExit(f"database schema at {current}, code expects {head}: run `alembic upgrade head`")


def run_all(engine: Engine, force: bool = False) -> None:
    settings = get_settings()
    window = timedelta(minutes=settings.min_run_interval_min)
    with make_client() as client:
        for source, job in (("xp", run_xp_job), ("adventure_log", run_log_job)):
            if not force and recent_successful_run(engine, source, window):
                log.info("skipping %s: successful run within the last %s", source, window)
                continue
            with tracked_run(engine, source) as result:
                job(engine, settings, client, result)
    now = datetime.now(UTC)
    if backup_configured(settings) and now.hour == settings.backup_hour_utc:
        if force or not recent_successful_run(engine, "backup", timedelta(hours=20)):
            with tracked_run(engine, "backup") as result:
                run_backup(settings, result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tracker.jobs")
    parser.add_argument("command", choices=["run-all", "xp", "log", "backup", "reparse", "probe", "export-corpus"])
    parser.add_argument("--all", action="store_true", help="reparse: rebuild every event")
    parser.add_argument("--force", action="store_true", help="ignore the min-interval double-fire guard")
    parser.add_argument("--out", default="probe_output")
    args = parser.parse_args(argv)
    configure_logging()

    if args.command == "probe":
        from .probe import run_probe
        run_probe(args.out)
        return 0

    settings = get_settings()
    engine = get_engine()
    check_schema(engine)
    if args.command == "run-all":
        run_all(engine, force=args.force)
    elif args.command in ("xp", "log"):
        source, job = ("xp", run_xp_job) if args.command == "xp" else ("adventure_log", run_log_job)
        with make_client() as client, tracked_run(engine, source) as result:
            job(engine, settings, client, result)
    elif args.command == "backup":
        with tracked_run(engine, "backup") as result:
            run_backup(settings, result)
    elif args.command == "reparse":
        entries, events = reparse(engine, everything=args.all)
        log.info("reparsed %d raw entries into %d events", entries, events)
    elif args.command == "export-corpus":
        with session_scope(engine) as s:
            for text, details in s.execute(
                select(AdventureLogRaw.text, AdventureLogRaw.details).distinct().order_by(AdventureLogRaw.text)
            ):
                sys.stdout.write(json.dumps({"text": text, "details": details, "source": "live"}) + "\n")
    # Job failures are recorded in ingestion_runs; exit 0 so cron never retry-loops.
    return 0


if __name__ == "__main__":
    sys.exit(main())
