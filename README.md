# RS3 Progress Tracker

A personal tracker for the RuneScape 3 ironman **Get Grouped**. Every hour it records:

1. **XP snapshots** for every skill from the ironman hiscores (full history, never overwritten).
2. **The adventure log** from RuneMetrics. Raw entries are stored forever and parsed into structured
   events: boss kills with counts, level-ups, XP milestones, quests, drops, pets, clues and so on.

A public, read-only dashboard charts XP over time, gains, projections and boss kills.

## Architecture

```
Railway project
├── Postgres            (Railway database)
├── web                 FastAPI + Jinja + ECharts dashboard, public domain   railway.web.json
└── ingest              cron "0 * * * *": python -m tracker.jobs run-all     railway.cron.json
```

Both services build from the same `Dockerfile`.

- **Why a cron service instead of an always-on worker:** ingestion is isolated from dashboard
  deploys and crashes, and it is billed only for the few seconds it runs each hour. All inserts
  are idempotent (`ON CONFLICT DO NOTHING`), and a guard skips a job that already succeeded in the
  last 50 minutes, so double fires and overlaps are harmless. The web service shows freshness
  from the `ingestion_runs` table.
- **Migrations** run as the web service's pre-deploy command (`alembic upgrade head`). The cron job
  refuses to run if the schema is behind.
- **Failures** never crash-loop. Each job records a `failed` row in `ingestion_runs` (shown on
  `/status` and `/health`) and the process exits 0. There is no external alerting, by design.

| Path | What it is |
|---|---|
| `src/tracker/clients/` | Hiscores and RuneMetrics HTTP clients (User-Agent, retry with backoff, typed errors) |
| `src/tracker/jobs/` | XP job, adventure-log job, reparse, backup, probe, CLI (`__main__.py`) |
| `src/tracker/parser/rules.py` | Ordered regex rules for adventure log entries, plus `PARSER_VERSION` |
| `src/tracker/reference.py` | Skills, XP curve, bosses and aliases |
| `src/tracker/web/` | Dashboard, JSON API (`/api/...`), CSV exports, `/health` |
| `alembic/` | Schema migrations |
| `tests/` | Unit tests and Postgres integration tests with recorded fixtures |

### Data handling decisions

- **XP source.** The table set by `HISCORE_TABLE` (default `hiscore_ironman`). A **404 fails the
  run loudly** and never silently switches tables. On other hiscores errors the job falls back to
  RuneMetrics `skillvalues`, which report XP ×10; the job divides by 10. The snapshot time is the
  run time truncated to the hour (UTC).
- **Bad data.** An all-zero payload is rejected. An XP decrease is stored with `flag='xp_decrease'`
  and a warning.
- **Adventure log dedupe.** `entry_key = sha256(time | text | details)` plus an `occurrence` number.
  If a fetch contains *n* copies of a key and the database already holds *m*, occurrences *m+1..n*
  are inserted. Two genuinely identical entries in the same minute are both kept, and re-fetching
  the same 20 entries inserts nothing.
- **Gap detection.** If the database already has entries and none of the fetched entries were seen
  before, the run is marked `partial` with a "possible gap" warning, because events may have been lost.
- **Timestamps** are stored in UTC and shown in `DISPLAY_TZ` (default Europe/Berlin). A kill entry's
  time is when the game wrote the log entry, not when each kill happened. The bosses page says so.

## Railway setup

1. Create a project and add **Postgres**.
2. Add a service from this GitHub repo and name it **web**:
   - Settings → Config-as-code → file path `railway.web.json`.
   - Variables: `DATABASE_URL=${{Postgres.DATABASE_URL}}` (the private network URL) and the optional
     variables below.
   - Networking → generate a public domain.
3. Add a second service from the same repo and name it **ingest**:
   - Config file path `railway.cron.json` (sets cron `0 * * * *`, restart policy NEVER).
   - The same `DATABASE_URL` variable. No public domain.
4. Deploy `web` first so its pre-deploy runs the migrations. Then trigger `ingest` once by hand
   ("Run now" or a redeploy) and check `/status`.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | – | `${{Postgres.DATABASE_URL}}`; `postgres://` URLs are accepted |
| `PLAYER_NAME` | `Get Grouped` | URL-encoded as `Get%20Grouped` |
| `HISCORE_TABLE` | `hiscore_ironman` | `hiscore`, `hiscore_ironman` or `hiscore_hardcore_ironman` |
| `DISPLAY_TZ` | `Europe/Berlin` | |
| `USER_AGENT` | `rs-progress-tracker/0.1 (...)` | Identifiable UA sent to Jagex |
| `HTTP_RETRIES` | `3` | Attempts per request (backoff 2 s, 4 s) |
| `MIN_RUN_INTERVAL_MIN` | `50` | Double-fire guard |
| `BACKUP_DIR` / `BACKUP_S3_*` | unset | See Backups |

### Backups

The adventure log cannot be re-fetched beyond the last 20 entries, so **losing the database
permanently loses that history**.

1. Turn on Railway's native backups for the Postgres service if your plan includes them (Postgres
   service → Backups). Check what your plan offers.
2. Fallback, built in: when `BACKUP_S3_BUCKET` and/or `BACKUP_DIR` is set, the cron job runs
   `pg_dump --format=custom` once a day at `BACKUP_HOUR_UTC` (default 03) and prunes dumps older
   than `BACKUP_RETENTION_DAYS` (default 30).
   - **S3 / Railway Storage Bucket:** set `BACKUP_S3_BUCKET`, `BACKUP_S3_ENDPOINT`,
     `BACKUP_S3_ACCESS_KEY_ID` and `BACKUP_S3_SECRET_ACCESS_KEY` (and `BACKUP_S3_REGION` if needed),
     referenced from the bucket's variables.
   - **Volume:** attach a volume to `ingest` and set `BACKUP_DIR` to its mount path.
   - The image ships the PostgreSQL 17 `pg_dump`, which must be at least the server's major
     version. Check the Railway Postgres version.
   - Restore with `pg_restore --clean --no-owner -d "$DATABASE_URL" rs-tracker-YYYYMMDDTHHMMSSZ.dump`.

### Expected cost

This fits the Hobby plan: Postgres with less than 100 MB of data per year, the web service at about
100–200 MB RAM always on, and the cron service running for seconds per hour. Expect roughly
**$5–10 per month**. Confirm with Railway's usage page after the first week.

## Operations

```bash
python -m tracker.jobs run-all          # what cron runs (add --force to bypass the double-fire guard)
python -m tracker.jobs xp | log         # a single job
python -m tracker.jobs reparse [--all]  # rebuild events after parser changes (deterministic, no duplicates)
python -m tracker.jobs probe            # check the endpoints and save raw responses to probe_output/
python -m tracker.jobs export-corpus    # stored log entries as JSONL, to grow the parser test corpus
python -m tracker.jobs backup           # run a backup now
```

On Railway, run these with `railway run --service ingest python -m tracker.jobs ...`, or from the
service shell.

### Improving the parser

1. Open `/admin/unparsed` for unparsed entries and unrecognised boss or skill names.
2. Add a rule in `src/tracker/parser/rules.py`, or an alias in `reference.py` (`BOSSES` /
   `BOSS_ALIASES`).
3. Add the real entry with its expected event to `tests/fixtures/log_corpus.jsonl` and run the tests.
4. Bump `PARSER_VERSION`, deploy, then run `python -m tracker.jobs reparse`.

## Development

```bash
uv sync
uv run pytest                                   # unit tests; integration tests are skipped
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/rs_test uv run pytest   # with a disposable Postgres
DATABASE_URL=postgresql://localhost/rs_dev uv run alembic upgrade head
DATABASE_URL=postgresql://localhost/rs_dev uv run python scripts/seed_synthetic.py --yes-this-is-a-dev-db
DATABASE_URL=postgresql://localhost/rs_dev uv run uvicorn tracker.web.app:app --reload
```

**Warning:** the integration tests drop and recreate the `public` schema of `TEST_DATABASE_URL`.
Never point it at a real database.

## Status against the brief

See [`docs/verification.md`](docs/verification.md) for what is verified and what still needs a
live check. The endpoint wording and the log corpus could not be fetched from the build sandbox,
so the fixtures are **synthetic** until the probe has run.
