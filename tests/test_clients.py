import httpx
import pytest
import respx

from tests.conftest import load_fixture, load_json
from tracker.clients.hiscores import hiscores_url, parse_lite
from tracker.clients.http import NotFound, ProfilePrivate, UpstreamError, get_with_retry
from tracker.clients.runemetrics import parse_profile, profile_url
from tracker.reference import OVERALL_ID


def test_parse_lite_maps_overall_and_skills():
    rows = parse_lite(load_fixture("hiscores_ironman_synthetic.csv"))
    assert len(rows) == 30
    assert rows[0].skill_id == OVERALL_ID
    assert [r.skill_id for r in rows[1:]] == list(range(29))
    assert rows[0].xp == sum(r.xp for r in rows[1:])


def test_parse_lite_unranked_and_bad_input():
    body = "\n".join(["-1,1,-1"] * 30)
    rows = parse_lite(body)
    assert rows[5].rank is None and rows[5].xp == 0
    with pytest.raises(UpstreamError):
        parse_lite("1,2,3\n4,5,6")
    with pytest.raises(UpstreamError):
        parse_lite("<html>not csv</html>\n" * 30)


def test_name_is_url_encoded():
    assert hiscores_url("hiscore_ironman", "Get Grouped").endswith("player=Get%20Grouped")
    assert "user=Get%20Grouped&activities=20" in profile_url("Get Grouped")


def test_runemetrics_xp_divided_by_ten_and_activities_parsed():
    data = load_json("runemetrics_profile_synthetic.json")
    profile = parse_profile(data)
    by_id = {s.skill_id: s for s in profile.skills}
    assert by_id[0].xp == data["skillvalues"][0]["xp"] // 10
    assert by_id[OVERALL_ID].xp == data["totalxp"]
    assert profile.activities[0].time_utc.isoformat() == "2026-10-13T09:58:00+00:00"


def test_profile_private():
    with pytest.raises(ProfilePrivate, match="PROFILE_PRIVATE"):
        parse_profile(load_json("runemetrics_private.json"))


@respx.mock
def test_retry_then_success_and_404():
    url = "https://example.test/x"
    route = respx.get(url).mock(side_effect=[httpx.Response(503), httpx.Response(200, text="ok")])
    sleeps = []
    with httpx.Client() as c:
        assert get_with_retry(c, url, retries=3, sleep=sleeps.append).text == "ok"
    assert route.call_count == 2 and sleeps == [2]
    respx.get("https://example.test/missing").mock(return_value=httpx.Response(404))
    with httpx.Client() as c, pytest.raises(NotFound):
        get_with_retry(c, "https://example.test/missing", retries=3, sleep=sleeps.append)


@respx.mock
def test_retry_gives_up():
    respx.get("https://example.test/down").mock(side_effect=httpx.ConnectError("boom"))
    with httpx.Client() as c, pytest.raises(UpstreamError, match="giving up after 3"):
        get_with_retry(c, "https://example.test/down", retries=3, sleep=lambda s: None)
