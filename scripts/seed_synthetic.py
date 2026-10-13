"""Fill a DEVELOPMENT database with one year of synthetic hourly data (performance + UI checks).

    DATABASE_URL=postgresql://localhost/rs_dev uv run python scripts/seed_synthetic.py --yes-this-is-a-dev-db

Never run against the Railway production database: it inserts fake history.
"""

import random
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from tracker.clients.runemetrics import Activity
from tracker.config import get_settings
from tracker.db import get_engine
from tracker.jobs.adventure_log import store_activities
from tracker.jobs.runs import RunResult

if "--yes-this-is-a-dev-db" not in sys.argv:
    sys.exit(__doc__)

settings = get_settings()
engine = get_engine()
with engine.begin() as conn:
    conn.execute(text("INSERT INTO players (display_name) VALUES (:n) ON CONFLICT DO NOTHING"), {"n": settings.player_name})
    pid = conn.execute(text("SELECT id FROM players WHERE display_name = :n"), {"n": settings.player_name}).scalar()
    # Each skill grows by a random-walk amount per hour; Overall is the sum.
    conn.execute(text("""
        INSERT INTO xp_snapshots (player_id, skill_id, captured_at_utc, xp, level, rank, source)
        SELECT :p, s.id, t,
               (5000000 + s.id * 300000 + SUM((random() * 4000 * (1 + (s.id % 5)))::bigint) OVER w)::bigint,
               99, 50000, 'synthetic'
        FROM skills s
        CROSS JOIN generate_series(date_trunc('hour', now()) - interval '365 days', date_trunc('hour', now()), interval '1 hour') t
        WHERE s.id >= 0
        WINDOW w AS (PARTITION BY s.id ORDER BY t)
        ON CONFLICT DO NOTHING"""), {"p": pid})
    conn.execute(text("""
        INSERT INTO xp_snapshots (player_id, skill_id, captured_at_utc, xp, level, rank, source)
        SELECT player_id, -1, captured_at_utc, SUM(xp), 2800, 40000, 'synthetic' FROM xp_snapshots
        WHERE player_id = :p AND skill_id >= 0 GROUP BY player_id, captured_at_utc
        ON CONFLICT DO NOTHING"""), {"p": pid})

random.seed(1)
bosses = ["Raksha, the Shadow Colossus", "Vorago", "Kalphite Kings", "Telos, the Warden", "Arch-Glacors",
          "Zamorak, Lord of Chaos", "Nex", "Rasial, the First Necromancer", "Croesus", "Vorkath", "Solak"]
now = datetime.now(UTC).replace(second=0, microsecond=0)
acts = []
for i in range(3000):
    t = now - timedelta(minutes=random.randint(0, 365 * 24 * 60))
    roll = random.random()
    if roll < 0.7:
        b = random.choice(bosses[: random.randint(1, len(bosses))])
        txt = f"I killed {random.randint(1, 15)} {b}."
    elif roll < 0.85:
        txt = f"Levelled up {random.choice(['Slayer', 'Necromancy', 'Herblore', 'Mining'])}."
    elif roll < 0.95:
        txt = f"I found a {random.choice(['Shadow spike', 'Dormant anima core helm', 'Ancient hilt'])}"
    else:
        txt = random.choice(["Something odd happened", "Visited my Clan Citadel."])
    acts.append(Activity(t, txt, txt + f" #{i}", {"synthetic": True}))
store_activities(engine, settings, acts, RunResult())
with engine.begin() as conn:
    conn.execute(text("INSERT INTO ingestion_runs (source, started_at, finished_at, status, rows_inserted) VALUES "
                      "('xp', now(), now(), 'ok', 30), ('adventure_log', now(), now(), 'ok', 3)"))
print("seeded")
