"""どのデータを見ているか。

既定のDBと作業フォルダは起動したフォルダからの相対パスで、設定名の
打ち間違いは黙って無視される。どちらも結果は同じで、既存のデータが
すべて消えたように見える。予約投稿のワーカーと Web UI が別の場所から
起動されていれば、予約は永遠に投稿されない。
"""

import subprocess
import sys
from pathlib import Path

import pytest


from snsauto.config import known_setting_names, unknown_settings


class TestMisspelledSettings:
    def test_the_misspelling_that_actually_happened_is_caught(self):
        found = unknown_settings({"SNSAUTO_DATABASE_URL": "x"}, env_file=None)
        assert found == [("SNSAUTO_DATABASE_URL", "SNSAUTO_DB_URL", "環境変数")]

    def test_a_near_miss_outside_our_prefix_is_caught(self):
        """YOUTUBE_CLIENT_SECRET の打ち間違いは「未設定」と表示され、原因が
        分からない。接頭辞に関係なく拾う。"""
        found = unknown_settings({"YOUTUBE_CLIENT_SECERT": "x"}, env_file=None)
        assert found[0][1] == "YOUTUBE_CLIENT_SECRET"

    def test_unrelated_environment_variables_are_left_alone(self):
        environ = {"PATH": "/bin", "HOME": "/root", "LANG": "C",
                   "OTHER_TOOL_TOKEN": "t", "DATABASE_URL": "other-app"}
        assert unknown_settings(environ, env_file=None) == []

    def test_correct_names_are_not_reported(self):
        environ = {name: "x" for name in known_setting_names()}
        assert unknown_settings(environ, env_file=None) == []

    def test_env_file_typos_are_caught(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FACEBOOK_APP_SECRT=x\nSNSAUTO_DB_URL=ok\n", encoding="utf-8")
        found = unknown_settings({}, env_file=env)
        assert [key for key, *_ in found] == ["FACEBOOK_APP_SECRT"]

    def test_doctor_lists_every_unknown_env_file_key(self, tmp_path):
        """.env を他のツールと共有していても、起動のたびには騒がない。
        doctor では全部見せる。"""
        env = tmp_path / ".env"
        env.write_text("DATABASE_URL=other-app\nexport SNSAUTO_WORKSPCE=x\n", encoding="utf-8")
        quiet = [key for key, *_ in unknown_settings({}, env_file=env)]
        loud = [key for key, *_ in unknown_settings({}, env_file=env, strict=True)]
        assert quiet == ["SNSAUTO_WORKSPCE"]
        assert set(loud) == {"SNSAUTO_WORKSPCE", "DATABASE_URL"}


class TestTheDatabaseLabel:
    def test_sqlite_is_shown_as_an_absolute_path(self, tmp_path, monkeypatch):
        from snsauto.db import database_label

        monkeypatch.chdir(tmp_path)
        label = database_label("sqlite:///workspace/snsauto.db")
        assert Path(label).is_absolute()
        assert label == str(tmp_path / "workspace" / "snsauto.db")

    def test_a_password_is_never_printed(self):
        from snsauto.db import database_label

        label = database_label("postgresql://app:hunter2@db.internal:5432/snsauto")
        assert "hunter2" not in label
        assert "app:***@db.internal" in label


def _cli(args, cwd, env_extra=None):
    """インストール済みのコマンドと同じ入口（app()）で、別プロセスとして起動する。"""
    import os

    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SNSAUTO_") and k not in ("YOUTUBE_API_KEY",)}
    env.update(env_extra or {})
    code = "import sys; from snsauto.cli import app; sys.argv=['snsauto', *sys.argv[1:]]; app()"
    return subprocess.run(
        [sys.executable, "-c", code, *args], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=120,
    )


class TestStartingFromAnotherFolder:
    def test_an_empty_database_says_where_it_is_and_why(self, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        second.mkdir()
        _cli(["project", "create", "顧客A"], cwd=first)

        result = _cli(["project", "list"], cwd=second)
        output = result.stdout + result.stderr
        assert "新しいデータベースを作成しました" in output
        assert str(second / "workspace" / "snsauto.db") in output.replace("\n", "")
        assert "SNSAUTO_DB_URL" in output

    def test_an_existing_database_is_quiet(self, tmp_path):
        _cli(["project", "create", "顧客A"], cwd=tmp_path)
        result = _cli(["project", "list"], cwd=tmp_path)
        assert "新しいデータベース" not in result.stdout + result.stderr

    def test_a_misspelled_name_is_reported_on_every_run(self, tmp_path):
        result = _cli(["project", "list"], cwd=tmp_path,
                      env_extra={"SNSAUTO_DATABASE_URL": "sqlite:///x.db"})
        assert "SNSAUTO_DB_URL" in result.stderr
        # 警告は stderr。JSON 出力をパイプしている利用者の結果を汚さない。
        assert "SNSAUTO_DATABASE_URL" not in result.stdout


class TestTheEntryPointDoesNotMatter:
    """インストール済みスクリプトが app() を直接呼ぶ環境があった。

    pyproject の入口は main だが、入口を変える前にインストールされた環境の
    スクリプトは app() のまま。その場合、エラー説明も警告も一切出ていなかった。
    """

    def test_app_explains_errors_instead_of_tracing(self, tmp_path):
        result = _cli(["research", "run", "無い", "x"], cwd=tmp_path)
        output = result.stdout + result.stderr
        assert "Traceback" not in output
        assert result.returncode != 0

    def test_main_and_app_are_the_same_path(self):
        from snsauto import cli

        assert isinstance(cli.app, cli._SnsApp)
        assert "app()" in cli.main.__doc__ or cli.main.__code__.co_names == ("app",)


REPO = Path(__file__).resolve().parents[1]


def _unit(name):
    """systemd ユニットの Key=Value を、同じキーが複数あっても全部拾う。"""
    entries: dict[str, list[str]] = {}
    for line in (REPO / "deploy" / "systemd" / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            entries.setdefault(key, []).append(value)
    return entries


class TestTheShippedDeploymentStarts:
    """同梱の systemd 設定どおりに配備すると、起動できなかった。

    ProtectSystem=strict で書けるのは /opt/snsauto/data だけなのに、DB の
    場所は .env.example の相対パス（= /opt/snsauto/workspace）のままだった。
    """

    @pytest.mark.parametrize("name", ["snsauto-web.service", "snsauto-worker.service"])
    def test_the_data_lives_inside_the_writable_paths(self, name):
        unit = _unit(name)
        writable = unit["ReadWritePaths"][0].split()
        env = dict(item.split("=", 1) for item in unit.get("Environment", []))

        db = env["SNSAUTO_DB_URL"].removeprefix("sqlite:///")
        workspace = env["SNSAUTO_WORKSPACE"]
        assert Path(db).is_absolute() and Path(workspace).is_absolute()
        assert any(db.startswith(root + "/") for root in writable), db
        assert any(workspace.startswith(root + "/") for root in writable), workspace

    def test_web_and_worker_use_the_same_database(self):
        """違えば、Web で予約した投稿を worker が一生見つけられない。"""
        def env(name):
            return dict(item.split("=", 1) for item in _unit(name).get("Environment", []))

        web, worker = env("snsauto-web.service"), env("snsauto-worker.service")
        assert web["SNSAUTO_DB_URL"] == worker["SNSAUTO_DB_URL"]
        assert web["SNSAUTO_WORKSPACE"] == worker["SNSAUTO_WORKSPACE"]

    def test_the_env_template_does_not_override_the_units(self):
        """systemd では EnvironmentFile が Environment= を上書きする。

        .env.example に値が書いてあれば、コピーした .env がユニットの指定を
        潰し、書けない場所を指して起動できなくなる。
        """
        for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip()
            assert key not in ("SNSAUTO_DB_URL", "SNSAUTO_WORKSPACE"), line

    def test_compose_pins_both_paths_too(self):
        compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        assert "SNSAUTO_DB_URL: sqlite:////data/" in compose
        assert "SNSAUTO_WORKSPACE: /data/" in compose


class TestAnUnusableLocation:
    def test_it_says_what_and_how_to_fix(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("", encoding="utf-8")
        result = _cli(["project", "list"], cwd=tmp_path,
                      env_extra={"SNSAUTO_DB_URL": f"sqlite:///{blocker}/snsauto.db"})
        output = (result.stdout + result.stderr).replace("\n", "")
        assert result.returncode != 0
        assert "データベースを置けません" in output
        assert "SNSAUTO_DB_URL" in output
        assert "Traceback" not in output

    def test_a_failed_creation_is_not_announced_as_a_success(self, tmp_path):
        """作れなかったのに「作成しました」と出ると、原因の表示と矛盾する。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("", encoding="utf-8")
        result = _cli(["project", "list"], cwd=tmp_path,
                      env_extra={"SNSAUTO_DB_URL": f"sqlite:///{blocker}/snsauto.db"})
        assert "作成しました" not in result.stdout + result.stderr
