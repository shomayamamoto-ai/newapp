"""Database engine / session management."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
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


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    Base.metadata.create_all(get_engine())


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
