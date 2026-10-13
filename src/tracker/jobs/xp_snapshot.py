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


def _on_table(client: httpx.Client, table: str, name: str) -> bool | None:
    """Diagnostic only: is the player on `table`? None if that could not be determined."""
    try:
        fetch_hiscores(client, table, name)
        return True
    except NotFound:
        return False
    except UpstreamError:
        return None


def _runemetrics_rows(client: httpx.Client, settings: Settings, reason: Exception) -> list[SkillRow]:
    profile = fetch_profile(client, settings.player_name)
    if not profile.skills:
        raise UpstreamError("RuneMetrics fallback returned no skill values") from reason
    if settings.hiscore_table == "hiscore":
        return profile.skills
    # RuneMetrics ranks are regular-hiscores ranks; don't mix them into an ironman-table rank history.
    return [SkillRow(r.skill_id, r.level, r.xp, None) for r in profile.skills]


def fetch_skill_rows(client: httpx.Client, settings: Settings, result: RunResult) -> tuple[list[SkillRow], str]:
    table, name = settings.hiscore_table, settings.player_name
    try:
        rows, url = fetch_hiscores(client, table, name)
        log.info("hiscores ok: %s", url)
        return rows, "hiscores"
    except NotFound as exc:
        # Never read another hiscores table in its place. Check the regular table only to explain
        # the problem, then take XP from RuneMetrics (the account's own data) so tracking continues.
        hint = ""
        if table != "hiscore":
            on_regular = _on_table(client, "hiscore", name)
            if on_regular:
                hint = (" The player IS on the regular hiscores (m=hiscore). Group ironmen and former ironmen "
                        "are only ranked there: set HISCORE_TABLE=hiscore.")
            elif on_regular is False:
                hint = (" The player is not on the regular hiscores either: the name may be wrong or the "
                        "account is not ranked in any skill yet.")
        try:
            rows = _runemetrics_rows(client, settings, exc)
        except UpstreamError as rm_exc:
            raise TableMismatch(f"'{name}' not found on m={table} ({exc}).{hint} RuneMetrics fallback also "
                                f"failed: {rm_exc}") from exc
        result.warn(f"'{name}' not found on m={table} ({exc}).{hint} XP taken from RuneMetrics instead.")
        return rows, "runemetrics"
    except UpstreamError as exc:
        result.warn(f"hiscores failed ({exc}); used RuneMetrics fallback")
        return _runemetrics_rows(client, settings, exc), "runemetrics"


def store_snapshot(
    engine: Engine, settings: Settings, rows: list[SkillRow], source: str, captured_at: datetime, result: RunResult,
    overwrite: bool = False,
) -> None:
    """Insert one snapshot per skill for `captured_at`. With overwrite (manual force update), an
    existing snapshot for the same hour is replaced with the fresh values instead of kept."""
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
        stmt = insert(XpSnapshot).values(values)
        if overwrite:
            stmt = stmt.on_conflict_do_update(
                constraint="uq_xp_snapshot",
                set_={c: stmt.excluded[c] for c in ("xp", "level", "rank", "source", "flag")},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(constraint="uq_xp_snapshot")
        result.rows_inserted += len(s.execute(stmt.returning(XpSnapshot.id)).all())


def run_xp_job(engine: Engine, settings: Settings, client: httpx.Client, result: RunResult,
               now: datetime | None = None, overwrite: bool = False) -> None:
    captured_at = hour_floor(now or datetime.now(UTC))
    rows, source = fetch_skill_rows(client, settings, result)
    store_snapshot(engine, settings, rows, source, captured_at, result, overwrite=overwrite)
    if result.rows_inserted == 0:
        result.warn(f"snapshot for {captured_at.isoformat()} already existed (duplicate fire)")

