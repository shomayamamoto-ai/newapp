"""Database engine / session management."""

from __future__ import annotations

import logging
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

log = logging.getLogger(__name__)

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


# Tokens are stored unencrypted by deliberate choice, which makes the file
# permissions the only thing protecting them. So they are set rather than
# inherited from whatever umask the process happened to start with.
DB_FILE_MODE = 0o600


def sqlite_path(url: str | None = None) -> Path | None:
    """The database file, when the database is a local SQLite file."""
    url = url or get_settings().db_url
    if not url.startswith("sqlite"):
        return None
    _, _, tail = url.partition("///")
    return Path(tail) if tail else None


def secure_db_file(url: str | None = None) -> str | None:
    """Restrict the database file to its owner. Returns what changed, if any.

    Called on every init because a restore from backup, a container volume
    mount, or a file copied with cp -p can all reintroduce a readable mode
    long after the first setup.
    """
    path = sqlite_path(url)
    if path is None or not path.exists():
        return None
    try:
        current = stat.S_IMODE(path.stat().st_mode)
        if current & 0o077:
            path.chmod(DB_FILE_MODE)
            return f"{oct(current)} -> {oct(DB_FILE_MODE)}"
    except OSError as exc:  # pragma: no cover - platform dependent
        log.warning("could not secure %s: %s", path, exc)
    return None


def db_permission_warning(url: str | None = None) -> str | None:
    """A line for the UI when the database is readable by others.

    It holds every connected account's access token in the clear, so anyone
    who can read the file can post as those accounts. Running an agency, those
    are clients' accounts rather than your own.
    """
    path = sqlite_path(url)
    if path is None or not path.exists():
        return None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if not mode & 0o077:
        return None
    return (
        f"データベース {path} が所有者以外から読める状態です（{oct(mode)}）。"
        "連携アカウントのトークンは暗号化せずに保存しているため、"
        "このファイルを読めれば各アカウントとして投稿できます。"
        f"`chmod 600 {path}` を実行してください。"
    )


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
        secure_db_file()
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
        secure_db_file()
        return
    command.upgrade(config, "head")
    secure_db_file()


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
