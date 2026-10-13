"""Hourly XP snapshot: hiscores (configured table) with RuneMetrics skillvalues as fallback."""

import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import distinct_on, insert

from ..clients.hiscores import SkillRow, fetch_hiscores
from ..clients.http import NotFound, UpstreamError
from ..clients.runemetrics import fetch_profile
from ..config import Settings
from ..db import get_or_create_player, session_scope
from ..models import XpSnapshot
from .runs import RunResult

log = logging.getLogger(__name__)


class TableMismatch(UpstreamError):
    """Player is not on the configured hiscores table (404). Never silently use another table."""


def hour_floor(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def fetch_skill_rows(client: httpx.Client, settings: Settings, result: RunResult) -> tuple[list[SkillRow], str]:
    try:
        rows, _ = fetch_hiscores(client, settings.hiscore_table, settings.player_name)
        return rows, "hiscores"
    except NotFound as exc:
        raise TableMismatch(
            f"'{settings.player_name}' not found on m={settings.hiscore_table} (404). Check HISCORE_TABLE "
            "(hiscore / hiscore_ironman / hiscore_hardcore_ironman); not falling back to another table."
        ) from exc
    except UpstreamError as exc:
        result.warn(f"hiscores failed ({exc}); used RuneMetrics fallback")
        profile = fetch_profile(client, settings.player_name)
        if not profile.skills:
            raise UpstreamError("RuneMetrics fallback returned no skill values") from exc
        return profile.skills, "runemetrics"


def store_snapshot(
    engine: Engine, settings: Settings, rows: list[SkillRow], source: str, captured_at: datetime, result: RunResult
) -> None:
    if not rows or all(r.xp == 0 for r in rows):
        raise UpstreamError("all-zero XP payload; refusing to store")
    with session_scope(engine) as s:
        player_id = get_or_create_player(s, settings.player_name)
        # Latest previous XP per skill, to flag decreases (should never happen in RS3).
        latest = dict(
            s.execute(
                select(XpSnapshot.skill_id, XpSnapshot.xp)
                .ext(distinct_on(XpSnapshot.skill_id))
                .where(XpSnapshot.player_id == player_id, XpSnapshot.captured_at_utc < captured_at)
                .order_by(XpSnapshot.skill_id, XpSnapshot.captured_at_utc.desc())
            ).all()
        )
        values = []
        for r in rows:
            flag = None
            if r.skill_id in latest and r.xp < latest[r.skill_id]:
                flag = "xp_decrease"
                result.warn(f"XP decreased for skill {r.skill_id}: {latest[r.skill_id]} -> {r.xp}")
            values.append(dict(player_id=player_id, skill_id=r.skill_id, captured_at_utc=captured_at,
                               xp=r.xp, level=r.level, rank=r.rank, source=source, flag=flag))
        stmt = insert(XpSnapshot).values(values).on_conflict_do_nothing(constraint="uq_xp_snapshot")
        result.rows_inserted += len(s.execute(stmt.returning(XpSnapshot.id)).all())


def run_xp_job(engine: Engine, settings: Settings, client: httpx.Client, result: RunResult,
               now: datetime | None = None) -> None:
    captured_at = hour_floor(now or datetime.now(UTC))
    rows, source = fetch_skill_rows(client, settings, result)
    store_snapshot(engine, settings, rows, source, captured_at, result)
    if result.rows_inserted == 0:
        result.warn(f"snapshot for {captured_at.isoformat()} already existed (duplicate fire)")

