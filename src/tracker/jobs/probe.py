"""Endpoint verification probe (milestone 1). Run where runescape.com is reachable:

    python -m tracker.jobs probe [--out probe_output]

Saves raw responses and prints a report: hiscores status per table and name encoding, line
count, Adventurer's Log API playerEvents (type, date with offset, parser result per event) and
playerData (XP per skill next to the hiscores XP). Commit the findings to docs/verification.md and copy useful
responses into tests/fixtures/.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from ..clients.adventurers_log import (
    EVENTS_URL,
    events_url,
    parse_events,
    parse_player_data,
    player_data_url,
)
from ..clients.hiscores import name_variants, parse_lite
from ..clients.http import make_client
from ..config import get_settings
from ..parser.engine import match_rule
from ..reference import ALL_SKILLS


def run_probe(out_dir: str = "probe_output") -> None:
    s = get_settings()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report: list[str] = [f"probe at {datetime.now(UTC).isoformat()} for {s.player_name!r}"]
    with make_client() as client:
        for table in ("hiscore", "hiscore_ironman", "hiscore_hardcore_ironman"):
            for encoded in name_variants(s.player_name):
                label = encoded
                url = f"https://secure.runescape.com/m={table}/index_lite.ws?player={encoded}"
                try:
                    resp = client.get(url)
                    line = f"hiscores {table:26} player={label:18} -> HTTP {resp.status_code}"
                    if resp.status_code == 200:
                        lines = resp.text.strip().splitlines()
                        rows = parse_lite(resp.text)
                        line += f", {len(lines)} lines, overall xp={rows[0].xp}"
                        (out / f"hiscores_{table}.csv").write_text(resp.text)
                except Exception as exc:  # noqa: BLE001
                    line = f"hiscores {table} enc={label}: {type(exc).__name__}: {exc}"
                report.append(line)

        hiscore_xp: dict[int, int] = {}
        regular = out / "hiscores_hiscore.csv"
        if regular.exists():
            hiscore_xp = {r.skill_id: r.xp for r in parse_lite(regular.read_text())}

        events_resp = None
        for encoded in name_variants(s.player_name):
            url = EVENTS_URL.format(name=encoded)
            try:
                events_resp = client.get(url)
                report.append(f"playerEvents player={encoded:18} -> HTTP {events_resp.status_code}: "
                              f"{events_resp.text[:80]!r}" if events_resp.status_code != 200
                              else f"playerEvents player={encoded:18} -> HTTP 200")
            except Exception as exc:  # noqa: BLE001
                report.append(f"playerEvents player={encoded}: {type(exc).__name__}: {exc}")
                events_resp = None
            if events_resp is not None and events_resp.status_code == 200:
                break
        if events_resp is not None:
            (out / "adventurers_log_events.json").write_text(events_resp.text)
            try:
                activities = parse_events(events_resp.json())
                now = datetime.now(UTC)
                report.append(f"  {len(activities)} events (url {events_url(s.player_name)})")
                with (out / "activities.jsonl").open("w") as f:
                    for a in activities:
                        rule, ev = match_rule(a.text, a.details)
                        f.write(json.dumps({"date": a.raw.get("date"), "type": a.event_type, "text": a.text,
                                            "details": a.details}) + "\n")
                        report.append(f"  {a.raw.get('date')} ({(now - a.time_utc).total_seconds() / 3600:6.2f} h ago) "
                                      f"{a.event_type} [{rule:16}] {ev.event_type:14} {ev.subject!r} x{ev.count} "
                                      f"| {a.text!r} | {a.details!r}")
            except Exception as exc:  # noqa: BLE001
                report.append(f"  playerEvents parse: {type(exc).__name__}: {exc}")

        try:
            resp = client.get(player_data_url(s.player_name))
            report.append(f"playerData includeSkills -> HTTP {resp.status_code}")
            (out / "adventurers_log_player_data.json").write_text(resp.text)
            rows = parse_player_data(resp.json())
            for r in rows:
                hs = hiscore_xp.get(r.skill_id)
                diff = "" if hs is None else f" hiscores xp={hs:>11} diff={r.xp - hs:+}"
                report.append(f"  {ALL_SKILLS[r.skill_id]:14} level={r.level:4} xp={r.xp:>11}{diff}")
        except Exception as exc:  # noqa: BLE001
            report.append(f"playerData: {type(exc).__name__}: {exc}")

        try:
            from ..clients.wiki import fetch_page
            from .comp_cape import parse_requirements

            page = fetch_page(client, s.comp_cape_page)
            (out / "comp_cape.html").write_text(page.html)
            reqs = parse_requirements(page.html)
            report.append(f"wiki {s.comp_cape_page!r} revision {page.revid}: {len(page.html)} bytes, "
                          f"{len(reqs)} requirements parsed")
            counts: dict[str, int] = {}
            for r in reqs:
                counts[r.category] = counts.get(r.category, 0) + 1
            report.extend(f"  {n:4} {cat}" for cat, n in counts.items())
            report.extend(f"  e.g. [{r.category}] {r.name} | {r.description[:80]}" for r in reqs[:5])
        except Exception as exc:  # noqa: BLE001
            report.append(f"wiki {s.comp_cape_page!r}: {type(exc).__name__}: {exc}")

    text = "\n".join(report)
    (out / "report.txt").write_text(text + "\n")
    print(text)
