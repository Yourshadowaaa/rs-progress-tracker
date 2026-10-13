"""Daily pg_dump fallback backup to a local/volume directory and/or an S3-compatible bucket
(e.g. a Railway Storage Bucket). Keeps BACKUP_RETENTION_DAYS of dumps."""

import logging
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import Settings
from .runs import RunResult

log = logging.getLogger(__name__)
PREFIX = "rs-tracker-"


def backup_configured(settings: Settings) -> bool:
    return bool(settings.backup_dir or settings.backup_s3_bucket)


def _libpq_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def run_backup(settings: Settings, result: RunResult, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    name = f"{PREFIX}{now:%Y%m%dT%H%M%SZ}.dump"
    cutoff = now - timedelta(days=settings.backup_retention_days)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", f"--file={path}", _libpq_url(settings.database_url)],
            check=True, capture_output=True, timeout=600,
        )
        size = path.stat().st_size
        if settings.backup_dir:
            target = Path(settings.backup_dir)
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_bytes(path.read_bytes())
            for old in target.glob(f"{PREFIX}*.dump"):
                if datetime.fromtimestamp(old.stat().st_mtime, UTC) < cutoff:
                    old.unlink()
        if settings.backup_s3_bucket:
            import boto3

            s3 = boto3.client(
                "s3", endpoint_url=settings.backup_s3_endpoint, region_name=settings.backup_s3_region,
                aws_access_key_id=settings.backup_s3_access_key_id,
                aws_secret_access_key=settings.backup_s3_secret_access_key,
            )
            s3.upload_file(str(path), settings.backup_s3_bucket, name)
            listing = s3.list_objects_v2(Bucket=settings.backup_s3_bucket, Prefix=PREFIX)
            for obj in listing.get("Contents", []):
                if obj["LastModified"] < cutoff:
                    s3.delete_object(Bucket=settings.backup_s3_bucket, Key=obj["Key"])
    result.rows_inserted = 1
    log.info("backup written: %s (%d bytes)", name, size)
