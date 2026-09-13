"""Workspace accounting and the manual clean. Nothing here runs on its own."""

import os
import stat

import pytest

from snsauto.workspace import (
    DISPOSABLE,
    apply_clean,
    plan_clean,
    total_bytes,
    usage,
    warning,
)


@pytest.fixture
def workspace(tmp_path):
    for name, size in [("research-media", 5_000_000), ("assets", 1_000_000),
                       ("renders", 3_000_000), ("reports", 100_000)]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "f.bin").write_bytes(b"\0" * size)
    return tmp_path


class TestUsage:
    def test_it_reports_each_category_largest_first(self, workspace):
        rows = usage(workspace)
        assert rows[0].category == "research-media"
        assert rows[0].megabytes == pytest.approx(4.8, abs=0.1)

    def test_finished_videos_are_not_marked_disposable(self, workspace):
        """For an agency these are the deliverable, not scratch."""
        by_name = {row.category: row for row in usage(workspace)}
        assert not by_name["renders"].disposable
        assert not by_name["reports"].disposable
        assert by_name["research-media"].disposable

    def test_a_missing_folder_is_zero_not_an_error(self, tmp_path):
        assert total_bytes(tmp_path) == 0


class TestPlanning:
    def test_a_bare_clean_only_touches_caches(self, workspace):
        """It must never be able to take a client's finished video."""
        targets = {r.path.parent.name for r in plan_clean(workspace)}
        assert targets <= DISPOSABLE
        assert "renders" not in targets

    def test_an_age_filter_excludes_fresh_files(self, workspace):
        assert plan_clean(workspace, older_than_days=1.0) == []
        assert plan_clean(workspace, older_than_days=0.0)

    def test_an_explicit_category_can_reach_further(self, workspace):
        targets = {r.path.parent.name for r in plan_clean(workspace, ["renders"])}
        assert targets == {"renders"}

    def test_planning_deletes_nothing(self, workspace):
        before = total_bytes(workspace)
        plan_clean(workspace)
        assert total_bytes(workspace) == before


class TestApplying:
    def test_it_removes_exactly_what_was_planned(self, workspace):
        removals = plan_clean(workspace)
        planned = sum(r.bytes for r in removals)
        count, freed = apply_clean(removals)

        assert count == len(removals)
        assert freed == planned
        assert (workspace / "renders" / "f.bin").exists()

    def test_an_already_deleted_file_does_not_stop_the_rest(self, workspace):
        removals = plan_clean(workspace)
        removals[0].path.unlink()
        count, _ = apply_clean(removals)
        assert count == len(removals) - 1


class TestWarning:
    def test_it_is_quiet_when_there_is_room(self, workspace):
        assert warning(workspace, threshold_gb=0.0) is None

    def test_it_says_what_can_be_freed(self, workspace):
        note = warning(workspace, threshold_gb=10_000)
        assert note and "snsauto workspace clean" in note
        # The consequence matters: rendering and publishing stop together.
        assert "予約投稿" in note


class TestDatabasePermissions:
    def test_a_new_database_is_owner_only(self, tmp_path, monkeypatch):
        """Tokens are stored unencrypted, so the mode is the whole defence."""
        from snsauto import db as dbmod

        monkeypatch.setenv("SNSAUTO_DB_URL", f"sqlite:///{tmp_path}/t.db")
        monkeypatch.setattr(dbmod, "_engine", None, raising=False)
        from snsauto.config import get_settings

        get_settings.cache_clear()
        dbmod.init_db()

        mode = stat.S_IMODE(os.stat(tmp_path / "t.db").st_mode)
        assert not mode & 0o077

    def test_a_loosened_mode_is_reported_with_the_fix(self, tmp_path, monkeypatch):
        from snsauto.db import db_permission_warning

        path = tmp_path / "loose.db"
        path.write_bytes(b"")
        path.chmod(0o644)
        note = db_permission_warning(f"sqlite:///{path}")

        assert note and "chmod 600" in note
        # It has to say what the exposure actually is.
        assert "投稿できます" in note

    def test_a_non_sqlite_url_has_no_file_to_check(self):
        from snsauto.db import db_permission_warning, sqlite_path

        assert sqlite_path("postgresql://host/db") is None
        assert db_permission_warning("postgresql://host/db") is None
