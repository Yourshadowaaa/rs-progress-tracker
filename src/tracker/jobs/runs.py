"""ingestion_runs bookkeeping. A run row is inserted as 'running' and always finalised."""

import json
import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Iterator

from sqlalchemy import Engine, select, update

from ..db import session_scope
from ..models import IngestionRun

log = logging.getLogger("tracker.jobs")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "extra_fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


@dataclass
class RunResult:
    status: str = "ok"
    rows_inserted: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        if self.status == "ok":
            self.status = "partial"


def recent_successful_run(engine: Engine, source: str, within: timedelta) -> bool:
    with session_scope(engine) as s:
        cutoff = datetime.now(UTC) - within
        return s.scalar(
            select(IngestionRun.id).where(
                IngestionRun.source == source,
                IngestionRun.status.in_(("ok", "partial")),
                IngestionRun.started_at >= cutoff,
            ).limit(1)
        ) is not None


@contextmanager
def tracked_run(engine: Engine, source: str) -> Iterator[RunResult]:
    """Record a run. Exceptions are caught, logged and stored; they never propagate."""
    with session_scope(engine) as s:
        run = IngestionRun(source=source, started_at=datetime.now(UTC), status="running")
        s.add(run)
        s.flush()
        run_id = run.id
    result = RunResult()
    try:
        yield result
    except Exception as exc:  # noqa: BLE001 - every failure must be recorded, never crash-loop
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        log.exception("job %s failed", source, extra={"extra_fields": {"job": source}})
    finally:
        with session_scope(engine) as s:
            s.execute(
                update(IngestionRun).where(IngestionRun.id == run_id).values(
                    finished_at=datetime.now(UTC),
                    status=result.status,
                    rows_inserted=result.rows_inserted,
                    warning="; ".join(result.warnings) or None,
                    error=result.error,
                )
            )
        log.info(
            "job %s finished: %s", source, result.status,
            extra={"extra_fields": {"job": source, "status": result.status, "rows": result.rows_inserted,
                                    "warnings": result.warnings, "error": result.error}},
        )
