"""Ordered, table-driven rules that turn adventure log entries into structured events.

Each rule matches one field ("text" or "details") of an entry. Rules are tried in order and the
first match wins. Wording is based on the corpus in tests/fixtures/log_corpus.jsonl. Entries
marked "synthetic" there are hypotheses until confirmed against real log entries
(see docs/verification.md). When rules change, bump PARSER_VERSION and run `reparse`.
"""

import re
from dataclasses import dataclass
from typing import Callable

from ..reference import ALL_SKILLS, SKILL_BY_NAME, normalise_boss

PARSER_VERSION = "1.0.0"


@dataclass(frozen=True)
class ParsedEvent:
    event_type: str  # boss_kill | level_up | xp_milestone | quest_complete | drop | clue | pet | achievement | other | unparsed
    subject: str
    count: int = 1
    value: int | None = None
    known_subject: bool = True


Builder = Callable[[re.Match, str, str], ParsedEvent | None]


@dataclass(frozen=True)
class Rule:
    name: str
    field: str  # "text" | "details"
    pattern: re.Pattern
    build: Builder


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


_ARTICLE = r"(?:an?\s+|the\s+|some\s+)?"
_LEVEL_IN_DETAILS = _rx(r"\blevel\s+(?P<level>\d+)\b")


def _skill(name: str) -> tuple[str, bool]:
    sid = SKILL_BY_NAME.get(name.strip().lower())
    return (ALL_SKILLS[sid], True) if sid is not None else (name.strip(), False)


def _kill(m: re.Match, text: str, details: str) -> ParsedEvent:
    boss, known = normalise_boss(m["name"])
    count = int(m.groupdict().get("count") or 1)
    return ParsedEvent("boss_kill", boss, count=count, known_subject=known)


def _level_up(m: re.Match, text: str, details: str) -> ParsedEvent:
    skill, known = _skill(m["skill"])
    level = m.groupdict().get("level")
    if level is None:
        lm = _LEVEL_IN_DETAILS.search(details)
        level = lm["level"] if lm else None
    return ParsedEvent("level_up", skill, value=int(level) if level else None, known_subject=known)


def _xp_milestone(m: re.Match, text: str, details: str) -> ParsedEvent:
    skill, known = _skill(m["skill"])
    return ParsedEvent("xp_milestone", skill, value=int(m["xp"].replace(",", "")), known_subject=known)


def _simple(event_type: str, group: str = "subject", title: bool = False) -> Builder:
    def build(m: re.Match, text: str, details: str) -> ParsedEvent:
        subject = " ".join(m[group].strip().rstrip(".!").split())
        return ParsedEvent(event_type, subject.capitalize() if title else subject)
    return build


def _pet(m: re.Match, text: str, details: str) -> ParsedEvent | None:
    found = re.match(rf"^I found\s+{_ARTICLE}(?:pet\s+)?(?P<subject>.+?)[.!]?$", text, re.IGNORECASE)
    return ParsedEvent("pet", found["subject"].strip() if found else text.strip().rstrip("."))


RULES: list[Rule] = [
    # "I killed 11 Raksha, the Shadow Colossus." / "I killed  4 Kalphite Kings." / "I defeated 3 Vorago."
    Rule("kill_count", "text", _rx(r"^I\s+(?:killed|defeated)\s+(?P<count>\d+)\s+(?P<name>.+?)[.!]?$"), _kill),
    # "I killed a Vorago." / "I defeated the Kalphite King."
    Rule("kill_single", "text", _rx(rf"^I\s+(?:killed|defeated)\s+{_ARTICLE}(?P<name>[^\d].*?)[.!]?$"), _kill),
    # "Levelled up Herblore." + details "I levelled my Herblore skill, I am now level 85."
    Rule("level_up", "text", _rx(r"^Levelled up (?P<skill>[A-Za-z]+)[.!]?$"), _level_up),
    Rule("level_up_details", "details", _rx(r"^I levelled my (?P<skill>[A-Za-z]+) skill, I am now level (?P<level>\d+)"), _level_up),
    # "200000000XP in Divination" / "50000000 XP in Attack"
    Rule("xp_milestone", "text", _rx(r"^(?P<xp>[\d,]+)\s*XP in (?P<skill>[A-Za-z]+)[.!]?$"), _xp_milestone),
    # "Quest complete: Sliske's Endgame"
    Rule("quest", "text", _rx(r"^Quest complete:\s*(?P<subject>.+?)[.!]?$"), _simple("quest_complete")),
    Rule("quest_details", "details", _rx(r"^I (?:have )?completed the quest:?\s*(?P<subject>.+?)[.!]?$"), _simple("quest_complete")),
    # Pets: a "found" entry whose details mention a pet.
    Rule("pet", "details", _rx(r"\b(?:pet|following me)\b"), _pet),
    # "Completed a hard Treasure Trail." / "Elite treasure trail completed" / "...master clue..."
    Rule("clue", "text", _rx(r"\b(?P<subject>easy|medium|hard|elite|master)\s+(?:treasure trail|clue)"), _simple("clue", title=True)),
    # "I found a Saradomin hilt" / "Item found: a dragon platelegs" / "I found a pair of Ancient Gauntlets"
    Rule("drop_found", "text", _rx(rf"^I found\s+{_ARTICLE}(?P<subject>.+?)[.!]?$"), _simple("drop")),
    Rule("drop_item_found", "text", _rx(rf"^Item found:\s*{_ARTICLE}(?P<subject>.+?)[.!]?$"), _simple("drop")),
    # "Total levels gained 2500" style milestones and other achievements
    Rule("total_level", "text", _rx(r"^Total levels? (?:gained|reached)?\s*(?P<subject>\d+)"), _simple("achievement")),
    Rule("achievement", "text", _rx(r"^(?:Achievement unlocked:|Unlocked the|Completed)\s*(?P<subject>.+?)[.!]?$"), _simple("achievement")),
    Rule("citadel", "text", _rx(r"^(?P<subject>Visited my Clan Citadel)"), _simple("other")),
]
