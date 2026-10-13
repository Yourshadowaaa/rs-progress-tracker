import time
import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from tests.conftest import load_fixture, load_json
from tracker.clients.hiscores import hiscores_url
from tracker.clients.runemetrics import profile_url
from tracker.jobs import runner
from tracker.models import IngestionRun, XpSnapshot
from tracker.web import admin
from tracker.web import app as web

AUTH = ("admin", "correct horse battery staple")


@pytest.fixture
def admin_client(engine, settings, monkeypatch):
    configured = settings.model_copy(update={"admin_password": AUTH[1], "force_update_cooldown_min": 5})
    for target in ("tracker.web.admin.get_settings", "tracker.web.app.get_settings",
                   "tracker.jobs.runner.get_settings", "tracker.clients.http.get_settings"):
        monkeypatch.setattr(target, lambda: configured)
    monkeypatch.setattr("tracker.web.admin.get_engine", lambda: engine)

    def conn_override():
        with engine.connect() as conn:
            yield conn

    web.app.dependency_overrides[web.get_conn] = conn_override
    yield TestClient(web.app)
    web.app.dependency_overrides.clear()


def wait_for_manual_run():
    for _ in range(100):
        if not admin._manual_run.locked():
            return
        time.sleep(0.05)
    raise AssertionError("force update did not finish")


def test_disabled_without_password(client):
    assert client.get("/admin").status_code == 404
    assert client.post("/admin/refresh", data={"csrf": "x"}).status_code == 404
    assert "Force update" not in client.get("/status").text


@pytest.fixture
def client(engine):
    def conn_override():
        with engine.connect() as conn:
            yield conn

    web.app.dependency_overrides[web.get_conn] = conn_override
    yield TestClient(web.app)
    web.app.dependency_overrides.clear()


def test_requires_basic_auth(admin_client):
    r = admin_client.get("/admin")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert admin_client.get("/admin", auth=("admin", "wrong")).status_code == 401
    assert admin_client.get("/admin", auth=("someone", AUTH[1])).status_code == 401
    assert admin_client.post("/admin/refresh", data={"csrf": admin.csrf_token()}).status_code == 401
    page = admin_client.get("/admin", auth=AUTH)
    assert page.status_code == 200 and admin.csrf_token() in page.text and AUTH[1] not in page.text
    assert 'href="/admin"' in admin_client.get("/status").text


def test_post_requires_csrf_token(admin_client):
    assert admin_client.post("/admin/refresh", auth=AUTH, data={"csrf": "forged"}).status_code == 403
    assert admin_client.post("/admin/refresh", auth=AUTH).status_code == 403


@respx.mock
def test_force_update_runs_jobs_then_enforces_cooldown(admin_client, engine):
    respx.get(hiscores_url("hiscore_ironman", "Get Grouped")).mock(
        return_value=httpx.Response(200, text=load_fixture("hiscores_ironman_synthetic.csv")))
    respx.get(profile_url("Get Grouped")).mock(
        return_value=httpx.Response(200, json=load_json("runemetrics_profile_synthetic.json")))
    r = admin_client.post("/admin/refresh", auth=AUTH, data={"csrf": admin.csrf_token()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin?result=started"
    wait_for_manual_run()
    with engine.connect() as conn:
        runs = conn.execute(select(IngestionRun.source, IngestionRun.status, IngestionRun.trigger)
                            .order_by(IngestionRun.id)).all()
    assert runs == [("xp", "ok", "manual"), ("adventure_log", "ok", "manual")]
    again = admin_client.post("/admin/refresh", auth=AUTH, data={"csrf": admin.csrf_token()}, follow_redirects=False)
    assert again.headers["location"] == "/admin?result=cooldown"
    page = admin_client.get("/admin?result=cooldown", auth=AUTH).text
    assert "Too soon" in page and "Available in" in page and "manual" in page


@respx.mock
def test_manual_run_refreshes_current_hour_snapshot(engine, settings, monkeypatch):
    monkeypatch.setattr("tracker.jobs.runner.get_settings", lambda: settings)
    monkeypatch.setattr("tracker.clients.http.get_settings", lambda: settings)
    body = load_fixture("hiscores_ironman_synthetic.csv")
    lines = body.splitlines()
    rank, level, xp = lines[1].split(",")
    newer = "\n".join([lines[0], f"{rank},{level},{int(xp) + 5000}", *lines[2:]])
    respx.get(hiscores_url("hiscore_ironman", "Get Grouped")).mock(
        side_effect=[httpx.Response(200, text=body), httpx.Response(200, text=newer)])
    respx.get(profile_url("Get Grouped")).mock(
        return_value=httpx.Response(200, json=load_json("runemetrics_profile_synthetic.json")))
    runner.run_all(engine)  # scheduled run
    runner.run_all(engine, trigger="manual")  # force update in the same hour
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(XpSnapshot)).scalar() == 30
        assert conn.execute(select(XpSnapshot.xp).where(XpSnapshot.skill_id == 0)).scalar() == int(xp) + 5000


def test_force_update_skipped_while_another_run_holds_lock(engine, settings, monkeypatch):
    monkeypatch.setattr("tracker.jobs.runner.get_settings", lambda: settings)
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": runner.INGEST_LOCK_KEY})
        assert runner.run_all(engine, trigger="manual") is False
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": runner.INGEST_LOCK_KEY})
        holder.commit()
    with engine.connect() as conn:
        rows = conn.execute(select(IngestionRun.status, IngestionRun.warning)).all()
    assert len(rows) == 2 and all(s == "skipped" and "in progress" in w for s, w in rows)
