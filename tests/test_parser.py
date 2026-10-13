import json

import pytest

from tests.conftest import FIXTURES
from tracker.parser.engine import parse_entry
from tracker.reference import normalise_boss

CORPUS = [json.loads(line) for line in (FIXTURES / "log_corpus.jsonl").read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("entry", CORPUS, ids=[e["text"][:40] for e in CORPUS])
def test_corpus_entry(entry):
    events = parse_entry(entry["text"], entry["details"])
    assert len(events) == 1
    ev = events[0]
    exp = entry["expected"]
    assert (ev.event_type, ev.subject, ev.count, ev.value) == (
        exp["event_type"], exp["subject"], exp["count"], exp["value"]
    )


def test_unparsed_rate_under_5_percent():
    unparsed = sum(1 for e in CORPUS if parse_entry(e["text"], e["details"])[0].event_type == "unparsed")
    assert unparsed / len(CORPUS) < 0.05


def test_unmatched_entry_is_unparsed_with_raw_text():
    ev = parse_entry("Something brand new happened", "Details here")[0]
    assert ev.event_type == "unparsed" and ev.subject == "Something brand new happened" and not ev.known_subject


def test_parser_is_deterministic():
    for e in CORPUS:
        assert parse_entry(e["text"], e["details"]) == parse_entry(e["text"], e["details"])


def test_unknown_boss_kept_and_flagged():
    ev = parse_entry("I killed 3 Brand New Bosses.", "")[0]
    assert ev.event_type == "boss_kill" and ev.count == 3 and ev.subject == "Brand New Bosses"
    assert not ev.known_subject


@pytest.mark.parametrize("raw,expected", [
    ("Kalphite Kings", "Kalphite King"),
    ("raksha, the shadow colossus.", "Raksha, the Shadow Colossus"),
    ("The Magister", "The Magister"),
    ("Vorkaths", "Vorkath"),
])
def test_normalise_boss(raw, expected):
    assert normalise_boss(raw) == (expected, True)
