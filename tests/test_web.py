"""Web UI route tests, driven against a real in-process app."""

import pytest
from fastapi.testclient import TestClient

from snsauto.config import Settings
from snsauto.creative.imagegen import ImageGenerator, PlaceholderProvider
from snsauto.creative.script import ScriptService
from snsauto.creative.storyboard import StoryboardService
from snsauto.models import Platform, Project, Publication, PublicationStatus, Render
from snsauto.platforms import PostRecord
from snsauto.research.keyword import ResearchService
from snsauto.research.structure import StructureService


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """A populated database plus a client, wired to a temp workspace."""
    from datetime import datetime, timedelta, timezone

    db = tmp_path / "web.db"
    monkeypatch.setenv("SNSAUTO_DB_URL", f"sqlite:///{db}")
    monkeypatch.setenv("SNSAUTO_WORKSPACE", str(tmp_path / "ws"))

    import snsauto.db as dbmod

    dbmod._engine = None  # the module caches one engine per process
    dbmod._Session = None

    from snsauto.config import get_settings

    get_settings.cache_clear()
    settings = Settings(_env_file=None, SNSAUTO_DB_URL=f"sqlite:///{db}",
                        SNSAUTO_WORKSPACE=str(tmp_path / "ws"))
    settings.ensure_workspace()
    dbmod.init_db()

    now = datetime.now(timezone.utc)
    records = [
        PostRecord(
            external_id=f"p{i}", platform=Platform.TIKTOK, title=f"なぜ失敗するのか？（{i}）",
            caption="本文 #副業 保存してね", url=f"https://example.com/{i}",
            published_at=now - timedelta(days=i + 1), duration_sec=22.0,
            views=10_000 + i * 500, likes=900, comments=70, shares=40,
        )
        for i in range(8)
    ]

    with dbmod.session_scope() as session:
        project = Project(name="demo", description="テスト", brand_profile={})
        session.add(project)
        session.flush()

        run = ResearchService(session).run(
            project, "副業", Platform.TIKTOK, records=records, source="manual"
        )
        structure = StructureService(session)
        for post in run.posts[:4]:
            structure.analyze(post)

        script = ScriptService(session).generate(project, "副業", Platform.TIKTOK, 6.0)
        board = StoryboardService(session).generate(script)
        ImageGenerator(provider=PlaceholderProvider()).render_storyboard(
            board, tmp_path / "img", 180, 320
        )
        session.flush()

        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypmp42")  # a real file on disk, not real video
        session.add(Render(storyboard_id=board.id, path=str(video), duration_sec=6.0,
                           meta={"telop_cues": 2}))
        session.add(Publication(project_id=project.id, platform=Platform.TIKTOK,
                                status=PublicationStatus.DRAFT, caption="c"))
        session.flush()
        ids = {"project": project.id, "run": run.id, "script": script.id,
               "shot": board.shots[0].id}

    from snsauto.web import create_app

    with TestClient(create_app(settings)) as client:
        yield client, ids

    get_settings.cache_clear()
    dbmod._engine = None
    dbmod._Session = None


class TestPages:
    def test_dashboard_lists_the_project(self, app_env):
        client, _ = app_env
        r = client.get("/")
        assert r.status_code == 200
        assert "demo" in r.text
        assert "ダッシュボード" in r.text

    def test_capabilities_page_states_the_tiktok_limit(self, app_env):
        client, _ = app_env
        r = client.get("/capabilities")
        assert r.status_code == 200
        assert "TikTokの検索は原理的に使えません" in r.text

    def test_project_page(self, app_env):
        client, ids = app_env
        r = client.get(f"/projects/{ids['project']}")
        assert r.status_code == 200 and "副業" in r.text

    def test_run_page_shows_ranked_posts_and_live_weights(self, app_env):
        client, ids = app_env
        r = client.get(f"/runs/{ids['run']}")
        assert r.status_code == 200
        assert "上位パフォーマー" in r.text
        assert "55%" in r.text  # weights come from the scorer

    def test_run_page_shows_hook_types(self, app_env):
        client, ids = app_env
        r = client.get(f"/runs/{ids['run']}")
        assert "フック類型" in r.text and "question" in r.text

    def test_script_page_embeds_the_player_and_shots(self, app_env):
        client, ids = app_env
        r = client.get(f"/scripts/{ids['script']}")
        assert r.status_code == 200
        assert "<video" in r.text
        assert "/media/shot/" in r.text


class TestMedia:
    def test_shot_is_served_as_png(self, app_env):
        client, ids = app_env
        r = client.get(f"/media/shot/{ids['shot']}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content.startswith(b"\x89PNG")

    def test_render_download_sets_attachment(self, app_env):
        client, _ = app_env
        r = client.get("/media/render/1?download=1")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")

    def test_missing_media_is_404_not_500(self, app_env):
        client, _ = app_env
        assert client.get("/media/shot/9999").status_code == 404
        assert client.get("/media/render/9999").status_code == 404


class TestApi:
    def test_projects_json(self, app_env):
        client, _ = app_env
        rows = client.get("/api/projects").json()
        assert rows[0]["name"] == "demo" and rows[0]["runs"] == 1

    def test_run_json_carries_the_summary(self, app_env):
        client, ids = app_env
        body = client.get(f"/api/runs/{ids['run']}").json()
        assert body["keyword"] == "副業"
        assert body["summary"]["count"] == 8

    def test_capabilities_json_covers_all_platforms(self, app_env):
        client, _ = app_env
        assert set(client.get("/api/capabilities").json()) == {p.value for p in Platform}

    def test_healthz(self, app_env):
        client, _ = app_env
        assert client.get("/healthz").json() == {"ok": True}


class TestNotFound:
    @pytest.mark.parametrize("path", ["/projects/999", "/runs/999", "/scripts/999", "/cycles/999"])
    def test_unknown_ids_return_404(self, app_env, path):
        client, _ = app_env
        assert client.get(path).status_code == 404


def test_report_routes_render_html(app_env):
    client, ids = app_env
    for path in (f"/runs/{ids['run']}/report", f"/projects/{ids['project']}/report"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.text.lstrip().startswith("<!doctype html>")
