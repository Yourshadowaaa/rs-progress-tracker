"""Runs the ingestion jobs. Used by the hourly cron (CLI) and the dashboard's force-update button."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, text

from ..clients.http import make_client
from ..config import get_settings
from .adventure_log import run_log_job
from .backup import backup_configured, run_backup
from .runs import log, record_skipped, recent_successful_run, tracked_run
from .xp_snapshot import run_xp_job

# Postgres advisory lock key: only one ingestion run at a time across the cron and web services.
INGEST_LOCK_KEY = 0x5253_7472  # "RStr"
SOURCES = ("xp", "adventure_log")


def run_all(engine: Engine, force: bool = False, trigger: str = "cron") -> bool:
    """Run XP + adventure log (+ the daily backup on scheduled runs). Returns False if another run
    held the lock. trigger='manual' (force update) skips the min-interval guard and refreshes the
    current hour's XP snapshot instead of keeping the earlier one."""
    settings = get_settings()
    manual = trigger == "manual"
    with engine.connect() as lock_conn:
        if not lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": INGEST_LOCK_KEY}).scalar():
            log.warning("another ingestion run is in progress; skipping (%s)", trigger)
            if manual:
                for source in SOURCES:
                    record_skipped(engine, source, trigger, "another ingestion run was in progress")
            return False
        try:
            _run_jobs(engine, settings, force or manual, trigger)
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": INGEST_LOCK_KEY})
            lock_conn.commit()
    return True


def _run_jobs(engine: Engine, settings, force: bool, trigger: str) -> None:
    window = timedelta(minutes=settings.min_run_interval_min)
    overwrite = trigger == "manual"
    with make_client() as client:
        for source in SOURCES:
            if not force and recent_successful_run(engine, source, window):
                log.info("skipping %s: successful run within the last %s", source, window)
                continue
            with tracked_run(engine, source, trigger) as result:
                if source == "xp":
                    run_xp_job(engine, settings, client, result, overwrite=overwrite)
                else:
                    run_log_job(engine, settings, client, result)
    if trigger == "manual":
        return
    now = datetime.now(UTC)
    if backup_configured(settings) and now.hour == settings.backup_hour_utc:
        if force or not recent_successful_run(engine, "backup", timedelta(hours=20)):
            with tracked_run(engine, "backup", trigger) as result:
                run_backup(settings, result)
