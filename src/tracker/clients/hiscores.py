"""RS3 hiscores 'index_lite' client (CSV: one `rank,level,xp` line per skill, Overall first)."""

from dataclasses import dataclass
from urllib.parse import quote, quote_plus

import httpx

from ..reference import ALL_SKILLS, OVERALL_ID
from .http import NotFound, UpstreamError, get_with_retry

BASE = "https://secure.runescape.com/m={table}/index_lite.ws?player={name}"
SKILL_LINES = len(ALL_SKILLS)  # Overall + 29 skills


@dataclass(frozen=True)
class SkillRow:
    skill_id: int
    level: int
    xp: int
    rank: int | None


def hiscores_url(table: str, name: str) -> str:
    return BASE.format(table=table, name=quote(name))


def name_variants(name: str) -> list[str]:
    """Spellings of a display name that Jagex endpoints accept, most common first.
    Spaces in RS names can be sent as %20, +, _ or a non-breaking space (%A0)."""
    variants = [quote(name)]
    if " " in name:
        variants += [quote_plus(name), quote(name.replace(" ", "_")), quote(name).replace("%20", "%A0")]
    return list(dict.fromkeys(variants))


def parse_lite(body: str) -> list[SkillRow]:
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    if len(lines) < SKILL_LINES:
        raise UpstreamError(f"hiscores returned {len(lines)} lines, expected at least {SKILL_LINES}")
    rows: list[SkillRow] = []
    for index, line in enumerate(lines[:SKILL_LINES]):
        parts = line.split(",")
        if len(parts) != 3:
            raise UpstreamError(f"unexpected hiscores line {index}: {line!r}")
        try:
            rank, level, xp = (int(p) for p in parts)
        except ValueError as exc:
            raise UpstreamError(f"non-numeric hiscores line {index}: {line!r}") from exc
        skill_id = OVERALL_ID if index == 0 else index - 1
        rows.append(SkillRow(skill_id, level, max(xp, 0), rank if rank > 0 else None))
    return rows


def fetch_hiscores(client: httpx.Client, table: str, name: str) -> tuple[list[SkillRow], str]:
    """Fetch the lite CSV. A 404 means "player not on this table" but can also come from how the
    space in the name was encoded, so every spelling is tried before raising NotFound.
    Returns (rows, the URL that worked)."""
    tried = []
    for encoded in name_variants(name):
        url = BASE.format(table=table, name=encoded)
        try:
            resp = get_with_retry(client, url)
        except NotFound:
            tried.append(encoded)
            continue
        return parse_lite(resp.text), url
    raise NotFound(f"404 from m={table} for every name spelling tried: {', '.join(tried)}")
