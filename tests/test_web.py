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


# ---------------------------------------------------------------------------
# Authenticated UI and action routes
# ---------------------------------------------------------------------------


@pytest.fixture
def secure_env(tmp_path, monkeypatch):
    """The same app with auth turned on, plus an admin and a viewer."""
    from snsauto.web.auth import create_user

    db = tmp_path / "secure.db"
    monkeypatch.setenv("SNSAUTO_DB_URL", f"sqlite:///{db}")
    monkeypatch.setenv("SNSAUTO_WORKSPACE", str(tmp_path / "ws"))

    import snsauto.db as dbmod
    from snsauto.config import get_settings

    dbmod._engine = None
    dbmod._Session = None
    get_settings.cache_clear()

    settings = Settings(
        _env_file=None, SNSAUTO_DB_URL=f"sqlite:///{db}",
        SNSAUTO_WORKSPACE=str(tmp_path / "ws"), SNSAUTO_AUTH_ENABLED=True,
        SNSAUTO_SECRET_KEY="s" * 40, SNSAUTO_COOKIE_SECURE=False,
    )
    settings.ensure_workspace()
    dbmod.init_db()

    with dbmod.session_scope() as session:
        project = Project(name="secure", brand_profile={})
        session.add(project)
        session.flush()
        create_user(session, "admin@x.com", "supersecret1", "Admin", "admin")
        create_user(session, "editor@x.com", "supersecret1", "Editor", "editor")
        create_user(session, "viewer@x.com", "supersecret1", "Viewer", "viewer")
        ids = {"project": project.id}

    from snsauto.web import create_app

    def client_for(email: str | None):
        client = TestClient(create_app(settings))
        client.get("/login")
        token = client.cookies.get("snsauto_csrf")
        if email:
            client.post(
                "/login",
                data={"email": email, "password": "supersecret1", "csrf_token": token},
                follow_redirects=False,
            )
        return client, token

    yield client_for, ids

    get_settings.cache_clear()
    dbmod._engine = None
    dbmod._Session = None


class TestLoginGate:
    def test_anonymous_pages_redirect_to_login(self, secure_env):
        """A signed-out browser must land on the login form, not raw JSON."""
        client_for, _ = secure_env
        client, _ = client_for(None)
        for path in ("/", "/jobs", "/capabilities"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/login"

    def test_anonymous_api_calls_get_a_status_code(self, secure_env):
        client_for, _ = secure_env
        client, _ = client_for(None)
        assert client.get("/api/projects", follow_redirects=False).status_code == 401

    def test_login_page_is_reachable(self, secure_env):
        client_for, _ = secure_env
        client, _ = client_for(None)
        assert client.get("/login").status_code == 200

    def test_login_sets_a_session_cookie(self, secure_env):
        client_for, _ = secure_env
        client, _ = client_for("admin@x.com")
        assert "snsauto_session" in client.cookies
        assert client.get("/").status_code == 200

    def test_login_without_csrf_is_rejected(self, secure_env):
        client_for, _ = secure_env
        client, _ = client_for(None)
        response = client.post(
            "/login", data={"email": "admin@x.com", "password": "supersecret1"},
            follow_redirects=False,
        )
        assert response.status_code == 400

    def test_bad_password_and_unknown_user_look_identical(self, secure_env):
        """Different messages would let an attacker enumerate accounts."""
        client_for, _ = secure_env
        client, token = client_for(None)
        wrong = client.post("/login", data={"email": "admin@x.com", "password": "no",
                                            "csrf_token": token}, follow_redirects=False)
        ghost = client.post("/login", data={"email": "ghost@x.com", "password": "no",
                                            "csrf_token": token}, follow_redirects=False)
        assert wrong.headers["location"] == ghost.headers["location"]
        assert "snsauto_session" not in client.cookies

    def test_logout_clears_access(self, secure_env):
        client_for, _ = secure_env
        client, token = client_for("admin@x.com")
        client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
        assert client.get("/", follow_redirects=False).status_code == 303


class TestRoles:
    def test_viewer_reads_but_cannot_write(self, secure_env):
        client_for, ids = secure_env
        client, token = client_for("viewer@x.com")
        assert client.get("/").status_code == 200
        assert client.post(f"/projects/{ids['project']}/metrics",
                           data={"csrf_token": token}).status_code == 403

    def test_editor_writes_but_cannot_publish(self, secure_env):
        client_for, ids = secure_env
        client, token = client_for("editor@x.com")
        assert client.post(f"/projects/{ids['project']}/metrics",
                           data={"csrf_token": token}).status_code in (303, 200)
        assert client.post("/renders/1/publish",
                           data={"platforms": ["tiktok"], "confirm": "PUBLISH",
                                 "csrf_token": token}).status_code == 403

    def test_actions_require_csrf(self, secure_env):
        client_for, ids = secure_env
        client, _ = client_for("admin@x.com")
        assert client.post(f"/projects/{ids['project']}/metrics", data={}).status_code == 400


class TestActions:
    def test_enqueuing_redirects_with_a_job_id(self, secure_env):
        client_for, ids = secure_env
        client, token = client_for("admin@x.com")
        response = client.post(
            f"/projects/{ids['project']}/research",
            data={"keyword": "副業", "platform": "youtube", "limit": 10,
                  "csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "job=" in response.headers["location"]

    def test_job_status_is_readable(self, secure_env):
        client_for, ids = secure_env
        client, token = client_for("admin@x.com")
        response = client.post(
            f"/projects/{ids['project']}/metrics", data={"csrf_token": token},
            follow_redirects=False,
        )
        job_id = response.headers["location"].split("job=")[-1]
        body = client.get(f"/api/jobs/{job_id}").json()
        assert body["kind"] == "metrics"

    def test_jobs_page_renders(self, secure_env):
        client_for, ids = secure_env
        client, token = client_for("admin@x.com")
        client.post(f"/projects/{ids['project']}/metrics", data={"csrf_token": token})
        assert "実行履歴" in client.get("/jobs").text

    def test_publish_needs_the_typed_confirmation(self, secure_env, tmp_path):
        """Posting publicly is irreversible, so one click must not do it."""
        import snsauto.db as dbmod
        from snsauto.models import Render

        video = tmp_path / "r.mp4"
        video.write_bytes(b"\x00" * 8)
        with dbmod.session_scope() as session:
            render = Render(storyboard_id=1, path=str(video), duration_sec=5)
            session.add(render)
            session.flush()
            render_id = render.id

        client_for, _ = secure_env
        client, token = client_for("admin@x.com")
        assert client.post(f"/renders/{render_id}/publish",
                           data={"platforms": ["tiktok"], "confirm": "yes",
                                 "csrf_token": token}).status_code == 400

    def test_publish_needs_a_platform(self, secure_env, tmp_path):
        import snsauto.db as dbmod
        from snsauto.models import Render

        video = tmp_path / "r2.mp4"
        video.write_bytes(b"\x00" * 8)
        with dbmod.session_scope() as session:
            render = Render(storyboard_id=1, path=str(video), duration_sec=5)
            session.add(render)
            session.flush()
            render_id = render.id

        client_for, _ = secure_env
        client, token = client_for("admin@x.com")
        assert client.post(f"/renders/{render_id}/publish",
                           data={"confirm": "PUBLISH", "csrf_token": token}
                           ).status_code == 400


def test_localhost_mode_needs_no_login(app_env):
    """With auth off the UI is open, which is only safe on localhost."""
    client, _ = app_env
    assert client.get("/").status_code == 200


# ---------------------------------------------------------------------------
# Alerts, brand profile and public asset serving
# ---------------------------------------------------------------------------


class TestAlertsInUi:
    def test_open_alerts_appear_on_the_dashboard(self, secure_env):
        import snsauto.db as dbmod
        from snsauto.notify import AlertService

        with dbmod.session_scope() as session:
            AlertService(session, sender=None).raise_alert(
                "worker.publish", "トークンが失効しました", "expired"
            )

        client_for, _ = secure_env
        client, _ = client_for("admin@x.com")
        body = client.get("/").text
        assert "要対応" in body and "トークンが失効しました" in body

    def test_acknowledging_clears_it(self, secure_env):
        import snsauto.db as dbmod
        from snsauto.notify import AlertService

        with dbmod.session_scope() as session:
            alert = AlertService(session, sender=None).raise_alert("x", "消えるはず")
            alert_id = alert.id

        client_for, _ = secure_env
        client, token = client_for("admin@x.com")
        assert client.post(f"/alerts/{alert_id}/ack", data={"csrf_token": token},
                           follow_redirects=False).status_code == 303
        assert "消えるはず" not in client.get("/").text

    def test_viewers_cannot_acknowledge(self, secure_env):
        import snsauto.db as dbmod
        from snsauto.notify import AlertService

        with dbmod.session_scope() as session:
            alert_id = AlertService(session, sender=None).raise_alert("x", "t").id

        client_for, _ = secure_env
        client, token = client_for("viewer@x.com")
        assert client.post(f"/alerts/{alert_id}/ack",
                           data={"csrf_token": token}).status_code == 403


class TestBrandProfile:
    def test_page_renders(self, secure_env):
        client_for, ids = secure_env
        client, _ = client_for("admin@x.com")
        assert client.get(f"/projects/{ids['project']}/brand").status_code == 200

    def test_saving_stores_the_profile(self, secure_env):
        import snsauto.db as dbmod
        from snsauto.models import Project as ProjectModel

        client_for, ids = secure_env
        client, token = client_for("admin@x.com")
        response = client.post(
            f"/projects/{ids['project']}/brand",
            data={"persona": "副業3年目の会社員", "tone": "落ち着いた",
                  "banned_words": "絶対、必ず儲かる\nラクして",
                  "csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303

        with dbmod.session_scope() as session:
            profile = session.get(ProjectModel, ids["project"]).brand_profile
        assert profile["persona"] == "副業3年目の会社員"
        assert profile["banned_words"] == ["絶対", "必ず儲かる", "ラクして"]
        # Blank fields must not become empty instructions.
        assert "audience" not in profile

    def test_viewers_cannot_save(self, secure_env):
        client_for, ids = secure_env
        client, token = client_for("viewer@x.com")
        assert client.post(f"/projects/{ids['project']}/brand",
                           data={"persona": "x", "csrf_token": token}).status_code == 403


class TestPublicAssets:
    def test_disabled_by_default(self, secure_env):
        client_for, _ = secure_env
        client, _ = client_for("admin@x.com")
        assert client.get("/public/renders/anything.mp4").status_code == 404

    def test_served_without_a_session_when_enabled(self, tmp_path, monkeypatch):
        """Instagram's servers fetch this URL and carry no login."""
        from snsauto.storage import LocalStorage
        from snsauto.web import create_app

        db = tmp_path / "pub.db"
        monkeypatch.setenv("SNSAUTO_DB_URL", f"sqlite:///{db}")
        monkeypatch.setenv("SNSAUTO_WORKSPACE", str(tmp_path / "ws"))
        import snsauto.db as dbmod
        from snsauto.config import get_settings

        dbmod._engine = None
        dbmod._Session = None
        get_settings.cache_clear()

        settings = Settings(
            _env_file=None, SNSAUTO_DB_URL=f"sqlite:///{db}",
            SNSAUTO_WORKSPACE=str(tmp_path / "ws"), STORAGE_BACKEND="local",
            SNSAUTO_PUBLIC_BASE_URL="https://x.example.com",
            SNSAUTO_AUTH_ENABLED=True, SNSAUTO_SECRET_KEY="s" * 40,
        )
        settings.ensure_workspace()
        dbmod.init_db()

        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 64)
        asset = LocalStorage(settings.workspace / "public",
                             "https://x.example.com").upload(video)

        with TestClient(create_app(settings)) as client:
            response = client.get(f"/public/{asset.key}")
            assert response.status_code == 200
            assert response.headers["content-type"] == "video/mp4"
            assert client.get("/public/../snsauto.db").status_code in (400, 404)

        get_settings.cache_clear()
        dbmod._engine = None
        dbmod._Session = None
