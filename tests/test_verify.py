"""The connection verifier, driven through the real request stack."""

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from snsauto.config import Settings
from snsauto.models import (
    Base, Platform, Project, Publication, PublicationStatus,
)
from snsauto.platforms.verify import FAIL, OK, SKIP, ConnectionVerifier


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/v.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        project = Project(name="v", brand_profile={})
        session.add(project)
        session.flush()
        session.commit()
        yield session, project.id


def transport(routes):
    def handle(request):
        for fragment, body in routes.items():
            if fragment in str(request.url):
                if isinstance(body, int):
                    return httpx.Response(body, json={"error": {"message": "denied"}})
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "no route"})

    return httpx.Client(transport=httpx.MockTransport(handle))


def verifier_with(db, adapter):
    session, project_id = db
    v = ConnectionVerifier(session, Settings(_env_file=None))
    v._adapter = lambda *a, **k: (adapter, None)
    return v, project_id


def checks(report):
    return {c.name: c for c in report.checks}


YT_SEARCH = {
    "/search": {"items": [{"id": {"videoId": "a"}}, {"id": {"videoId": "b"}}]},
    "/videos": {"items": [{
        "id": "a",
        "snippet": {"title": "t", "channelTitle": "c",
                    "publishedAt": "2026-09-01T00:00:00Z"},
        "statistics": {"viewCount": "500", "likeCount": "20", "commentCount": "3"},
        "contentDetails": {"duration": "PT45S"},
    }]},
}


def youtube(routes, **settings_kwargs):
    from snsauto.platforms.youtube import YouTubeAdapter

    base = {"_env_file": None, "YOUTUBE_API_KEY": "k"}
    base.update(settings_kwargs)
    return YouTubeAdapter(settings=Settings(**base), client=transport(routes))


class TestItNeverWrites:
    def test_no_publishing_method_is_reached(self, db):
        """Running this against a production account has to be safe."""
        calls = []

        adapter = youtube(YT_SEARCH)
        for name in ("publish", "ingest_manual"):
            if hasattr(adapter, name):
                setattr(adapter, name,
                        lambda *a, _n=name, **k: calls.append(_n))

        v, project_id = verifier_with(db, adapter)
        v.verify(Platform.YOUTUBE, project_id)
        assert calls == []

    def test_only_get_requests_reach_the_network(self, db):
        methods = []

        def handle(request):
            methods.append(request.method)
            for fragment, body in YT_SEARCH.items():
                if fragment in str(request.url):
                    return httpx.Response(200, json=body)
            return httpx.Response(404, json={})

        from snsauto.platforms.youtube import YouTubeAdapter

        adapter = YouTubeAdapter(
            settings=Settings(_env_file=None, YOUTUBE_API_KEY="k"),
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        v, project_id = verifier_with(db, adapter)
        v.verify(Platform.YOUTUBE, project_id)
        assert set(methods) <= {"GET"}


class TestSearchCheck:
    def test_a_working_search_reports_the_count(self, db):
        v, project_id = verifier_with(db, youtube(YT_SEARCH))
        result = checks(v.verify(Platform.YOUTUBE, project_id))["search"]
        assert result.status == OK
        assert "1件" in result.detail

    def test_an_empty_result_is_a_failure_not_a_pass(self, db):
        # Auth worked but nothing came back - that is not a working search,
        # and calling it OK would hide a broken query.
        v, project_id = verifier_with(
            db, youtube({"/search": {"items": []}, "/videos": {"items": []}})
        )
        result = checks(v.verify(Platform.YOUTUBE, project_id))["search"]
        assert result.status == FAIL
        assert "0件" in result.detail

    def test_a_403_names_the_scope_as_the_fix(self, db):
        v, project_id = verifier_with(db, youtube({"/search": 403}))
        result = checks(v.verify(Platform.YOUTUBE, project_id))["search"]
        assert result.status == FAIL
        assert "権限" in result.fix

    def test_a_401_says_to_reconnect(self, db):
        v, project_id = verifier_with(db, youtube({"/search": 401}))
        assert "再連携" in checks(v.verify(Platform.YOUTUBE, project_id))["search"].fix

    def test_fields_the_platform_left_empty_are_named(self, db):
        """A record that parses but carries no views is its own problem."""
        routes = dict(YT_SEARCH)
        routes["/videos"] = {"items": [{
            "id": "a",
            "snippet": {"title": "t", "publishedAt": "2026-09-01T00:00:00Z"},
            "statistics": {},
            "contentDetails": {},
        }]}
        v, project_id = verifier_with(db, youtube(routes))
        result = checks(v.verify(Platform.YOUTUBE, project_id))["search"]
        assert "views" in result.data["empty"]
        assert "duration_sec" in result.data["empty"]


class TestInsightsCheck:
    def _published(self, db, external_id="pub1"):
        session, project_id = db
        publication = Publication(
            project_id=project_id, platform=Platform.YOUTUBE,
            status=PublicationStatus.PUBLISHED, external_id=external_id,
        )
        session.add(publication)
        session.flush()
        session.commit()
        return publication

    def test_it_is_skipped_when_there_is_nothing_of_ours_to_read(self, db):
        v, project_id = verifier_with(db, youtube(YT_SEARCH))
        result = checks(v.verify(Platform.YOUTUBE, project_id))["insights"]
        assert result.status == SKIP
        assert "1本投稿" in result.fix

    def test_it_names_which_metrics_came_back(self, db):
        self._published(db)
        routes = dict(YT_SEARCH)
        routes["/videos"] = {"items": [{
            "id": "pub1", "snippet": {"title": "t"},
            "statistics": {"viewCount": "900", "likeCount": "40", "commentCount": "2"},
            "contentDetails": {"duration": "PT30S"},
        }]}
        v, project_id = verifier_with(db, youtube(routes))
        result = checks(v.verify(Platform.YOUTUBE, project_id))["insights"]
        assert result.status == OK
        assert "views" in result.data["present"]

    def test_missing_retention_is_called_out_with_the_scope_to_add(self, db):
        """Metrics "working" while every retention field is null is the case
        that is invisible without looking."""
        self._published(db)
        routes = dict(YT_SEARCH)
        routes["/videos"] = {"items": [{
            "id": "pub1", "snippet": {"title": "t"},
            "statistics": {"viewCount": "900"}, "contentDetails": {},
        }]}
        v, project_id = verifier_with(db, youtube(routes))
        result = checks(v.verify(Platform.YOUTUBE, project_id))["insights"]
        assert result.status == OK
        assert "retention_rate" in result.data["missing"]
        assert "yt-analytics.readonly" in result.fix


class TestCredentials:
    def test_no_capabilities_stops_before_any_network_call(self, db):
        from snsauto.platforms.youtube import YouTubeAdapter

        requested = []

        def handle(request):
            requested.append(str(request.url))
            return httpx.Response(200, json={})

        adapter = YouTubeAdapter(
            settings=Settings(_env_file=None),   # no key at all
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        v, project_id = verifier_with(db, adapter)
        report = v.verify(Platform.YOUTUBE, project_id)

        assert checks(report)["credentials"].status == FAIL
        assert requested == []
        assert not report.usable

    def test_a_usable_platform_reports_its_capabilities(self, db):
        v, project_id = verifier_with(db, youtube(YT_SEARCH))
        report = v.verify(Platform.YOUTUBE, project_id)
        assert report.usable
        assert "search" in checks(report)["credentials"].detail


class TestTikTokIsHonest:
    def test_search_is_reported_as_unsupported_not_broken(self, db):
        from snsauto.platforms.tiktok import TikTokAdapter

        adapter = TikTokAdapter(
            settings=Settings(_env_file=None, TIKTOK_ACCESS_TOKEN="t"),
            client=transport({}),
        )
        v, project_id = verifier_with(db, adapter)
        result = checks(v.verify(Platform.TIKTOK, project_id))["search"]
        assert result.status == SKIP        # not FAIL: nothing is broken
