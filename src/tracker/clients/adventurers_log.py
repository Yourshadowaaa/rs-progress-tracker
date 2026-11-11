"""Adventurer's Log API client (Jagex, replaces RuneMetrics since 10 Nov 2026).

- playerEvents/NAME: the 20 newest adventure log events, newest first.
- playerData/NAME?includeSkills=true: level and fine_xp (XP x10) per skill. No ranks.

Plain GET, no key, JSON bodies also on error: {"error": "PLAYER_NOT_FOUND"} (HTTP 404, also for a
hidden player), "INTERNAL_ERROR", ...
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from ..reference import OVERALL_ID, PLAYER_DATA_SKILL_KEYS
from .hiscores import SkillRow
from .http import NotFound, PlayerHidden, UpstreamError, get_with_retry

BASE_URL = "https://secure.runescape.com/m=adventurers-log/api/v1"
EVENTS_URL = BASE_URL + "/playerEvents/{name}"
PLAYER_DATA_URL = BASE_URL + "/playerData/{name}?includeSkills=true"


@dataclass(frozen=True)
class Activity:
    time_utc: datetime
    text: str
    details: str
    raw: dict  # the full event as returned; stored as adventure_log_raw.payload
    event_type: str | None = None  # e.g. rs_plog_npc_kill_count; None for legacy RuneMetrics rows


def events_url(name: str) -> str:
    return EVENTS_URL.format(name=quote(name, safe=""))


def player_data_url(name: str) -> str:
    return PLAYER_DATA_URL.format(name=quote(name, safe=""))


def check_error(data: object) -> dict:
    if not isinstance(data, dict):
        raise UpstreamError("Adventurer's Log API returned non-object JSON")
    if "error" in data:
        code = data["error"]
        if code == "PLAYER_NOT_FOUND":
            raise PlayerHidden("PLAYER_NOT_FOUND: player is hidden, renamed or does not exist")
        raise UpstreamError(f"Adventurer's Log API error: {code}")
    return data


def parse_event_time(value: str) -> datetime:
    t = datetime.fromisoformat(value.strip())
    if t.tzinfo is None:
        raise ValueError(f"event date without offset: {value!r}")
    return t.astimezone(UTC)


def parse_events(data: object) -> list[Activity]:
    data = check_error(data)
    try:
        return [
            Activity(parse_event_time(e["date"]), (e.get("text") or "").strip(), (e.get("details") or "").strip(),
                     e, e.get("type"))
            for e in data.get("events", [])
        ]
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise UpstreamError(f"unexpected playerEvents payload: {exc}") from exc


def parse_player_data(data: object) -> list[SkillRow]:
    data = check_error(data)
    try:
        skills = [
            # fine_xp is XP x10 (one decimal place); normalise to whole XP. Player Data has no ranks.
            SkillRow(PLAYER_DATA_SKILL_KEYS[key], int(v["level"]), int(v["fine_xp"]) // 10, None)
            for key, v in (data.get("skills") or {}).items()
            if key in PLAYER_DATA_SKILL_KEYS
        ]
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise UpstreamError(f"unexpected playerData payload: {exc}") from exc
    if not skills:
        raise UpstreamError("playerData returned no skills")
    skills.sort(key=lambda r: r.skill_id)
    skills.append(SkillRow(OVERALL_ID, sum(r.level for r in skills), sum(r.xp for r in skills), None))
    return skills


def _get_json(client: httpx.Client, url: str) -> object:
    try:
        resp = get_with_retry(client, url)
    except NotFound as exc:
        raise PlayerHidden(f"PLAYER_NOT_FOUND (HTTP 404): player is hidden, renamed or does not exist ({url})") from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise UpstreamError("Adventurer's Log API returned invalid JSON") from exc


def fetch_events(client: httpx.Client, name: str) -> list[Activity]:
    return parse_events(_get_json(client, events_url(name)))


def fetch_player_skills(client: httpx.Client, name: str) -> list[SkillRow]:
    return parse_player_data(_get_json(client, player_data_url(name)))
