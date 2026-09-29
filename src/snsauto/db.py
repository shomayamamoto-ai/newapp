"""Database engine / session management."""

from __future__ import annotations

import logging
import os
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

# init_db() がこのプロセスで新しく作ったDBファイル。無ければ None。
created_database: Path | None = None
_Session: sessionmaker[Session] | None = None


class DatabaseLocationError(RuntimeError):
    """DBファイルを置けない場所が指定されている。"""


def _prepare_sqlite_dir(path: Path) -> None:
    """DBファイルの置き場所を用意し、書けなければ理由と直し方を出す。

    何もしないと、SQLite の "unable to open database file" や
    FileExistsError がそのまま出て、原因が「DBの場所」だと分からない。
    同梱の systemd ユニットは ProtectSystem=strict で /opt/snsauto/data しか
    書けないため、DBの場所を相対パスのままにするとここで止まる。
    """
    folder = path.parent
    location = folder.resolve() if folder.exists() else folder.absolute()
    hint = (
        "SNSAUTO_DB_URL に、書き込めるフォルダの絶対パスを指定してください"
        "（同梱の systemd ユニットでは sqlite:////opt/snsauto/data/snsauto.db）。"
    )
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as exc:
        raise DatabaseLocationError(
            f"データベースの置き場所 {location} がフォルダではありません。{hint}"
        ) from exc
    except PermissionError as exc:
        raise DatabaseLocationError(
            f"データベースの置き場所 {location} を作成できません（権限がありません）。{hint}"
        ) from exc
    except OSError as exc:
        # 読み取り専用のファイルシステム（ProtectSystem=strict を含む）
        raise DatabaseLocationError(
            f"データベースの置き場所 {location} に書き込めません（{exc.strerror}）。{hint}"
        ) from exc
    if not path.exists() and not os.access(folder, os.W_OK):
        raise DatabaseLocationError(
            f"データベースの置き場所 {location} に書き込めません。{hint}"
        )


def get_engine():
    global _engine, _Session
    if _engine is None:
        settings = get_settings()
        url = settings.db_url
        if url.startswith("sqlite:///"):
            _prepare_sqlite_dir(Path(url.removeprefix("sqlite:///")))
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


def database_label(url: str | None = None) -> str:
    """どのDBを見ているかを、人が読める形で。

    SQLite は絶対パスにする。既定値は起動したフォルダからの相対パスなので、
    相対のまま出しても「どこの workspace か」が分からない。それ以外の URL は
    パスワードを伏せる。
    """
    url = url or get_settings().db_url
    path = sqlite_path(url)
    if path is not None:
        return str(path.resolve())
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - 読めないURLでも、表示のために落とさない
        return "（読み取れないURL）"


def new_database_notice() -> str | None:
    """このプロセスで空のDBを作ったなら、その旨と場所。"""
    if created_database is None:
        return None
    return (
        f"新しいデータベースを作成しました: {created_database}\n"
        "以前のデータが見えない場合は、別のフォルダから起動しているか、"
        "SNSAUTO_DB_URL の名前・値が違っています。常に同じDBを使うには、"
        "SNSAUTO_DB_URL を絶対パスで指定してください"
        "（例: sqlite:////srv/snsauto/snsauto.db）。"
    )


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
    global created_database
    path = sqlite_path()
    was_missing = path is not None and not path.exists()
    try:
        _init_schema()
    finally:
        # この起動で空のDBが新しくできた。初回なら正常だが、別のフォルダから
        # 起動した・DBの場所の設定名を打ち間違えた、でも同じことが起き、
        # その場合は既存のデータがすべて消えたように見える。区別はここでは
        # できないので、場所を明示して知らせる（new_database_notice）。
        # 実際にできたときだけ。作れずに失敗したのに「作成しました」と
        # 出すと、原因の表示と矛盾する。
        if was_missing and path is not None and path.exists():
            created_database = path.resolve()


def _init_schema() -> None:
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
