# Verification log

## Environment limitation

The code was built in a sandbox whose network proxy blocks `secure.runescape.com` (HTTP 403).
RuneMetrics (`apps.runescape.com/runemetrics/...`) went offline on 10 Nov 2026; the adventure log and
the XP fallback now use the Adventurer's Log API. **No live response has been fetched yet.** Every fixture in
`tests/fixtures/` whose name contains `synthetic` (including the `adventurers_log_*_synthetic.json`
files, built from the documented response shape), and every corpus line with
`"source": "synthetic"`, follows the documented or commonly observed format but is a hypothesis
until confirmed.

## First-deploy checklist

Run `python -m tracker.jobs probe` in a shell on the web service (`railway ssh --service web`), or run the probe locally from any
machine that can reach runescape.com. It writes `probe_output/report.txt` and the raw responses.
Record the results here:

| # | Check | How | Result |
|---|---|---|---|
| 1 | Regular hiscores return data for `Get Grouped` (GIM accounts are not on the ironman tables) | report: `hiscore player=Get%20Grouped -> HTTP 200` | ✅ confirmed by user (regular table only) |
| 2 | Name encoding: `%20` and `+` both work? | report lines for each encoding | ☐ |
| 3 | Ironman tables return 404 for this group ironman | report | ✅ confirmed by user |
| 4 | Lite CSV has ≥ 30 `rank,level,xp` lines, Necromancy is line 30 | report line count; compare levels in-game | ☐ |
| 5 | `playerEvents/Get%20Grouped` returns 200 with events (event visibility on, `%20` accepted) | report `playerEvents player=Get%20Grouped -> HTTP 200` | ☐ |
| 6 | Player Data `fine_xp` is ×10 | report: `diff=+0` against the hiscores XP for every skill | ☐ |
| 7 | Event dates carry an offset and line up with real time | report: `date` and "h ago" per event | ✅ by API docs for new events; legacy rows (RuneMetrics era) still assumed UTC, unverified |
| 8 | Real log wording matches the parser rules, and matches the RuneMetrics-era wording | report lines show rule names; any `unparsed` ⇒ add rules; compare with `export-corpus` | ☐ |
| 9 | Hidden player gives 404 `{"error":"PLAYER_NOT_FOUND"}` | only if it happens: `/status` shows the "not found or hidden" banner | ☐ |
| 10 | Cutover: the first run after the switch inserts no duplicates of rows stored before 10 Nov | `/status` warning "cutover: N entries matched" or a gap warning; query below | ☐ |

After the probe:

1. Copy `probe_output/hiscores_hiscore.csv` to `tests/fixtures/hiscores_regular_live.csv` and
   `adventurers_log_events.json` / `adventurers_log_player_data.json` to
   `tests/fixtures/adventurers_log_events_live.json` / `adventurers_log_player_data_live.json`, then add
   tests that use them.
2. Append real entries (`python -m tracker.jobs export-corpus`) to `tests/fixtures/log_corpus.jsonl`
   with `"source": "live"` and their expected events. Fix any rules that disagree, bump
   `PARSER_VERSION` and run `reparse`.
3. If the live wording differs from the RuneMetrics era only in whitespace or punctuation, normalise it in
   `entry_key` only (never in stored text), otherwise legacy rows won't dedupe.
4. Check the cutover (expect no row pairs with the same text a whole number of hours apart):

   ```sql
   SELECT a.entry_time_utc, b.entry_time_utc, a.text FROM adventure_log_raw a
   JOIN adventure_log_raw b ON a.text = b.text AND a.details = b.details AND a.id < b.id
   WHERE NOT a.payload ? 'type' AND b.payload ? 'type'
     AND abs(extract(epoch FROM b.entry_time_utc - a.entry_time_utc)) <= 14 * 3600;
   ```

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

- 95 tests pass against PostgreSQL 16. They cover:
  - parser corpus and unparsed rate
  - hiscores and Adventurer's Log API parsing, including `fine_xp` ÷ 10, skill key mapping, ISO dates with
    offsets and `PLAYER_NOT_FOUND`
  - retry and backoff
  - idempotent XP snapshots
  - a 404 failing without fallback
  - fallback to Player Data (no ranks)
  - RuneMetrics cutover guard (exact match, whole-hour offset, genuine repeats still inserted)
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
