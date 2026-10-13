"""RuneMetrics profile client (adventure log + skill values)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from ..reference import OVERALL_ID, SKILLS
from .hiscores import SkillRow
from .http import ProfilePrivate, UpstreamError, get_with_retry

PROFILE_URL = "https://apps.runescape.com/runemetrics/profile/profile?user={name}&activities=20"
MONTHLY_URL = "https://apps.runescape.com/runemetrics/xp-monthly?searchName={name}&skillid={skill}"
DATE_FORMAT = "%d-%b-%Y %H:%M"


@dataclass(frozen=True)
class Activity:
    time_utc: datetime
    text: str
    details: str
    raw: dict


@dataclass(frozen=True)
class Profile:
    activities: list[Activity]  # newest first, as returned by the API
    skills: list[SkillRow]
    raw: dict


def profile_url(name: str) -> str:
    return PROFILE_URL.format(name=quote(name))


def parse_activity_time(value: str) -> datetime:
    # RuneMetrics timestamps carry no zone; treated as UTC (see docs/verification.md).
    return datetime.strptime(value.strip(), DATE_FORMAT).replace(tzinfo=UTC)


def parse_profile(data: dict) -> Profile:
    if not isinstance(data, dict):
        raise UpstreamError("RuneMetrics returned non-object JSON")
    if "error" in data:
        raise ProfilePrivate(f"RuneMetrics error: {data['error']}")
    try:
        activities = [
            Activity(parse_activity_time(a["date"]), a.get("text", "").strip(), a.get("details", "").strip(), a)
            for a in data.get("activities", [])
        ]
        skills = [
            # skillvalues XP is reported x10 (one decimal place); normalise to whole XP.
            SkillRow(int(sv["id"]), int(sv["level"]), int(sv["xp"]) // 10, int(sv["rank"]) if sv.get("rank") else None)
            for sv in data.get("skillvalues", [])
            if int(sv["id"]) in SKILLS
        ]
    except (KeyError, ValueError, TypeError) as exc:
        raise UpstreamError(f"unexpected RuneMetrics payload: {exc}") from exc
    if skills and "totalxp" in data:
        total_level = sum(s.level for s in skills)
        rank = data.get("rank")
        overall_rank = int(str(rank).replace(",", "")) if rank and str(rank).replace(",", "").isdigit() else None
        skills.append(SkillRow(OVERALL_ID, int(data.get("totalskill", total_level)), int(data["totalxp"]), overall_rank))
    return Profile(activities, skills, data)


def fetch_profile(client: httpx.Client, name: str) -> Profile:
    resp = get_with_retry(client, profile_url(name))
    try:
        data = resp.json()
    except ValueError as exc:
        raise UpstreamError("RuneMetrics returned invalid JSON") from exc
    return parse_profile(data)
