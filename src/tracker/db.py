from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Player


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().database_url, pool_pre_ping=True, pool_size=5)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on error."""
    factory = sessionmaker(bind=engine or get_engine(), expire_on_commit=False)
    with factory() as session:
        with session.begin():
            yield session


def get_or_create_player(session: Session, display_name: str) -> int:
    session.execute(
        insert(Player).values(display_name=display_name).on_conflict_do_nothing(index_elements=["display_name"])
    )
    return session.scalar(select(Player.id).where(Player.display_name == display_name))
