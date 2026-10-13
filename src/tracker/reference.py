"""Static RS3 reference data: skills, XP curve and bosses."""

import math
from dataclasses import dataclass

OVERALL_ID = -1

# RuneMetrics skill ids. The hiscores lite CSV lists Overall first, then skills in this same order,
# so hiscores line index == skill id + 1. Verified against fixtures in tests/fixtures.
SKILLS: dict[int, str] = {
    0: "Attack", 1: "Defence", 2: "Strength", 3: "Constitution", 4: "Ranged",
    5: "Prayer", 6: "Magic", 7: "Cooking", 8: "Woodcutting", 9: "Fletching",
    10: "Fishing", 11: "Firemaking", 12: "Crafting", 13: "Smithing", 14: "Mining",
    15: "Herblore", 16: "Agility", 17: "Thieving", 18: "Slayer", 19: "Farming",
    20: "Runecrafting", 21: "Hunter", 22: "Construction", 23: "Summoning",
    24: "Dungeoneering", 25: "Divination", 26: "Invention", 27: "Archaeology",
    28: "Necromancy",
}
ALL_SKILLS: dict[int, str] = {OVERALL_ID: "Overall", **SKILLS}
SKILL_BY_NAME: dict[str, int] = {name.lower(): sid for sid, name in ALL_SKILLS.items()}
# Invention is an "elite" skill with its own XP curve, which is not tabulated here, so the
# next-level helpers return None for it rather than guess.
ELITE_SKILLS = {26}
MAX_XP = 200_000_000
MAX_VIRTUAL_LEVEL = 120


def _standard_xp_table(max_level: int = MAX_VIRTUAL_LEVEL) -> list[int]:
    """table[L] = XP required for level L (index 0 unused). Standard RuneScape formula."""
    table = [0, 0]
    points = 0
    for lvl in range(1, max_level):
        points += math.floor(lvl + 300 * 2 ** (lvl / 7))
        table.append(points // 4)
    return table


XP_TABLE = _standard_xp_table()
assert XP_TABLE[99] == 13_034_431 and XP_TABLE[120] == 104_273_167


def virtual_level(xp: int) -> int:
    lvl = 1
    for candidate in range(2, MAX_VIRTUAL_LEVEL + 1):
        if xp >= XP_TABLE[candidate]:
            lvl = candidate
        else:
            break
    return lvl


@dataclass(frozen=True)
class LevelProgress:
    level: int
    next_level: int | None
    xp_to_next: int | None
    progress_pct: float | None


def level_progress(skill_id: int, xp: int) -> LevelProgress | None:
    if skill_id == OVERALL_ID or skill_id in ELITE_SKILLS:
        return None
    lvl = virtual_level(xp)
    if lvl >= MAX_VIRTUAL_LEVEL:
        return LevelProgress(lvl, None, None, 100.0)
    lo, hi = XP_TABLE[lvl], XP_TABLE[lvl + 1]
    return LevelProgress(lvl, lvl + 1, hi - xp, round(100 * (xp - lo) / (hi - lo), 2))


# Canonical RS3 boss names. Aliases map alternative/plural spellings seen in the adventure log
# (lower-cased) to the canonical name. Extend from the unparsed/unknown-boss admin report;
# bump parser.PARSER_VERSION when you do so `reparse` picks the change up.
BOSSES: list[str] = [
    "Amascut, the Devourer", "Ambassador", "Araxxi", "Arch-Glacor", "Barrows Brothers",
    "Chaos Elemental", "Commander Zilyana", "Corporeal Beast", "Croesus", "Dagannoth Kings",
    "Gate of Elidinis", "General Graardor", "Giant Mole", "Gregorovic", "Har-Aken", "Helwyr",
    "Hermod, the Spirit of War", "Kalphite King", "Kalphite Queen", "Kerapac, the bound",
    "King Black Dragon", "Kree'arra", "K'ril Tsutsaroth", "Legiones", "Nex",
    "Nex: Angel of Death", "Queen Black Dragon", "Raksha, the Shadow Colossus",
    "Rasial, the First Necromancer", "Rex Matriarchs", "Rise of the Six", "Sanctum Guardian",
    "Seiryu the Azure Serpent", "Solak", "Telos, the Warden", "The Magister", "Twin Furies",
    "TzKal-Zuk", "TzTok-Jad", "Vindicta & Gorvek", "Vorago", "Vorkath",
    "Zamorak, Lord of Chaos", "Zemouregal & Vorkath",
]
BOSS_ALIASES: dict[str, str] = {
    "kalphite kings": "Kalphite King",
    "kalphite queens": "Kalphite Queen",
    "king black dragons": "King Black Dragon",
    "queen black dragons": "Queen Black Dragon",
    "giant moles": "Giant Mole",
    "corporeal beasts": "Corporeal Beast",
    "chaos elementals": "Chaos Elemental",
    "the arch-glacor": "Arch-Glacor",
    "arch-glacors": "Arch-Glacor",
    "kerapac": "Kerapac, the bound",
    "kerapac, the bound": "Kerapac, the bound",
    "telos": "Telos, the Warden",
    "raksha": "Raksha, the Shadow Colossus",
    "rasial": "Rasial, the First Necromancer",
    "zamorak": "Zamorak, Lord of Chaos",
    "hermod": "Hermod, the Spirit of War",
    "hermod, the spirit of war": "Hermod, the Spirit of War",
    "the twin furies": "Twin Furies",
    "vindicta": "Vindicta & Gorvek",
    "vindicta and gorvek": "Vindicta & Gorvek",
    "the magisters": "The Magister",
    "magister": "The Magister",
    "the barrows brothers": "Barrows Brothers",
    "dagannoth king": "Dagannoth Kings",
    "nexes": "Nex",
    "rex matriarch": "Rex Matriarchs",
    "the rex matriarchs": "Rex Matriarchs",
    "legios": "Legiones",
    "legio": "Legiones",
    "jad": "TzTok-Jad",
    "zuk": "TzKal-Zuk",
    "amascut": "Amascut, the Devourer",
    "croesuses": "Croesus",
}
_BOSS_BY_LOWER = {b.lower(): b for b in BOSSES}


def normalise_boss(name: str) -> tuple[str, bool]:
    """Return (canonical_name, known). Unknown names are returned cleaned but unchanged."""
    cleaned = " ".join(name.strip().rstrip(".!").split())
    key = cleaned.lower()
    if key in BOSS_ALIASES:
        return BOSS_ALIASES[key], True
    if key in _BOSS_BY_LOWER:
        return _BOSS_BY_LOWER[key], True
    for strip_word in ("the ",):
        if key.startswith(strip_word) and key[len(strip_word):] in _BOSS_BY_LOWER:
            return _BOSS_BY_LOWER[key[len(strip_word):]], True
    # Plural fallbacks, only accepted when they land on a known canonical name.
    for suffix in ("es", "s"):
        if key.endswith(suffix) and key[: -len(suffix)] in _BOSS_BY_LOWER:
            return _BOSS_BY_LOWER[key[: -len(suffix)]], True
    return cleaned, False
