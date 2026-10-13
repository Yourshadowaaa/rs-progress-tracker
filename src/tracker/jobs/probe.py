"""Endpoint verification probe (milestone 1). Run where runescape.com is reachable:

    python -m tracker.jobs probe [--out probe_output]

Saves raw responses and prints a report: hiscores status per table and name encoding, line
count, RuneMetrics status, activity timestamps vs now (to infer the log's timezone), and the
parser result for each activity. Commit the findings to docs/verification.md and copy useful
responses into tests/fixtures/.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from ..clients.hiscores import name_variants, parse_lite
from ..clients.http import make_client
from ..clients.runemetrics import MONTHLY_URL, parse_profile, profile_url
from ..config import get_settings
from ..parser.engine import match_rule


def run_probe(out_dir: str = "probe_output") -> None:
    s = get_settings()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report: list[str] = [f"probe at {datetime.now(UTC).isoformat()} for {s.player_name!r}"]
    with make_client() as client:
        for table in ("hiscore_ironman", "hiscore_hardcore_ironman", "hiscore"):
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

        try:
            resp = client.get(profile_url(s.player_name))
            report.append(f"runemetrics profile -> HTTP {resp.status_code}")
            (out / "runemetrics_profile.json").write_text(resp.text)
            profile = parse_profile(resp.json())
            now = datetime.now(UTC)
            report.append(f"  {len(profile.activities)} activities, {len(profile.skills)} skill rows")
            if profile.activities:
                newest = profile.activities[0].time_utc
                report.append(f"  newest activity {newest:%Y-%m-%d %H:%M} (as UTC); now {now:%Y-%m-%d %H:%M} UTC; "
                              f"diff {(now - newest).total_seconds() / 3600:+.2f} h (negative => not UTC)")
            with (out / "activities.jsonl").open("w") as f:
                for a in profile.activities:
                    rule, ev = match_rule(a.text, a.details)
                    f.write(json.dumps({"date": a.raw.get("date"), "text": a.text, "details": a.details}) + "\n")
                    report.append(f"  [{rule:16}] {ev.event_type:14} {ev.subject!r} x{ev.count} | {a.text!r}")
        except Exception as exc:  # noqa: BLE001
            report.append(f"runemetrics profile: {type(exc).__name__}: {exc}")

        try:
            resp = client.get(MONTHLY_URL.format(name=quote(s.player_name), skill=-1))
            report.append(f"xp-monthly -> HTTP {resp.status_code}, {len(resp.content)} bytes")
            (out / "xp_monthly.json").write_text(resp.text)
        except Exception as exc:  # noqa: BLE001
            report.append(f"xp-monthly: {type(exc).__name__}: {exc}")

    text = "\n".join(report)
    (out / "report.txt").write_text(text + "\n")
    print(text)
