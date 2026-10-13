"""Integration tests against a real Postgres (TEST_DATABASE_URL) with mocked HTTP."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select, text

from tests.conftest import load_fixture, load_json
from tracker.clients.hiscores import hiscores_url
from tracker.clients.runemetrics import Activity, parse_profile, profile_url
from tracker.jobs.adventure_log import reparse, store_activities
from tracker.jobs.runs import RunResult, tracked_run
from tracker.jobs.xp_snapshot import run_xp_job
from tracker.models import AdventureEvent, AdventureLogRaw, IngestionRun, XpSnapshot

HISCORES = hiscores_url("hiscore_ironman", "Get Grouped")
PROFILE = profile_url("Get Grouped")
NOW = datetime(2026, 10, 13, 10, 0, 37, tzinfo=UTC)


def count(engine, model, *where):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(model).where(*where)).scalar()


def act(minute: int, text_: str, details: str = "") -> Activity:
    t = datetime(2026, 10, 13, 9, minute, tzinfo=UTC)
    return Activity(t, text_, details or text_, {"date": t.strftime("%d-%b-%Y %H:%M"), "text": text_, "details": details})


@respx.mock
def test_xp_job_idempotent_and_hour_truncated(engine, settings):
    respx.get(HISCORES).mock(return_value=httpx.Response(200, text=load_fixture("hiscores_ironman_synthetic.csv")))
    with httpx.Client() as client:
        r1, r2 = RunResult(), RunResult()
        run_xp_job(engine, settings, client, r1, now=NOW)
        run_xp_job(engine, settings, client, r2, now=NOW + timedelta(minutes=5))
    assert r1.rows_inserted == 30 and r2.rows_inserted == 0
    assert r2.status == "partial"  # duplicate fire is noted, not an error
    with engine.connect() as conn:
        times = conn.execute(select(XpSnapshot.captured_at_utc).distinct()).scalars().all()
    assert times == [NOW.replace(minute=0, second=0)]


def ironman_url(encoded: str, table: str = "hiscore_ironman") -> str:
    return f"https://secure.runescape.com/m={table}/index_lite.ws?player={encoded}"


@respx.mock
def test_xp_job_tries_other_space_encodings(engine, settings):
    respx.get(ironman_url("Get%20Grouped")).mock(return_value=httpx.Response(404))
    respx.get(ironman_url("Get+Grouped")).mock(return_value=httpx.Response(404))
    underscore = respx.get(ironman_url("Get_Grouped")).mock(
        return_value=httpx.Response(200, text=load_fixture("hiscores_ironman_synthetic.csv")))
    with httpx.Client() as client:
        result = RunResult()
        run_xp_job(engine, settings, client, result, now=NOW)
    assert underscore.called and result.status == "ok" and result.rows_inserted == 30
    with engine.connect() as conn:
        assert conn.execute(select(XpSnapshot.source).distinct()).scalars().all() == ["hiscores"]


@respx.mock
def test_xp_404_diagnoses_and_falls_back_to_runemetrics_not_regular_table(engine, settings):
    for enc in ("Get%20Grouped", "Get+Grouped", "Get_Grouped", "Get%A0Grouped"):
        respx.get(ironman_url(enc)).mock(return_value=httpx.Response(404))
    regular = respx.get(ironman_url("Get%20Grouped", "hiscore")).mock(
        return_value=httpx.Response(200, text="\n".join(["1,99,999999999"] * 30)))
    data = load_json("runemetrics_profile_synthetic.json")
    respx.get(PROFILE).mock(return_value=httpx.Response(200, json=data))
    with httpx.Client() as client, tracked_run(engine, "xp") as result:
        run_xp_job(engine, settings, client, result, now=NOW)
    assert regular.called and result.status == "partial"
    assert "IS on the regular hiscores" in result.warnings[0] and "Get%A0Grouped" in result.warnings[0]
    with engine.connect() as conn:
        attack = conn.execute(select(XpSnapshot.xp, XpSnapshot.source, XpSnapshot.rank)
                              .where(XpSnapshot.skill_id == 0)).one()
    # XP comes from RuneMetrics, never from the regular table's numbers.
    assert attack == (data["skillvalues"][0]["xp"] // 10, "runemetrics", None)


@respx.mock
def test_xp_404_and_runemetrics_private_fails_loudly(engine, settings):
    respx.get(url__startswith="https://secure.runescape.com/").mock(return_value=httpx.Response(404))
    respx.get(PROFILE).mock(return_value=httpx.Response(200, json={"error": "PROFILE_PRIVATE"}))
    with httpx.Client() as client, tracked_run(engine, "xp") as result:
        run_xp_job(engine, settings, client, result, now=NOW)
    assert result.status == "failed" and "TableMismatch" in result.error
    assert "not on the regular hiscores either" in result.error and "PROFILE_PRIVATE" in result.error
    assert count(engine, XpSnapshot) == 0


@respx.mock
def test_xp_job_falls_back_to_runemetrics_on_outage(engine, settings):
    respx.get(HISCORES).mock(return_value=httpx.Response(503))
    data = load_json("runemetrics_profile_synthetic.json")
    respx.get(PROFILE).mock(return_value=httpx.Response(200, json=data))
    with httpx.Client() as client:
        result = RunResult()
        run_xp_job(engine, settings, client, result, now=NOW)
    assert result.status == "partial" and result.rows_inserted == 30
    with engine.connect() as conn:
        attack = conn.execute(select(XpSnapshot.xp, XpSnapshot.source).where(XpSnapshot.skill_id == 0)).one()
    assert attack == (data["skillvalues"][0]["xp"] // 10, "runemetrics")


@respx.mock
def test_xp_decrease_is_flagged(engine, settings):
    body = load_fixture("hiscores_ironman_synthetic.csv")
    lines = body.splitlines()
    rank, level, xp = lines[1].split(",")
    lowered = "\n".join([lines[0], f"{rank},{level},{int(xp) - 100}", *lines[2:]])
    respx.get(HISCORES).mock(side_effect=[httpx.Response(200, text=body), httpx.Response(200, text=lowered)])
    with httpx.Client() as client:
        run_xp_job(engine, settings, client, RunResult(), now=NOW)
        r = RunResult()
        run_xp_job(engine, settings, client, r, now=NOW + timedelta(hours=1))
    assert any("decreased" in w for w in r.warnings)
    assert count(engine, XpSnapshot, XpSnapshot.flag == "xp_decrease") == 1


def test_xp_deltas_view_sums_to_last_minus_first(engine, settings):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO players (display_name) VALUES ('Get Grouped')"))
        for h, xp in enumerate([1000, 1500, 1500, 4000, 4100]):
            conn.execute(text(
                "INSERT INTO xp_snapshots (player_id, skill_id, captured_at_utc, xp, level, source) "
                "VALUES (1, 0, :t, :xp, 10, 'hiscores')"), {"t": NOW + timedelta(hours=h), "xp": xp})
        total = conn.execute(text(
            "SELECT SUM(delta) FROM xp_deltas WHERE skill_id = 0 AND captured_at_utc > :a AND captured_at_utc <= :b"
        ), {"a": NOW, "b": NOW + timedelta(hours=4)}).scalar()
    assert total == 4100 - 1000


def test_log_dedupe_keeps_identical_entries_and_reruns_insert_nothing(engine, settings):
    batch = [act(58, "I killed 11 Raksha, the Shadow Colossus."),
             act(50, "I killed  4 Kalphite Kings."), act(50, "I killed  4 Kalphite Kings.")]
    r1, r2 = RunResult(), RunResult()
    store_activities(engine, settings, batch, r1)
    store_activities(engine, settings, batch, r2)
    assert r1.rows_inserted == 3 and r2.rows_inserted == 0
    with engine.connect() as conn:
        kk = conn.execute(select(func.sum(AdventureEvent.count)).where(AdventureEvent.subject == "Kalphite King")).scalar()
        raksha = conn.execute(select(AdventureEvent.count, AdventureEvent.event_time_utc)
                              .where(AdventureEvent.subject == "Raksha, the Shadow Colossus")).one()
    assert kk == 8
    assert raksha == (11, datetime(2026, 10, 13, 9, 58, tzinfo=UTC))


def test_log_sliding_window_and_new_identical_entry(engine, settings):
    a, b = act(50, "I killed  4 Kalphite Kings."), act(51, "Levelled up Attack.", "I levelled my Attack skill, I am now level 92.")
    store_activities(engine, settings, [b, a, a], RunResult())
    # One of the two identical entries scrolled out of the window: nothing new.
    r = RunResult()
    store_activities(engine, settings, [b, a], r)
    assert r.rows_inserted == 0
    # A third identical entry appears in the same minute: exactly one new row.
    r = RunResult()
    store_activities(engine, settings, [b, a, a, a], r)
    assert r.rows_inserted == 1
    assert count(engine, AdventureLogRaw) == 4


def test_gap_warning_when_no_overlap(engine, settings):
    store_activities(engine, settings, [act(1, "Levelled up Attack.")], RunResult())
    r = RunResult()
    store_activities(engine, settings, [act(i + 10, f"I found a Thing {i}") for i in range(20)], r)
    assert r.status == "partial" and "possible gap" in r.warnings[0]
    r = RunResult()
    store_activities(engine, settings, [act(40, "I found a New thing"), act(29, "I found a Thing 19")], r)
    assert r.status == "ok"


def test_reparse_is_deterministic_and_does_not_duplicate(engine, settings):
    acts = parse_profile(load_json("runemetrics_profile_synthetic.json")).activities
    store_activities(engine, settings, acts, RunResult())
    before = count(engine, AdventureEvent)
    with engine.begin() as conn:
        conn.execute(text("UPDATE adventure_events SET parser_version = 'old' WHERE id % 2 = 0"))
        conn.execute(text("DELETE FROM adventure_events WHERE id = 1"))
    entries, written = reparse(engine)
    assert entries >= 1 and count(engine, AdventureEvent) == before
    with engine.connect() as conn:
        snapshot = conn.execute(text("SELECT raw_id, event_type, subject, count, value FROM adventure_events ORDER BY raw_id")).all()
    reparse(engine, everything=True)
    with engine.connect() as conn:
        again = conn.execute(text("SELECT raw_id, event_type, subject, count, value FROM adventure_events ORDER BY raw_id")).all()
    assert snapshot == again and count(engine, AdventureEvent, AdventureEvent.parser_version == "old") == 0


@respx.mock
def test_run_all_twice_is_idempotent(engine, settings, monkeypatch):
    import tracker.jobs.runner as cli

    respx.get(HISCORES).mock(return_value=httpx.Response(200, text=load_fixture("hiscores_ironman_synthetic.csv")))
    respx.get(PROFILE).mock(return_value=httpx.Response(200, json=load_json("runemetrics_profile_synthetic.json")))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("tracker.clients.http.get_settings", lambda: settings)
    cli.run_all(engine)
    counts = (count(engine, XpSnapshot), count(engine, AdventureLogRaw), count(engine, AdventureEvent))
    assert counts == (30, 8, 8)
    cli.run_all(engine)  # skipped by the min-interval guard
    cli.run_all(engine, force=True)  # forced: still no duplicate rows
    assert (count(engine, XpSnapshot), count(engine, AdventureLogRaw), count(engine, AdventureEvent)) == counts
    with engine.connect() as conn:
        statuses = conn.execute(select(IngestionRun.source, IngestionRun.status).order_by(IngestionRun.id)).all()
    assert statuses[:2] == [("xp", "ok"), ("adventure_log", "ok")] and len(statuses) == 4


@respx.mock
def test_profile_private_recorded_as_failed_run(engine, settings):
    from tracker.jobs.adventure_log import run_log_job

    respx.get(PROFILE).mock(return_value=httpx.Response(200, json={"error": "PROFILE_PRIVATE", "loggedIn": "false"}))
    with httpx.Client() as client, tracked_run(engine, "adventure_log") as result:
        run_log_job(engine, settings, client, result)
    assert result.status == "failed" and "PROFILE_PRIVATE" in result.error


def test_default_table_is_regular_for_group_ironman(monkeypatch):
    from tracker.config import Settings

    monkeypatch.delenv("HISCORE_TABLE", raising=False)
    assert Settings(_env_file=None).hiscore_table == "hiscore"


@respx.mock
def test_regular_table_fallback_keeps_runemetrics_ranks(engine, settings):
    regular = settings.model_copy(update={"hiscore_table": "hiscore"})
    respx.get(hiscores_url("hiscore", "Get Grouped")).mock(return_value=httpx.Response(503))
    data = load_json("runemetrics_profile_synthetic.json")
    respx.get(PROFILE).mock(return_value=httpx.Response(200, json=data))
    with httpx.Client() as client:
        run_xp_job(engine, regular, client, RunResult(), now=NOW)
    with engine.connect() as conn:
        rank = conn.execute(select(XpSnapshot.rank).where(XpSnapshot.skill_id == 0)).scalar()
    assert rank == data["skillvalues"][0]["rank"]
