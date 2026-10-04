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


class TestScopeCheck:
    """The grant an account actually holds, checked before it costs anything.

    The insights check needs a published post to read, so an account connected
    today cannot be checked that way until after its first post - and a missing
    permission shows up there as an empty metric rather than as an error. This
    check has no such prerequisite.
    """

    def _connected(self, session, platform, scopes):
        from snsauto.models import SocialAccount
        from snsauto.platforms.accounts import Credentials

        account = SocialAccount(
            platform=platform, external_id="x", access_token="t", scopes=scopes,
        )
        session.add(account)
        session.flush()
        return Credentials(access_token="t", account_id=account.id, source="connected")

    def test_a_complete_grant_passes(self, db):
        from snsauto.platforms.oauth import INSTAGRAM_SCOPES

        session, project_id = db
        credentials = self._connected(session, Platform.INSTAGRAM, list(INSTAGRAM_SCOPES))
        v = ConnectionVerifier(session, Settings(_env_file=None))
        assert v._check_scopes(Platform.INSTAGRAM, credentials).status == OK

    def test_a_missing_grant_fails_and_names_what_it_costs(self, db):
        from snsauto.platforms.oauth import INSTAGRAM_SCOPES

        session, project_id = db
        without = [s for s in INSTAGRAM_SCOPES if s != "instagram_manage_insights"]
        credentials = self._connected(session, Platform.INSTAGRAM, without)
        v = ConnectionVerifier(session, Settings(_env_file=None))
        result = v._check_scopes(Platform.INSTAGRAM, credentials)

        assert result.status == FAIL
        assert "instagram_manage_insights" in result.detail
        # The operator has to be told what stops working, not just a scope name.
        assert "保存" in result.fix and "再連携" in result.fix

    def test_an_unrecorded_grant_is_unknown_not_missing(self, db):
        """Accounts connected before scopes were stored must not read as broken."""
        session, project_id = db
        credentials = self._connected(session, Platform.INSTAGRAM, [])
        v = ConnectionVerifier(session, Settings(_env_file=None))
        assert v._check_scopes(Platform.INSTAGRAM, credentials).status == SKIP

    def test_an_environment_token_is_skipped(self, db):
        from snsauto.platforms.accounts import Credentials

        session, project_id = db
        v = ConnectionVerifier(session, Settings(_env_file=None))
        result = v._check_scopes(Platform.INSTAGRAM, Credentials(access_token="t"))
        assert result.status == SKIP

    def test_x_has_no_per_scope_requirement(self, db):
        session, project_id = db
        credentials = self._connected(session, Platform.X, ["anything"])
        v = ConnectionVerifier(session, Settings(_env_file=None))
        assert v._check_scopes(Platform.X, credentials).status == SKIP


class TestRequestedScopesCoverWhatWeCall:
    """Every grant the adapters depend on has to be asked for at connect time.

    The failure this prevents is quiet: Instagram publishes and returns likes
    and comments without `instagram_manage_insights`, so the connection looks
    healthy while reach, saves, shares, average watch time and skip rate all
    come back empty.
    """

    def test_insights_is_requested_for_instagram(self):
        from snsauto.platforms.oauth import INSTAGRAM_SCOPES

        assert "instagram_manage_insights" in INSTAGRAM_SCOPES

    def test_analytics_is_requested_for_youtube(self):
        from snsauto.platforms.oauth import YOUTUBE_SCOPES

        assert "https://www.googleapis.com/auth/yt-analytics.readonly" in YOUTUBE_SCOPES

    def test_every_requested_scope_is_documented(self):
        """`snsauto verify` explains each grant, so none may go unexplained."""
        from snsauto.platforms.oauth import (
            INSTAGRAM_SCOPES, REQUIRED_SCOPES, TIKTOK_SCOPES, YOUTUBE_SCOPES,
        )

        for platform, requested in (
            (Platform.INSTAGRAM, INSTAGRAM_SCOPES),
            (Platform.YOUTUBE, YOUTUBE_SCOPES),
            (Platform.TIKTOK, TIKTOK_SCOPES),
        ):
            assert sorted(REQUIRED_SCOPES[platform]) == sorted(requested), platform
