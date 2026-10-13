# Verification log

## Environment limitation

The code was built in a sandbox whose network proxy blocks `secure.runescape.com` and
`apps.runescape.com` (HTTP 403). **No live response has been fetched yet.** Every fixture in
`tests/fixtures/` whose name contains `synthetic`, and every corpus line with
`"source": "synthetic"`, follows the documented or commonly observed format but is a hypothesis
until confirmed.

## First-deploy checklist

Run `python -m tracker.jobs probe` in a shell on the web service (`railway ssh --service web`), or run the probe locally from any
machine that can reach runescape.com. It writes `probe_output/report.txt` and the raw responses.
Record the results here:

| # | Check | How | Result |
|---|---|---|---|
| 1 | Ironman hiscores return data for `Get Grouped` | report: `hiscore_ironman enc=%20 -> HTTP 200` | ☐ |
| 2 | Name encoding: `%20` and `+` both work? | report lines for each encoding | ☐ |
| 3 | Not on hardcore table / regular table behaviour (404 vs 200) | report | ☐ |
| 4 | Lite CSV has ≥ 30 `rank,level,xp` lines, Necromancy is line 30 | report line count; compare levels in-game | ☐ |
| 5 | RuneMetrics profile is public (no `PROFILE_PRIVATE`) | report | ☐ |
| 6 | `skillvalues` XP is ×10 | compare `runemetrics_profile.json` with hiscores CSV | ☐ |
| 7 | Adventure log timestamps are UTC | report "diff" for a recent entry (≈ 0 to +1 h ⇒ UTC; −1/−2 h ⇒ UK/CET local) | ☐ |
| 8 | Real log wording matches the parser rules | report lines show rule names; any `unparsed` ⇒ add rules | ☐ |
| 9 | Monthly XP endpoint reachable (optional) | report | ☐ |

After the probe:

1. Copy `probe_output/hiscores_hiscore_ironman.csv` to `tests/fixtures/hiscores_ironman_live.csv` and
   `runemetrics_profile.json` to `tests/fixtures/runemetrics_profile_live.json`, then add tests that
   use them.
2. Append real entries (`python -m tracker.jobs export-corpus`) to `tests/fixtures/log_corpus.jsonl`
   with `"source": "live"` and their expected events. Fix any rules that disagree, bump
   `PARSER_VERSION` and run `reparse`.
3. If item 7 shows the log is not UTC, change `parse_activity_time` in `clients/runemetrics.py`,
   then fix existing rows with a one-off migration.

## 7-day soak (acceptance)

After 7 days of cron runs:

```sql
-- 168 ok/partial runs per job, no failed runs (or failures explained)
SELECT source, status, count(*) FROM ingestion_runs
WHERE started_at > now() - interval '7 days' GROUP BY 1, 2 ORDER BY 1, 2;

-- no duplicate snapshots (enforced by a unique key; expect 0 rows)
SELECT player_id, skill_id, captured_at_utc, count(*) FROM xp_snapshots GROUP BY 1, 2, 3 HAVING count(*) > 1;

-- hours with a missing XP snapshot
SELECT h FROM generate_series(date_trunc('hour', now() - interval '7 days'), date_trunc('hour', now()), '1 hour') h
WHERE NOT EXISTS (SELECT 1 FROM xp_snapshots WHERE skill_id = -1 AND captured_at_utc = h);

-- gap warnings in the adventure log
SELECT started_at, warning FROM ingestion_runs WHERE source = 'adventure_log' AND warning LIKE '%gap%';
```

## Verified in the sandbox

- 55 tests pass against PostgreSQL 16. They cover:
  - parser corpus and unparsed rate
  - hiscores and RuneMetrics parsing, including XP ÷ 10 and `PROFILE_PRIVATE`
  - retry and backoff
  - idempotent XP snapshots
  - a 404 failing without fallback
  - fallback to RuneMetrics
  - XP-decrease flag
  - delta sums equal to last minus first
  - occurrence dedupe across sliding windows
  - gap warnings
  - deterministic reparse
  - `run-all` twice with no duplicates
  - the API, including the "11 Raksha" acceptance case
- With one year of synthetic hourly data (about 263k snapshots), every dashboard page and API call
  responded in under 0.4 s locally, against the 2 s target.
- A real `python -m tracker.jobs run-all` with blocked endpoints recorded two `failed` runs, exited 0,
  and turned `/health` to `degraded`.
- **Not verified here:**
  - building the Docker image (no Docker daemon in the sandbox)
  - Railway config-as-code field names: check them against Railway's current schema on first deploy
  - the backup upload to S3
