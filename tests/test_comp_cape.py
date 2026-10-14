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
    assert r.status_code == 303 and "result=fetched" in r.headers["location"]
    # a second press within the cooldown does not contact the wiki again
    again = admin_client.post("/admin/comp/sync", auth=AUTH, data={"csrf": csrf}, follow_redirects=False)
    assert again.headers["location"] == "/admin/comp?result=fetch_cooldown"
    assert respx.calls.call_count == 1
    assert "available again in" in admin_client.get("/admin/comp", auth=AUTH).text

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
    assert "result=fetch_failed" in r.headers["location"]
    assert "Fetch failed" in admin_client.get(r.headers["location"], auth=AUTH).text


def test_admin_save_with_nothing_ticked_clears_ticks(admin_client, engine):  # noqa: F811
    comp_cape.sync_requirements(engine, synthetic_page())
    comp_cape.save_completions(engine, {"quest-cape"})
    r = admin_client.post("/admin/comp", auth=AUTH, data={"csrf": admin.csrf_token()}, follow_redirects=False)
    assert r.status_code == 303
    with engine.connect() as conn:
        assert q.comp_progress(conn, "UTC")["done"] == 0


def test_category_column_groups_rows():
    html = """<h2>Achievements</h2><table class="wikitable sortable">
    <tr><th>Name</th><th>Members</th><th>Description</th><th>Category</th><th>Subcategory</th><th>Score</th></tr>
    <tr><td>Dragon Slayer</td><td>Yes</td><td>Slay a dragon.</td><td>Combat</td><td>Bosses</td><td>10</td></tr>
    <tr><td>Lore Hunter</td><td>Yes</td><td>Read the books.</td><td>Lore</td><td>Books</td><td>5</td></tr>
    <tr><td>Big Sword</td><td>Yes</td><td>Hit hard.</td><td>Combat</td><td>Melee</td><td>5</td></tr>
    </table>"""
    reqs = parse_requirements(html)
    assert [(r.category, r.name, r.description) for r in reqs] == [
        ("Combat", "Dragon Slayer", "Slay a dragon."), ("Lore", "Lore Hunter", "Read the books."),
        ("Combat", "Big Sword", "Hit hard."),
    ]


def test_tabber_panels_name_the_category():
    html = """<h2>Achievements</h2><div class="tabber">
    <article class="tabber__panel" data-mw-tabber-title="Skills"><table class="wikitable">
      <tr><th>Achievement</th><th>Description</th></tr><tr><td>Max</td><td>All 99s.</td></tr></table></article>
    <div class="tabbertab" title="Exploration"><table class="wikitable">
      <tr><th>Achievement</th><th>Description</th></tr><tr><td>Elite Achiever</td><td>Diaries.</td></tr></table></div>
    </div><table class="wikitable"><tr><th>Achievement</th></tr><tr><td>After tabs</td></tr></table>"""
    assert [(r.category, r.name) for r in parse_requirements(html)] == [
        ("Skills", "Max"), ("Exploration", "Elite Achiever"), ("Achievements", "After tabs")]


def test_overview_groups_interleaved_categories(engine):
    html = """<table class="wikitable"><tr><th>Name</th><th>Category</th></tr>
    <tr><td>A</td><td>Combat</td></tr><tr><td>B</td><td>Lore</td></tr><tr><td>C</td><td>Combat</td></tr></table>"""
    comp_cape.sync_requirements(engine, synthetic_page(html))
    with engine.connect() as conn:
        cats = q.comp_progress(conn, "UTC")["categories"]
    assert [(c["name"], c["total"]) for c in cats] == [("Combat", 2), ("Lore", 1)]


def test_real_page_groups_into_the_seven_achievement_categories():
    """Captured page: one "Achievements list" table with a Category column. The page says it lists 102."""
    reqs = parse_requirements(load_fixture("comp_cape_page.html"))
    counts = {}
    for r in reqs:
        counts[r.category] = counts.get(r.category, 0) + 1
    assert counts == {"Activities": 16, "Area Tasks": 2, "Combat": 1, "Completionist": 1, "Exploration": 24,
                      "Lore": 14, "Skills": 44}
    first = reqs[0]
    assert (first.name, first.description) == ("Balanced Combat Triangle", "Solve a lockbox puzzle.")
    assert len({k for k, _ in assign_keys(reqs)}) == 102
