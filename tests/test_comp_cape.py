import httpx
import pytest
import respx
from sqlalchemy import text

from tests.conftest import load_fixture
from tests.test_admin import AUTH, admin_client  # noqa: F401  (fixture)
from tracker.clients.http import UpstreamError, make_client
from tracker.clients.wiki import WikiPage, fetch_page, parse_url
from tracker.jobs import comp_cape
from tracker.jobs.comp_cape import Requirement, assign_keys, parse_requirements
from tracker.web import admin
from tracker.web import queries as q

PAGE = "Completionist Cape (achievement)"


def synthetic_page(html: str | None = None) -> WikiPage:
    return WikiPage(title=PAGE, revid=123, html=html or load_fixture("comp_cape_synthetic.html"))


def test_parse_requirements_from_rendered_tables():
    reqs = parse_requirements(load_fixture("comp_cape_synthetic.html"))
    assert [(r.category, r.name) for r in reqs] == [
        ("Skills", "Attack"), ("Skills", "Dungeoneering"),
        ("Quests & lore › Quests", "Quest Cape"), ("Quests & lore › Quests", "Attack"),
        ("Quests & lore › Lore", "Lore Collector"),
    ]
    by_name = {(r.category, r.name): r for r in reqs}
    assert by_name[("Skills", "Attack")].description == "99"  # hidden sort key dropped
    assert by_name[("Quests & lore › Quests", "Quest Cape")].description == "Complete every quest."
    assert by_name[("Quests & lore › Lore", "Lore Collector")].description == "Find every lore book."


def test_list_fallback_only_under_requirement_headings():
    html = ("<h2>Requirements</h2><ul><li>Max cape</li><li>All quests<ul><li>sub note</li></ul></li></ul>"
            "<h2>Trivia</h2><ul><li>Not a requirement</li></ul>")
    assert [r.name for r in parse_requirements(html)] == ["Max cape", "sub note", "All quests"]
    assert parse_requirements("<h2>Other</h2><ul><li>x</li></ul>") == []


def test_keys_are_name_slugs_qualified_only_on_collision():
    keys = [k for k, _ in assign_keys([Requirement("Skills", "Attack"), Requirement("Quests", "Attack"),
                                       Requirement("Quests", "Quest Cape")])]
    assert keys == ["skills-attack", "quests-attack", "quest-cape"]


@respx.mock
def test_fetch_page_uses_parse_api_and_reports_errors():
    respx.get(parse_url(PAGE)).mock(return_value=httpx.Response(
        200, json={"parse": {"title": PAGE, "revid": 9, "text": "<p>hi</p>"}}))
    with make_client() as client:
        page = fetch_page(client, PAGE)
    assert (page.revid, page.html) == (9, "<p>hi</p>")
    respx.get(parse_url("Missing")).mock(return_value=httpx.Response(
        200, json={"error": {"code": "missingtitle", "info": "The page you specified doesn't exist."}}))
    with make_client() as client, pytest.raises(UpstreamError, match="missingtitle"):
        fetch_page(client, "Missing")


def test_sync_keeps_ticks_and_deactivates_dropped_requirements(engine):
    result = comp_cape.sync_requirements(engine, synthetic_page())
    assert (result.total, result.added, result.removed) == (5, 5, 0)
    comp_cape.save_completions(engine, {"quest-cape", "dungeoneering", "not-a-key"})
    with engine.connect() as conn:
        progress = q.comp_progress(conn, "UTC")
    assert (progress["done"], progress["total"], progress["pct"]) == (2, 5, 40.0)
    assert progress["source"]["revid"] == 123

    smaller = load_fixture("comp_cape_synthetic.html").replace(
        '<tr><td><a href="/w/Dungeoneering">Dungeoneering</a></td><td>120</td></tr>', "")
    result = comp_cape.sync_requirements(engine, synthetic_page(smaller))
    assert (result.total, result.added, result.removed) == (4, 0, 1)
    with engine.connect() as conn:
        progress = q.comp_progress(conn, "UTC")
        assert (progress["done"], progress["total"]) == (1, 4)
        # the dropped requirement keeps its tick, so it comes back done if the wiki lists it again
        assert conn.execute(text("SELECT count(*) FROM comp_completions")).scalar() == 2
    comp_cape.sync_requirements(engine, synthetic_page())
    with engine.connect() as conn:
        assert q.comp_progress(conn, "UTC")["done"] == 2


def test_sync_with_nothing_parsed_changes_nothing(engine):
    comp_cape.sync_requirements(engine, synthetic_page())
    with pytest.raises(comp_cape.NoRequirementsFound):
        comp_cape.sync_requirements(engine, synthetic_page("<p>redesigned page</p>"))
    with engine.connect() as conn:
        assert q.comp_progress(conn, "UTC")["total"] == 5
    assert "redesigned" in comp_cape.stored_page(engine, PAGE).html  # kept for diagnosis


@respx.mock
def test_admin_sync_and_save_then_overview_shows_progress(admin_client, engine):  # noqa: F811
    from tests.test_web import seed_xp
    seed_xp(engine, hours=2)
    respx.get(parse_url(PAGE)).mock(return_value=httpx.Response(
        200, json={"parse": {"title": PAGE, "revid": 77, "text": load_fixture("comp_cape_synthetic.html")}}))
    csrf = admin.csrf_token()
    assert admin_client.post("/admin/comp/sync", data={"csrf": csrf}).status_code == 401
    assert admin_client.post("/admin/comp/sync", auth=AUTH, data={"csrf": "forged"}).status_code == 403
    r = admin_client.post("/admin/comp/sync", auth=AUTH, data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "result=synced" in r.headers["location"]

    page = admin_client.get("/admin/comp", auth=AUTH).text
    assert 'value="quest-cape"' in page and "Lore Collector" in page and AUTH[1] not in page
    assert admin_client.post("/admin/comp", auth=AUTH, data={"csrf": "forged", "done": ["quest-cape"]}).status_code == 403
    r = admin_client.post("/admin/comp", auth=AUTH, data={"csrf": csrf, "done": ["quest-cape", "skills-attack"]},
                          follow_redirects=False)
    assert r.status_code == 303

    overview = admin_client.get("/").text
    assert "Completionist cape" in overview and "2 of 5 components complete" in overview
    assert "40.0%" in overview and 'href="/admin/comp"' in overview and csrf not in overview
    assert admin_client.get("/admin/comp/source.html", auth=AUTH).text.startswith("<div")


@respx.mock
def test_admin_sync_failure_is_reported(admin_client, engine):  # noqa: F811
    respx.get(parse_url(PAGE)).mock(return_value=httpx.Response(403, text="blocked"))
    r = admin_client.post("/admin/comp/sync", auth=AUTH, data={"csrf": admin.csrf_token()}, follow_redirects=False)
    assert "result=sync_failed" in r.headers["location"]
    assert "Sync failed" in admin_client.get(r.headers["location"], auth=AUTH).text


def test_admin_save_with_nothing_ticked_clears_ticks(admin_client, engine):  # noqa: F811
    comp_cape.sync_requirements(engine, synthetic_page())
    comp_cape.save_completions(engine, {"quest-cape"})
    r = admin_client.post("/admin/comp", auth=AUTH, data={"csrf": admin.csrf_token()}, follow_redirects=False)
    assert r.status_code == 303
    with engine.connect() as conn:
        assert q.comp_progress(conn, "UTC")["done"] == 0
