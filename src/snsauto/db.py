"""Database engine / session management."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_engine = None
_Session: sessionmaker[Session] | None = None


def get_engine():
    global _engine, _Session
    if _engine is None:
        settings = get_settings()
        url = settings.db_url
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, future=True)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _alembic_config():
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    config.set_main_option("sqlalchemy.url", get_settings().db_url)
    return config


def init_db() -> None:
    """Bring the schema up to date. Safe to call repeatedly.

    Uses Alembic when it is available, because a long-running install
    accumulates data that ``create_all`` cannot migrate - it creates missing
    tables but never adds a column to an existing one, which silently breaks
    the app after any schema change.

    A database created before migrations existed has tables but no version
    stamp; stamping it rather than upgrading avoids trying to re-create what
    is already there.
    """
    engine = get_engine()
    if not ALEMBIC_INI.exists():
        Base.metadata.create_all(engine)
        return

    try:
        from alembic import command
        from alembic.runtime.migration import MigrationContext
    except ImportError:
        Base.metadata.create_all(engine)
        return

    config = _alembic_config()
    with engine.connect() as connection:
        stamped = MigrationContext.configure(connection).get_current_revision()
        has_tables = inspect(engine).has_table("projects")

    if stamped is None and has_tables:
        command.stamp(config, "head")
        return
    command.upgrade(config, "head")


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _Session is not None
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
