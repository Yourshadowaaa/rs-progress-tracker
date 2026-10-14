import json
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent
TEST_DB = os.environ.get("TEST_DATABASE_URL")


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def load_json(name: str):
    return json.loads(load_fixture(name))


@pytest.fixture(scope="session")
def migrated_engine():
    """Postgres integration tests need TEST_DATABASE_URL (a disposable database)."""
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    url = TEST_DB.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def engine(migrated_engine):
    with migrated_engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE adventure_events, adventure_log_raw, xp_snapshots, ingestion_runs, players, "
            "comp_completions, comp_requirements, wiki_pages RESTART IDENTITY CASCADE"
        ))
    return migrated_engine


@pytest.fixture
def settings():
    from tracker.config import Settings
    return Settings(database_url=TEST_DB or "postgresql://x/y", player_name="Get Grouped",
                    hiscore_table="hiscore_ironman", http_retries=1)
