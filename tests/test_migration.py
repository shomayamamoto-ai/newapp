"""移行ファイル。

いちばん守りたいのは「認証情報が1つも出ていかない」ことで、次が
「書き出して取り込めば、元と同じデータに戻る」こと。どちらも、秘密を
わざと仕込んだ実際のデータで確かめている。
"""

import csv
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from snsauto.config import Settings
from snsauto.migration import (
    FILE_COLUMNS, SECRET_COLUMNS, MigrationError, export_bundle, import_bundle,
    read_manifest, rules,
)
from snsauto.models import (
    Base, Job, JobStatus, MetricSnapshot, PdcaCycle, PdcaStage, Platform, Project, Publication,
    PublicationStatus, Render, Script, SocialAccount, Storyboard, User,
)

# 仕込む秘密。どれか1つでも移行ファイルに出たら失敗。
PAGE_TOKEN = "EAAGpagetokenSECRET0001"
USER_TOKEN = "EAAGusertokenSECRET0002"        # meta.user_token に入る
REFRESH = "1//refreshSECRET0003"
API_KEY = "AIzaSyAPIKEYSECRET0004"             # 設定の YouTube API キー
PASSWORD_HASH = "scrypt$16384$8$1$deadbeef$cafebabeSECRET0005"


def _new_session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, SNSAUTO_WORKSPACE=str(tmp_path / "ws"),
                    YOUTUBE_API_KEY=API_KEY)


@pytest.fixture
def source(tmp_path):
    """2社分のデータ。秘密を列・JSON・エラー文の3か所に仕込んである。"""
    session = _new_session()
    now = datetime.now(timezone.utc)

    keio = Project(name="showstagram.keio", description="慶應の学生",
                   brand_profile={"tone": "親しみやすく",
                                  "notation": {"アストラ": "Astra"},
                                  "notation_exceptions": ["プレミアリーグ"]})
    other = Project(name="他社", description="別の顧客", brand_profile={})
    session.add_all([keio, other])
    session.flush()

    instagram = SocialAccount(
        project_id=keio.id, platform=Platform.INSTAGRAM, external_id="1784",
        display_name="showstagram", username="showstagram.keio",
        access_token=PAGE_TOKEN, refresh_token=REFRESH, scopes=["instagram_basic"],
        expires_at=now + timedelta(days=40), is_active=True,
        meta={"page_id": "99", "user_token": USER_TOKEN, "profile": {"followers": 12480}},
    )
    shared = SocialAccount(project_id=None, platform=Platform.YOUTUBE, external_id="UC1",
                           display_name="共有チャンネル", access_token="sharedSECRET0006",
                           is_active=True)
    rival = SocialAccount(project_id=other.id, platform=Platform.X, external_id="x1",
                          display_name="他社の公式", access_token="otherSECRET0007",
                          is_active=True)
    session.add_all([instagram, shared, rival])
    session.add(User(email="admin@example.com", name="管理者", password_hash=PASSWORD_HASH,
                     role="admin", is_active=True))
    session.flush()

    script = Script(project_id=keio.id, title="低単価案件の見分け方", platform=Platform.INSTAGRAM,
                    target_duration_sec=28.0, hook="なぜ？",
                    lines=[{"index": 0, "start": 0.0, "end": 3.0, "telop": "Astraで編集"}],
                    hashtags=["#動画編集"], notation=["アストラ→Astra"])
    session.add(script)
    session.flush()
    board = Storyboard(script_id=script.id)
    session.add(board)
    session.flush()
    video = tmp_path / "render.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 512)
    render = Render(storyboard_id=board.id, path=str(video), duration_sec=27.4,
                    width=1080, height=1920, meta={"telop_cues": 9})
    session.add(render)
    session.flush()

    published = Publication(
        project_id=keio.id, render_id=render.id, script_id=script.id,
        account_id=instagram.id, platform=Platform.INSTAGRAM,
        status=PublicationStatus.PUBLISHED, caption="日本語のキャプション",
        hashtags=["#慶應"], external_id="ig1", published_at=now - timedelta(days=2),
    )
    # 共有アカウントで投稿した記録。1社分の書き出しでは参照先が範囲外になる。
    via_shared = Publication(
        project_id=keio.id, account_id=shared.id, platform=Platform.YOUTUBE,
        status=PublicationStatus.FAILED, caption="共有で投稿",
        # httpx の例外文は URL を含む。クエリにトークンが乗っていれば一緒に残る。
        error=("ConnectError: https://graph.facebook.com/v21.0/1784/media"
               f"?access_token={PAGE_TOKEN}&fields=id"),
    )
    session.add_all([published, via_shared])
    session.flush()
    session.add(MetricSnapshot(publication_id=published.id, views=88412, likes=1840,
                               saves=1204, retention_rate=0.663,
                               captured_at=now - timedelta(days=1)))
    session.add(PdcaCycle(project_id=keio.id, title="冒頭を疑問形に", stage=PdcaStage.CHECK,
                          target={"metric": "retention_rate", "target": 0.06}))
    session.add(Job(project_id=keio.id, kind="research", status=JobStatus.SUCCEEDED,
                    params={"keyword": "副業", "api_key": API_KEY},
                    log=[f"GET https://www.googleapis.com/youtube/v3/search?key={API_KEY}"]))
    session.flush()
    session.commit()
    yield session
    session.close()


def _bundle(path):
    with zipfile.ZipFile(path) as bundle:
        return {name: bundle.read(name) for name in bundle.namelist()}


class TestNoSecretLeaves:
    @pytest.mark.parametrize("secret", [
        PAGE_TOKEN, USER_TOKEN, REFRESH, API_KEY, PASSWORD_HASH,
        "sharedSECRET0006", "otherSECRET0007",
    ])
    def test_the_secret_appears_in_no_file(self, source, settings, tmp_path, secret):
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        for name, body in _bundle(path).items():
            assert secret.encode() not in body, f"{name} に {secret[:10]}… が残っている"

    def test_the_token_hidden_inside_meta_is_removed(self, source, settings, tmp_path):
        """列だけ落とすと meta.user_token から長期トークンが漏れる。"""
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        accounts = json.loads(_bundle(path)["data/social_accounts.json"])
        meta = next(a for a in accounts if a["external_id"] == "1784")["meta"]
        assert "user_token" not in meta
        assert meta["page_id"] == "99"                      # 秘密でないものは残る
        assert meta["profile"] == {"followers": 12480}

    def test_a_token_inside_an_error_url_is_masked(self, source, settings, tmp_path):
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        rows = json.loads(_bundle(path)["data/publications.json"])
        error = next(r["error"] for r in rows if r["error"])
        assert "access_token=***" in error
        assert "graph.facebook.com" in error                 # 何が起きたかは読める

    def test_the_final_check_stops_a_leak_the_scrubbers_missed(
        self, source, settings, tmp_path, monkeypatch
    ):
        """伏せる処理に穴があっても、照合で止まる。消し忘れのファイルも残さない。"""
        import snsauto.migration as migration

        monkeypatch.setattr(migration, "_SECRET_IN_TEXT", ())    # 文字列の伏せ字を無効化
        destination = tmp_path / "m.zip"
        with pytest.raises(MigrationError) as caught:
            export_bundle(source, destination, settings=settings)
        assert "中止" in str(caught.value)
        assert PAGE_TOKEN not in str(caught.value)                # 値は表示しない
        assert not destination.exists()

    def test_analysis_words_named_like_tokens_survive(self, source, settings, tmp_path):
        """文字解析の tokens や token_count まで落とすと、分析結果が壊れる。"""
        from snsauto.migration import _scrub

        removed = []
        kept = _scrub({"tokens": ["副業", "始め方"], "token_count": 2,
                       "access_token": "x", "client_secret": "y"}, removed)
        assert kept == {"tokens": ["副業", "始め方"], "token_count": 2}
        assert sorted(removed) == ["x", "y"]

    def test_the_bundle_is_readable_only_by_its_owner(self, source, settings, tmp_path):
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        assert path.stat().st_mode & 0o077 == 0


class TestRoundTrip:
    def _counts(self, session):
        return {t.name: session.execute(select(func.count()).select_from(t)).scalar()
                for t in Base.metadata.sorted_tables}

    def test_every_row_comes_back(self, source, settings, tmp_path):
        path, manifest = export_bundle(source, tmp_path / "m.zip", settings=settings)
        target = _new_session()
        result = import_bundle(target, path, settings)
        assert self._counts(target) == self._counts(source)
        assert result.tables == manifest.tables

    def test_values_come_back_exactly(self, source, settings, tmp_path):
        """日本語・JSON・列挙型・日時・小数が、型ごと元に戻る。"""
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        target = _new_session()
        import_bundle(target, path, settings)

        project = target.query(Project).filter_by(name="showstagram.keio").one()
        assert project.brand_profile["notation"] == {"アストラ": "Astra"}
        script = target.query(Script).one()
        assert script.lines[0]["telop"] == "Astraで編集"
        assert script.platform is Platform.INSTAGRAM
        publication = target.query(Publication).filter_by(external_id="ig1").one()
        assert publication.status is PublicationStatus.PUBLISHED
        original = source.query(Publication).filter_by(external_id="ig1").one()
        assert publication.published_at.replace(tzinfo=None) == \
            original.published_at.replace(tzinfo=None)
        snapshot = target.query(MetricSnapshot).one()
        assert snapshot.retention_rate == pytest.approx(0.663)

    def test_accounts_arrive_needing_reconnection(self, source, settings, tmp_path):
        """トークンは持ち出していない。使える状態に見せてはいけない。"""
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        target = _new_session()
        result = import_bundle(target, path, settings)
        accounts = target.query(SocialAccount).all()
        assert accounts and all(not a.is_active for a in accounts)
        assert all("再連携" in (a.refresh_error or "") for a in accounts)
        assert result.accounts_to_reconnect == len(accounts)

    def test_users_cannot_sign_in_until_they_reset(self, source, settings, tmp_path):
        from snsauto.web.auth import verify_password

        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        target = _new_session()
        result = import_bundle(target, path, settings)
        user = target.query(User).one()
        assert not verify_password("anything", user.password_hash)
        assert result.users_to_reset == ["admin@example.com"]

    def test_media_travels_and_paths_are_rewritten(self, source, settings, tmp_path):
        path, manifest = export_bundle(source, tmp_path / "m.zip", with_media=True,
                                       settings=settings)
        assert len(manifest.media) == 1 and manifest.media[0]["sha256"]
        target = _new_session()
        result = import_bundle(target, path, settings)
        render = target.query(Render).one()
        assert result.media_copied == 1
        assert render.path.startswith(str(settings.workspace.resolve()))
        assert open(render.path, "rb").read().startswith(b"\x00\x00\x00\x18ftyp")

    def test_without_media_the_missing_files_are_counted(self, source, settings, tmp_path):
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        source.query(Render).one()
        (tmp_path / "render.mp4").unlink()                # 移行先には実物が無い
        result = import_bundle(_new_session(), path, settings)
        assert result.missing_files == 1


class TestImportRefuses:
    def test_a_database_that_already_has_data(self, source, settings, tmp_path):
        """IDが重なると、別の顧客のデータにつながる。"""
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        target = _new_session()
        target.add(Project(name="既存", brand_profile={}))
        target.commit()
        with pytest.raises(MigrationError) as caught:
            import_bundle(target, path, settings)
        assert "空ではありません" in str(caught.value)
        assert target.query(Project).count() == 1           # 何も入っていない

    def test_a_zip_that_writes_outside_its_folder(self, tmp_path, settings):
        evil = tmp_path / "evil.zip"
        with zipfile.ZipFile(evil, "w") as bundle:
            bundle.writestr("manifest.json", "{}")
            bundle.writestr("../../escaped.txt", "x")
        with pytest.raises(MigrationError):
            import_bundle(_new_session(), evil, settings)
        assert not (tmp_path.parent / "escaped.txt").exists()

    def test_something_that_is_not_a_bundle(self, tmp_path, settings):
        other = tmp_path / "other.zip"
        with zipfile.ZipFile(other, "w") as bundle:
            bundle.writestr("hello.txt", "x")
        with pytest.raises(MigrationError):
            read_manifest(other)


class TestRunFromAnywhere:
    def test_import_works_outside_the_repository(self, source, settings, tmp_path, monkeypatch):
        """alembic.ini の script_location は相対パス。そのまま読むと、リポジトリ
        以外のフォルダから実行したときだけ取り込みが失敗していた。"""
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        result = import_bundle(_new_session(), path, settings)
        assert result.tables["projects"] == 2


class TestOneClientOnly:
    """代理店で、1社分だけを渡す・移すとき。"""

    def test_other_clients_are_not_included(self, source, settings, tmp_path):
        keio = source.query(Project).filter_by(name="showstagram.keio").one()
        path, manifest = export_bundle(source, tmp_path / "m.zip", project_id=keio.id,
                                       settings=settings)
        files = _bundle(path)
        projects = json.loads(files["data/projects.json"])
        assert [p["name"] for p in projects] == ["showstagram.keio"]
        body = b"".join(files.values())
        assert "他社の公式".encode() not in body
        # 複数の顧客で共有しているアカウントも、1社分には入れない
        assert "共有チャンネル".encode() not in body

    def test_staff_and_the_shared_library_stay_behind(self, source, settings, tmp_path):
        keio = source.query(Project).filter_by(name="showstagram.keio").one()
        _, manifest = export_bundle(source, tmp_path / "m.zip", project_id=keio.id,
                                    settings=settings)
        assert manifest.tables["users"] == 0
        assert manifest.tables["clip_assets"] == 0

    def test_a_reference_to_a_shared_account_is_detached_and_counted(
        self, source, settings, tmp_path
    ):
        keio = source.query(Project).filter_by(name="showstagram.keio").one()
        path, manifest = export_bundle(source, tmp_path / "m.zip", project_id=keio.id,
                                       settings=settings)
        assert manifest.detached == {"publications.account_id": 1}
        # 範囲外を指したまま入れると、取り込み先で外部キーが壊れる
        target = _new_session()
        import_bundle(target, path, settings)
        assert target.query(Publication).filter_by(caption="共有で投稿").one().account_id is None

    def test_the_child_rows_follow_their_project(self, source, settings, tmp_path):
        keio = source.query(Project).filter_by(name="showstagram.keio").one()
        _, manifest = export_bundle(source, tmp_path / "m.zip", project_id=keio.id,
                                    settings=settings)
        for name in ("scripts", "storyboards", "renders", "publications",
                     "metric_snapshots", "pdca_cycles"):
            assert manifest.tables[name] > 0, name


class TestForOtherTools:
    def test_csv_opens_in_excel_without_mojibake(self, source, settings, tmp_path):
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        raw = _bundle(path)["csv/projects.csv"]
        assert raw.startswith(b"\xef\xbb\xbf")                # BOM が無いと Excel で化ける
        rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
        keio = next(r for r in rows if r["name"] == "showstagram.keio")
        assert json.loads(keio["brand_profile"])["notation"] == {"アストラ": "Astra"}

    def test_csv_never_has_the_secret_columns(self, source, settings, tmp_path):
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        header = _bundle(path)["csv/social_accounts.csv"].decode("utf-8-sig").splitlines()[0]
        for column in SECRET_COLUMNS["social_accounts"]:
            assert column not in header.split(",")

    @pytest.mark.parametrize("dialect", ["postgresql", "mysql", "sqlite"])
    def test_ddl_is_shipped_for_each_database(self, source, settings, tmp_path, dialect):
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        ddl = _bundle(path)[f"schema/{dialect}.sql"].decode()
        assert ddl.count("CREATE TABLE") == len(Base.metadata.sorted_tables)

    def test_the_sqlite_ddl_actually_builds_the_schema(self, source, settings, tmp_path):
        """書いてあるだけでなく、流せば表ができる。"""
        import sqlite3

        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        connection = sqlite3.connect(":memory:")
        connection.executescript(_bundle(path)["schema/sqlite.sql"].decode())
        names = {r[0] for r in connection.execute("select name from sqlite_master where type='table'")}
        assert names == {t.name for t in Base.metadata.sorted_tables}

    def test_the_rules_are_read_from_the_code(self):
        """書いた数字とツールの動きが食い違わないこと。"""
        from snsauto.media import trim

        assert rules()["無音カット"]["MIN_GAP_SEC"]["value"] == trim.MIN_GAP_SEC
        assert rules()["無音カット"]["HANDLE_SEC"]["value"] == trim.HANDLE_SEC

    def test_the_readme_explains_what_is_missing_and_why(self, source, settings, tmp_path):
        path, _ = export_bundle(source, tmp_path / "m.zip", settings=settings)
        readme = _bundle(path)["README.md"].decode()
        assert "access_token" in readme and "連携し直してください" in readme
        assert "snsauto migrate import" in readme


class TestNothingNewSlipsThrough:
    """列が増えたときに、秘密やファイルの列を見落とさないための見張り。"""

    def test_every_secret_looking_column_is_excluded(self):
        import re

        pattern = re.compile(r"(token|secret|password|passwd|api_?key)", re.I)
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if pattern.search(column.name):
                    assert column.name in SECRET_COLUMNS.get(table.name, ()), \
                        f"{table.name}.{column.name} を SECRET_COLUMNS に入れてください"

    def test_every_file_column_is_known(self):
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if column.name.endswith("path"):
                    assert column.name in FILE_COLUMNS.get(table.name, ()), \
                        f"{table.name}.{column.name} を FILE_COLUMNS に入れてください"
