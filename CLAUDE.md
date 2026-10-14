# CLAUDE.md

Context for Claude Code sessions working on this repo. `README.md` covers setup and operations for
humans. This file covers what a new session needs to change the code safely.

## What this is

A personal RS3 progress tracker for one account, **`Get Grouped`**, a **group ironman** (GIM).
- An hourly job stores XP snapshots from the Jagex hiscores.
- It also stores the RuneMetrics adventure log, parsed into events (boss kills with counts,
  level-ups, XP milestones, quests, drops, pets, clues and so on).
- A public, read-only dashboard charts both.

Production runs on Railway: https://rs-progress-tracker-production.up.railway.app/

## Facts that were learned the hard way

- **GIM accounts are only ranked on the regular hiscores table** (`m=hiscore`), not the ironman
  tables. `HISCORE_TABLE` therefore defaults to `hiscore`.
- **The adventure log only exposes the last 20 entries.** A missed poll loses events for good, and
  losing the database loses history for good. Ingestion reliability and backups matter more than features.
- **The display name contains a space.** The hiscores client tries `%20`, `+`, `_` and `%A0`
  (`name_variants()` in `clients/hiscores.py`).
- **RuneMetrics `skillvalues` XP is ×10.** Divide it by 10, as `parse_profile` does.
- **Unverified hypotheses.** Log timestamps (`13-Oct-2026 10:01`) are assumed to be UTC. Parser
  wording comes from synthetic samples. Both still need confirming against live data; see
  `docs/verification.md`.
- **Jagex and railway.app are blocked from Claude's cloud sandbox** (proxy 403). Live checks have to
  happen on Railway: run `python -m tracker.jobs probe` in the web service shell. The user can also
  paste output.

## Architecture

```
Railway project
├── Postgres     DATABASE_URL = ${{Postgres.DATABASE_URL}} on both services
├── web          railway.web.json   start: alembic upgrade head; uvicorn tracker.web.app:app   healthcheck /livez
└── ingest       railway.cron.json  cron "0 * * * *": python -m tracker.jobs run-all (runs once, exits)
```

- One `Dockerfile` serves both services.
- Railway deploys from **`master`**: every push to `master` goes live on both services. Work on a
  feature branch and merge (or push) to `master` when it is ready; run the full test suite first.
- Stack: Python 3.12, uv, FastAPI, Jinja2 templates, Apache ECharts 5.5.1 from the jsdelivr CDN,
  SQLAlchemy 2 Core/ORM, Alembic, psycopg 3, httpx, pytest + respx.

## Code map

| Path | Role |
|---|---|
| `src/tracker/config.py` | pydantic-settings, read from env vars. Rewrites `postgres://` URLs to `postgresql+psycopg://` |
| `src/tracker/reference.py` | Skill ids (RuneMetrics 0–28, Overall = −1; hiscores line index = id + 1), XP table to level 120, `BOSSES` / `BOSS_ALIASES` / `normalise_boss()` |
| `src/tracker/models.py` | Tables: players, skills, xp_snapshots, adventure_log_raw, adventure_events, ingestion_runs. The `xp_deltas` view is defined in migration 0001 |
| `src/tracker/clients/` | `http.py` (retry/backoff; `NotFound` / `ProfilePrivate` / `UpstreamError`), `hiscores.py` (lite CSV), `runemetrics.py` (profile JSON) |
| `src/tracker/jobs/xp_snapshot.py` | Hourly snapshot: hiscores, falling back to RuneMetrics. `overwrite=True` for manual runs |
| `src/tracker/jobs/adventure_log.py` | Raw entry storage with occurrence dedupe, gap warning, event parsing, `reparse()` |
| `src/tracker/jobs/runner.py` | `run_all()`, shared by cron and the force-update button. Postgres advisory lock `INGEST_LOCK_KEY` |
| `src/tracker/jobs/runs.py` | `tracked_run()` context manager writes `ingestion_runs` and swallows and records every exception. JSON logging |
| `src/tracker/jobs/__main__.py` | CLI: `run-all`, `xp`, `log`, `backup`, `reparse [--all]`, `probe`, `export-corpus` |
| `src/tracker/jobs/backup.py` / `probe.py` | Daily `pg_dump` to a directory or S3 / endpoint verification report |
| `src/tracker/parser/rules.py` | Ordered regex `RULES` list (first match wins), `PARSER_VERSION` |
| `src/tracker/web/app.py` | Pages, `/api/*` JSON, CSV exports, `/health`, `/livez`, DB-error handler (503 page that names the fix) |
| `src/tracker/web/queries.py` | All dashboard SQL. Buckets are computed in `DISPLAY_TZ` (Europe/Berlin) |
| `src/tracker/web/admin.py` | `/admin` + `POST /admin/refresh`: HTTP Basic auth (`ADMIN_PASSWORD`), CSRF token, cooldown, background thread. `/admin/comp` checklist: `POST /admin/comp` (save ticks), `POST /admin/comp/sync` ("Fetch new requirements": the only way the web app contacts the wiki, 10-minute cooldown) |
| `src/tracker/clients/wiki.py` / `jobs/comp_cape.py` | Completionist cape: MediaWiki `action=parse` fetch, generic `wikitable` extractor, requirement sync (deactivate, never delete), tick saving |
| `src/tracker/web/static/app.js` | Chart rendering, one `Tracker.*Page()` per page. Colours come from CSS variables in `style.css` |
| `alembic/versions/` | 0001 schema + view, 0002 skill seed, 0003 `ingestion_runs.trigger`, 0004 `wiki_pages` / `comp_requirements` / `comp_completions` |
| `tests/` | Unit tests plus Postgres integration tests. Fixtures in `tests/fixtures/` (the `*_synthetic*` ones are not real API captures) |

## Invariants: don't break these

1. **Idempotent ingestion.**
   - Every insert uses `ON CONFLICT` on the unique keys: `uq_xp_snapshot(player, skill, hour)`,
     `uq_log_entry(player, entry_key, occurrence)` and `uq_event(raw_id, type, subject)`.
   - Cron can double-fire or overlap, and re-running must never duplicate rows.
2. **Snapshot timing.** Snapshots sit at the hour floor (UTC). A manual force update overwrites the
   current hour's row and never adds a second row in the same hour.
3. **Adventure log dedupe.**
   - `entry_key = sha256(time|text|details)`. If the database holds *m* copies of a key and the
     fetch contains *n*, insert occurrences *m+1..n*.
   - Identical same-minute entries are real and are kept.
4. **Raw log text is never discarded.** Events are derived from it and can be rebuilt. After
   changing parser rules or `BOSSES` / `BOSS_ALIASES`, bump `PARSER_VERSION` and run `reparse`.
5. **Never substitute another hiscores table's data.** On a 404 the job checks the regular table
   only to explain the problem, then uses RuneMetrics (the account's own data).
6. **Jobs never crash-loop.**
   - Failures are recorded in `ingestion_runs` and the process exits 0.
   - Health counts as degraded when a job's latest run failed, or it has had no success for 2 hours.
7. **The public site is read-only.** The only write endpoints are `POST /admin/refresh`,
   `POST /admin/comp` and `POST /admin/comp/sync`, all behind Basic auth plus CSRF.
   `test_only_get_routes_exist_except_admin_writes` enforces this. Don't add write routes without
   auth, and never render the CSRF token on a public page.
8. **Polite to Jagex.**
   - Identifiable User-Agent, at most hourly automatic polling, retry with backoff only on 5xx and
     429 responses and network errors.
   - Force update has a cooldown (`FORCE_UPDATE_COOLDOWN_MIN`).
   - The same goes for the RuneScape Wiki: it is fetched only on an explicit admin action (button or
     `comp-sync`), never on a schedule or page view.
9. **Schema changes go through new Alembic migrations.** Never use `create_all`. The web container
   runs `alembic upgrade head` on every start. `check_schema()` makes the cron job refuse to run on
   a schema that is behind.
10. **No secrets in the repo.** Configuration comes from Railway variables only, and the dashboard
    must never echo credentials (the DB error page shows host/port/db only).

## Dashboard and chart conventions

- Dark theme only (the user's preference). Use the tokens on `:root` in `style.css` and don't
  hard-code colours.
- Series colours: `--series-1..8`, in that fixed order.
- Boss stacks: at most 8 named series, the rest grouped as "Other". A boss's colour slot comes from
  its all-time kill rank (`boss_order`), so colours stay stable when filters change.
- One y-axis per chart. Gains bars use a 7-period moving-average line.
- Long ranges are downsampled: ranges over 7d use daily points, and hourly gain buckets are only
  allowed up to 30d.
- Performance target: every page under 2 s with one year of hourly data. Last measured locally at
  under 0.4 s with the synthetic seed.
- Must work at phone width (390px) without horizontal scroll.
- Skill names are shown with their RuneScape Wiki icon (`SKILL_ICONS` in `reference.py`, hotlinked from
  `runescape.wiki/images/<Skill>-icon.png`): `skill_icon()` in templates, `skillIcon()` in `app.js`, rich-text
  labels in ECharts. Overall's icon is bundled at `static/skills/overall.png`. A failed image removes itself, so the wiki being down only drops the icons.

## Working on it

```bash
uv sync
uv run pytest                     # without TEST_DATABASE_URL the integration tests are skipped
```

The integration tests need a **disposable** Postgres, and they drop and recreate its `public`
schema. In the cloud sandbox, PostgreSQL 16 binaries are at `/usr/lib/postgresql/16/bin`:

```bash
D=/var/tmp/rs-pg; mkdir -p $D && chown postgres $D
su postgres -c "/usr/lib/postgresql/16/bin/initdb -D $D/data -A trust -U postgres"   # first time only
su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D $D/data -o '-p 5433 -k /tmp' -l $D/log start"
psql -h localhost -p 5433 -U postgres -c "create database rs_test" -c "create database rs_tracker"
TEST_DATABASE_URL=postgresql://postgres@localhost:5433/rs_test uv run pytest -q
```

Sandbox gotchas:
- The Postgres server stops whenever the container is recycled. Re-run the `pg_ctl start` line.
- Don't use `pkill -f <pattern>` for servers you start: the pattern can match your own shell and kill
  it. Start them with `setsid ... & echo $! > pidfile` and stop them with `kill -- -$(cat pidfile)`.
- jsdelivr is blocked, so for Playwright screenshots, route `cdn.jsdelivr.net/**` to a local copy
  (`npm pack echarts@5.5.1`).
- Chromium is at `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`.

Dev data: `scripts/seed_synthetic.py --yes-this-is-a-dev-db` fills a dev database with a year of fake
data. **Never run it against Railway.**

CI (`.github/workflows/ci.yml`) runs the full suite against a Postgres 16 service container.

## Open items

- Confirm against live data (`docs/verification.md`):
  - log timezone
  - real adventure log wording; replace synthetic corpus lines with real ones (`export-corpus`)
  - Railway config-as-code field names
  - backup upload to S3 or a Railway bucket
- Invention uses the elite XP curve, which isn't tabulated, so it shows no next-level progress.
- Completionist cape extractor (`jobs/comp_cape.py`) was written against a synthetic page
  (`tests/fixtures/comp_cape_synthetic.html`) because the wiki is blocked from the sandbox. Check the
  first real sync, and replace the fixture with the real HTML (download it from `/admin/comp`).
- Optional, not built: backfill from the RuneMetrics `xp-monthly` endpoint. The URL is already in
  `clients/runemetrics.py` as `MONTHLY_URL`.
