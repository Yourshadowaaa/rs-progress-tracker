from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import load_json
from tracker.clients.runemetrics import parse_profile
from tracker.jobs.adventure_log import store_activities
from tracker.jobs.runs import RunResult
from tracker.web import app as web
from tracker.web import queries as q


@pytest.fixture
def client(engine):
    def conn_override():
        with engine.connect() as conn:
            yield conn

    web.app.dependency_overrides[web.get_conn] = conn_override
    yield TestClient(web.app)
    web.app.dependency_overrides.clear()


def seed_xp(engine, hours=48):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO players (display_name) VALUES ('Get Grouped')"))
        for h in range(hours):
            t = now - timedelta(hours=hours - 1 - h)
            for sid, base in ((0, 1_000_000), (1, 5_000_000)):
                conn.execute(text("INSERT INTO xp_snapshots (player_id, skill_id, captured_at_utc, xp, level, source) "
                                  "VALUES (1, :s, :t, :xp, 70, 'hiscores')"), {"s": sid, "t": t, "xp": base + h * 1000})
            conn.execute(text("INSERT INTO xp_snapshots (player_id, skill_id, captured_at_utc, xp, level, source) "
                              "VALUES (1, -1, :t, :xp, 140, 'hiscores')"), {"t": t, "xp": 6_000_000 + h * 2000})


def test_empty_database_pages_render(client):
    for path in ("/", "/xp", "/bosses", "/events", "/admin/unparsed", "/status"):
        r = client.get(path)
        assert r.status_code == 200 and "No data yet" in r.text
    h = client.get("/health").json()
    assert h["status"] == "degraded"
    assert client.get("/api/levels").status_code == 404


def walk_routes(routes):
    for r in routes:
        if hasattr(r, "path"):
            yield r
        sub = getattr(r, "routes", None) or getattr(getattr(r, "original_router", None), "routes", None)
        if sub:
            yield from walk_routes(sub)


def test_only_get_routes_exist_except_force_update():
    routes = list(walk_routes(web.app.routes))
    assert any(r.path == "/admin/refresh" for r in routes)
    for route in routes:
        methods = set(getattr(route, "methods", None) or {"GET"})
        if route.path == "/admin/refresh":
            assert methods == {"POST"}
        else:
            assert methods <= {"GET", "HEAD"}, route.path


def test_xp_endpoints(client, engine):
    seed_xp(engine)
    levels = {r["skill"]: r for r in client.get("/api/levels").json()}
    overview = client.get("/").text
    assert '<tr class="total">' in overview and "6,094,000" in overview  # Overall XP as the Total row
    assert levels["Attack"]["xp"] == 1_047_000 and levels["Attack"]["gained_7d"] == 47_000
    day_buckets = {g["bucket"]: g["xp"] for g in client.get(
        "/api/xp/gains", params={"skill": 0, "range": "all", "bucket": "day"}).json()}
    today = datetime.now(ZoneInfo(web.ctx()[1])).date()
    assert levels["Attack"]["gained_today"] == day_buckets.get(today.isoformat(), 0)
    assert levels["Attack"]["gained_yesterday"] == day_buckets.get((today - timedelta(days=1)).isoformat(), 0)
    series = client.get("/api/xp/series", params={"skill": 0, "range": "7d"}).json()
    assert len(series) == 48 and series[-1]["xp"] == 1_047_000
    by_skill = client.get("/api/xp/by-skill", params={"range": "24h"}).json()
    assert {g["skill"]: g["xp"] for g in by_skill}["Defence"] == 24_000
    gains = client.get("/api/xp/gains", params={"skill": 0, "range": "all", "bucket": "hour"}).json()
    assert sum(g["xp"] for g in gains) == 47_000  # sum of deltas == last - first
    stats = client.get("/api/xp/stats", params={"skill": 0, "range": "7d"}).json()
    assert stats["gained"] == 47_000 and round(stats["xp_per_hour"]) == 1000
    assert client.get("/api/xp/series", params={"skill": 99}).status_code == 400
    csv_body = client.get("/api/export/xp.csv", params={"skill": 0}).text
    assert csv_body.splitlines()[0].startswith("captured_at_utc,skill,xp") and len(csv_body.splitlines()) == 49


def test_boss_and_event_endpoints(client, engine, settings):
    seed_xp(engine, hours=2)
    store_activities(engine, settings, parse_profile(load_json("runemetrics_profile_synthetic.json")).activities, RunResult())
    kills = client.get("/api/bosses/kills", params={"range": "all", "bucket": "day"}).json()
    raksha = next(s for s in kills["series"] if s["name"] == "Raksha, the Shadow Colossus")
    day = kills["buckets"].index("2026-10-13")
    assert raksha["data"][day] == 11
    totals = {t["boss"]: t["kills"] for t in client.get("/api/bosses/totals").json()}
    assert totals == {"Raksha, the Shadow Colossus": 11, "Kalphite King": 8}
    ev = client.get("/api/events", params={"type": "level_up"}).json()
    assert ev[0]["subject"] == "Necromancy" and ev[0]["value"] == 103 and ev[0]["time"].endswith("+02:00")
    assert "Raksha" in client.get("/api/export/events.csv").text
    assert client.get("/admin/unparsed").status_code == 200
    assert client.get("/events").status_code == 200


def test_health_reports_failed_latest_run(client, engine):
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO ingestion_runs (source, started_at, finished_at, status, rows_inserted, error) VALUES
            ('xp', now() - interval '10 minutes', now() - interval '10 minutes', 'ok', 30, NULL),
            ('adventure_log', now() - interval '10 minutes', now() - interval '10 minutes', 'ok', 2, NULL),
            ('adventure_log', now(), now(), 'failed', 0, 'ProfilePrivate: RuneMetrics error: PROFILE_PRIVATE')"""))
    h = client.get("/health").json()
    assert h["status"] == "degraded" and h["profile_private"] is True
    assert h["jobs"]["adventure_log"]["failing"] and not h["jobs"]["xp"]["failing"]
    assert "profile is private" in client.get("/status").text


@pytest.mark.parametrize("env_url,expected", [(True, "database_unreachable"), (False, "database_url_missing")])
def test_unreachable_database_explains_itself(monkeypatch, env_url, expected):
    from sqlalchemy import create_engine

    if env_url:
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:secret@127.0.0.1:1/x")
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    engine = create_engine("postgresql+psycopg://u:secret@127.0.0.1:1/x", connect_args={"connect_timeout": 2})

    def broken():
        with engine.connect() as conn:
            yield conn

    web.app.dependency_overrides[web.get_conn] = broken
    try:
        c = TestClient(web.app, raise_server_exceptions=False)
        page = c.get("/")
        assert page.status_code == 503 and expected in page.text and "${{Postgres.DATABASE_URL}}" in page.text
        assert "secret" not in page.text
        health = c.get("/health")
        assert health.status_code == 503 and health.json()["error"] == expected
        assert c.get("/livez").status_code == 200
    finally:
        web.app.dependency_overrides.clear()


def test_missing_schema_explains_itself(migrated_engine):
    from sqlalchemy import create_engine

    url = migrated_engine.url.set(database="rs_empty_schema")
    with migrated_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("DROP DATABASE IF EXISTS rs_empty_schema"))
        conn.execute(text("CREATE DATABASE rs_empty_schema"))
    engine = create_engine(url)

    def empty():
        with engine.connect() as conn:
            yield conn

    web.app.dependency_overrides[web.get_conn] = empty
    try:
        r = TestClient(web.app, raise_server_exceptions=False).get("/")
        assert r.status_code == 503 and "schema_missing" in r.text
    finally:
        web.app.dependency_overrides.clear()
        engine.dispose()


def test_skill_icon_markup():
    assert web.SKILL_ICONS.keys() == set(web.ALL_SKILLS.values())
    img = str(web.skill_icon("Runecrafting"))
    assert 'src="https://runescape.wiki/images/Runecrafting-icon.png"' in img and 'onerror="this.remove()"' in img
    assert str(web.skill_icon("Vorago")) == ""
    assert 'src="/static/skills/overall.png"' in str(web.skill_icon("Overall"))


def test_overview_marks_levels_gained_today(client, engine):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO players (display_name) VALUES ('Get Grouped')"))
        for t, xp, lvl in ((now - timedelta(days=3), 1_000_000, 70), (now, 1_400_000, 72)):
            conn.execute(text("INSERT INTO xp_snapshots (player_id, skill_id, captured_at_utc, xp, level, source) "
                              "VALUES (1, 0, :t, :xp, :lvl, 'hiscores')"), {"t": t, "xp": xp, "lvl": lvl})
    assert client.get("/api/levels").json()[0]["levels_today"] == 2
    assert '<span class="level-up" title="2 levels gained">+2</span>' in client.get("/").text


def test_day_gains_use_local_calendar_days(engine):
    # Berlin is UTC+2 in October: local midnight on the 13th is 22:00 UTC on the 12th.
    points = [("2026-10-11 12:00", 100, 1), ("2026-10-11 21:00", 150, 2), ("2026-10-12 08:00", 400, 4),
              ("2026-10-12 22:00", 700, 5), ("2026-10-13 09:00", 1000, 7)]
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO players (display_name) VALUES ('Get Grouped')"))
        for t, xp, lvl in points:
            conn.execute(text("INSERT INTO xp_snapshots (player_id, skill_id, captured_at_utc, xp, level, source) "
                              "VALUES (1, 0, :t, :xp, :lvl, 'hiscores')"), {"t": f"{t}+00", "xp": xp, "lvl": lvl})
    with engine.connect() as conn:
        now = datetime(2026, 10, 13, 10, tzinfo=UTC)
        # today (13th local) = 1000 - 400; yesterday (12th local, from 22:00 UTC on the 11th) = 400 - 150
        # levels follow the same days: Berlin 7 - 4 and 4 - 2, UTC 7 - 5 and 5 - 2
        assert q.day_gains(conn, 1, "Europe/Berlin", now) == {0: (600, 250, 3, 2)}
        assert q.day_gains(conn, 1, "UTC", now) == {0: (300, 550, 2, 3)}
