from tracker.reference import XP_TABLE, level_progress, virtual_level


def test_xp_table_known_points():
    assert XP_TABLE[2] == 83
    assert XP_TABLE[92] == 6_517_253
    assert XP_TABLE[99] == 13_034_431
    assert XP_TABLE[120] == 104_273_167


def test_virtual_level_and_progress():
    assert virtual_level(0) == 1
    assert virtual_level(13_034_431) == 99
    assert virtual_level(200_000_000) == 120
    p = level_progress(0, 83)
    assert p.level == 2 and p.next_level == 3 and p.progress_pct == 0.0
    assert level_progress(26, 1_000_000) is None  # Invention: elite curve not tabulated
    assert level_progress(0, 200_000_000).next_level is None
