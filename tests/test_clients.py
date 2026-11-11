from datetime import UTC, datetime

import httpx
import pytest
import respx

from tests.conftest import load_fixture, load_json
from tracker.clients.adventurers_log import (
    events_url,
    fetch_events,
    fetch_player_skills,
    parse_events,
    parse_player_data,
    player_data_url,
)
from tracker.clients.hiscores import hiscores_url, parse_lite
from tracker.clients.http import NotFound, PlayerHidden, UpstreamError, get_with_retry
from tracker.reference import OVERALL_ID, PLAYER_DATA_SKILL_KEYS, SKILLS


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
    assert events_url("Get Grouped") == \
        "https://secure.runescape.com/m=adventurers-log/api/v1/playerEvents/Get%20Grouped"
    assert player_data_url("Get Grouped") == \
        "https://secure.runescape.com/m=adventurers-log/api/v1/playerData/Get%20Grouped?includeSkills=true"


def test_player_data_skill_keys_cover_every_skill():
    assert sorted(PLAYER_DATA_SKILL_KEYS.values()) == sorted(SKILLS)
    assert PLAYER_DATA_SKILL_KEYS["hitpoints"] == 3 and PLAYER_DATA_SKILL_KEYS["runecraft"] == 20


def test_player_data_fine_xp_divided_by_ten_and_overall_synthesised():
    data = load_json("adventurers_log_player_data_synthetic.json")
    rows = parse_player_data(data)
    by_id = {r.skill_id: r for r in rows}
    assert len(rows) == 30 and all(r.rank is None for r in rows)
    assert by_id[0].xp == data["skills"]["attack"]["fine_xp"] // 10
    assert by_id[3].xp == data["skills"]["hitpoints"]["fine_xp"] // 10 and by_id[3].level == 94
    assert by_id[20].xp == data["skills"]["runecraft"]["fine_xp"] // 10
    overall = by_id[OVERALL_ID]
    assert overall.xp == sum(r.xp for r in rows if r.skill_id != OVERALL_ID)
    assert overall.level == sum(v["level"] for v in data["skills"].values())


def test_player_data_ignores_unknown_keys_and_rejects_empty():
    rows = parse_player_data({"skills": {"attack": {"fine_xp": 125, "level": 2}, "sailing": {"fine_xp": 1, "level": 1}}})
    assert [(r.skill_id, r.xp) for r in rows] == [(0, 12), (OVERALL_ID, 12)]
    with pytest.raises(UpstreamError):
        parse_player_data({"skills": {}})


def test_events_parsed_with_type_and_utc_time():
    data = load_json("adventurers_log_events_synthetic.json")
    acts = parse_events(data)
    assert len(acts) == 8
    assert acts[0].time_utc == datetime(2026, 10, 13, 9, 58, 12, tzinfo=UTC)
    assert acts[0].event_type == "rs_plog_npc_kill_count" and acts[0].raw["type"] == "rs_plog_npc_kill_count"
    quest = next(a for a in acts if a.event_type == "rs_plog_quest")
    assert quest.raw["date"].endswith("+01:00")
    assert quest.time_utc == datetime(2026, 10, 12, 22, 3, 50, tzinfo=UTC)
    assert parse_events({"events": [{"date": "2026-11-11T10:00:00+01:00", "text": "x", "details": "y"}]})[0] \
        .time_utc == datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def test_event_errors():
    with pytest.raises(PlayerHidden, match="PLAYER_NOT_FOUND"):
        parse_events(load_json("adventurers_log_not_found.json"))
    with pytest.raises(UpstreamError, match="INTERNAL_ERROR"):
        parse_events({"error": "INTERNAL_ERROR"})
    with pytest.raises(UpstreamError):
        parse_events({"events": [{"date": "13-Oct-2026 10:01", "text": "x", "details": ""}]})
    with pytest.raises(UpstreamError):
        parse_events([])


@respx.mock
def test_fetch_maps_404_to_player_hidden():
    respx.get(events_url("Get Grouped")).mock(
        return_value=httpx.Response(404, json=load_json("adventurers_log_not_found.json")))
    respx.get(player_data_url("Get Grouped")).mock(return_value=httpx.Response(404, json={"error": "PLAYER_NOT_FOUND"}))
    with httpx.Client() as c:
        with pytest.raises(PlayerHidden, match="PLAYER_NOT_FOUND"):
            fetch_events(c, "Get Grouped")
        with pytest.raises(PlayerHidden):
            fetch_player_skills(c, "Get Grouped")


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
