"""OAuth connect flows and connected-account credential resolution."""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from snsauto.config import Settings
from snsauto.models import Platform, SocialAccount, utcnow
from snsauto.platforms import get_adapter
from snsauto.platforms.accounts import AccountService
from snsauto.platforms.oauth import (
    InstagramOAuth,
    OAuthError,
    OAuthNotConfigured,
    TikTokOAuth,
    XOAuth,
    YouTubeOAuth,
    get_provider,
    oauth_readiness,
)

BASE = "https://snsauto.example.com"


def _settings(**kw):
    return Settings(_env_file=None, SNSAUTO_PUBLIC_BASE_URL=BASE, **kw)


class FakeResponse:
    def __init__(self, payload=None, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """Replays queued responses and records what was asked for."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        if not self.responses:
            raise AssertionError(f"unexpected call: {method} {url}")
        return self.responses.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)


class TestReadiness:
    def test_public_base_url_is_required(self):
        readiness = oauth_readiness(Settings(_env_file=None))
        assert all(not row["ready"] for row in readiness.values())
        assert "SNSAUTO_PUBLIC_BASE_URL" in readiness["youtube"]["reason"]

    def test_app_credentials_are_required(self):
        readiness = oauth_readiness(_settings())
        assert "YOUTUBE_CLIENT_ID" in readiness["youtube"]["reason"]
        assert "FACEBOOK_APP_ID" in readiness["instagram"]["reason"]

    def test_ready_once_configured(self):
        settings = _settings(YOUTUBE_CLIENT_ID="a", YOUTUBE_CLIENT_SECRET="b")
        assert oauth_readiness(settings)["youtube"]["ready"] is True

    def test_x_is_not_refreshable(self):
        """OAuth 1.0a tokens do not expire, so there is nothing to refresh."""
        assert oauth_readiness(_settings())["x"]["refreshable"] is False

    def test_redirect_uri_is_per_platform(self):
        provider = get_provider(Platform.TIKTOK, _settings())
        assert provider.redirect_uri() == f"{BASE}/connect/tiktok/callback"


class TestYouTube:
    def test_authorize_url_requests_offline_access(self):
        """Without offline access Google returns no refresh token, and the
        connection dies within the hour."""
        provider = YouTubeOAuth(_settings(YOUTUBE_CLIENT_ID="cid", YOUTUBE_CLIENT_SECRET="s"))
        start = provider.start()
        query = parse_qs(urlparse(start.url).query)
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]
        assert "youtube.upload" in query["scope"][0]
        assert query["state"] == [start.state]

    def test_missing_credentials_are_refused(self):
        with pytest.raises(OAuthNotConfigured):
            YouTubeOAuth(_settings()).start()

    def test_code_exchange_returns_the_channel(self):
        client = FakeClient([
            FakeResponse({"access_token": "at", "refresh_token": "rt",
                          "expires_in": 3600, "scope": "a b"}),
            FakeResponse({"items": [{"id": "UC123", "snippet": {"title": "My Channel"}}]}),
        ])
        provider = YouTubeOAuth(
            _settings(YOUTUBE_CLIENT_ID="cid", YOUTUBE_CLIENT_SECRET="s"), client=client
        )
        account = provider.finish({"code": "abc"}, {})
        assert account.external_id == "UC123"
        assert account.display_name == "My Channel"
        assert account.refresh_token == "rt" and account.expires_in == 3600

    def test_refresh_keeps_the_stored_refresh_token(self):
        """Google does not return the refresh token again on refresh."""
        client = FakeClient([FakeResponse({"access_token": "new", "expires_in": 3600})])
        provider = YouTubeOAuth(
            _settings(YOUTUBE_CLIENT_ID="cid", YOUTUBE_CLIENT_SECRET="s"), client=client
        )
        stored = SocialAccount(platform=Platform.YOUTUBE, external_id="UC1",
                               access_token="old", refresh_token="keep-me")
        refreshed = provider.refresh(stored)
        assert refreshed.access_token == "new"
        assert refreshed.refresh_token == "keep-me"

    def test_refresh_without_a_token_is_refused(self):
        provider = YouTubeOAuth(_settings(YOUTUBE_CLIENT_ID="c", YOUTUBE_CLIENT_SECRET="s"))
        account = SocialAccount(platform=Platform.YOUTUBE, external_id="x",
                                access_token="a", refresh_token=None)
        with pytest.raises(OAuthError):
            provider.refresh(account)


class TestTikTok:
    def test_authorize_url_requests_publish_scope(self):
        provider = TikTokOAuth(_settings(TIKTOK_CLIENT_KEY="k", TIKTOK_CLIENT_SECRET="s"))
        query = parse_qs(urlparse(provider.start().url).query)
        assert "video.publish" in query["scope"][0]

    def test_token_lifetimes_are_carried_through(self):
        """Access 24h, refresh 365d - both must be stored or the worker cannot
        know when to renew."""
        client = FakeClient([
            FakeResponse({"access_token": "at", "refresh_token": "rt", "open_id": "oid",
                          "expires_in": 86400, "refresh_expires_in": 31536000,
                          "scope": "video.publish,user.info.basic"}),
            FakeResponse({"data": {"user": {"display_name": "Demo", "username": "demo"}}}),
        ])
        provider = TikTokOAuth(
            _settings(TIKTOK_CLIENT_KEY="k", TIKTOK_CLIENT_SECRET="s"), client=client
        )
        account = provider.finish({"code": "c"}, {})
        assert account.external_id == "oid"
        assert account.expires_in == 86400
        assert account.refresh_expires_in == 31536000
        assert account.scopes == ["video.publish", "user.info.basic"]

    def test_api_level_errors_are_surfaced(self):
        client = FakeClient([
            FakeResponse({"error": "invalid_grant", "error_description": "code used"})
        ])
        provider = TikTokOAuth(
            _settings(TIKTOK_CLIENT_KEY="k", TIKTOK_CLIENT_SECRET="s"), client=client
        )
        with pytest.raises(OAuthError) as exc:
            provider.finish({"code": "c"}, {})
        assert "code used" in str(exc.value)


class TestInstagram:
    def _provider(self, responses):
        return InstagramOAuth(
            _settings(FACEBOOK_APP_ID="app", FACEBOOK_APP_SECRET="sec"),
            client=FakeClient(responses),
        )

    def test_finds_the_business_account_behind_a_page(self):
        provider = self._provider([
            FakeResponse({"access_token": "short"}),
            FakeResponse({"access_token": "long", "expires_in": 5184000}),
            FakeResponse({"data": [
                {"id": "page1", "name": "No IG"},
                {"id": "page2", "name": "My Page", "access_token": "pagetok",
                 "instagram_business_account": {"id": "ig99", "username": "demo"}},
            ]}),
        ])
        account = provider.finish({"code": "c"}, {})
        assert account.external_id == "ig99"
        # The Page token is what the publishing API accepts, not the user token.
        assert account.access_token == "pagetok"
        assert account.meta["page_id"] == "page2"
        assert account.meta["user_token"] == "long"

    def test_a_page_without_instagram_explains_the_fix(self):
        provider = self._provider([
            FakeResponse({"access_token": "short"}),
            FakeResponse({"access_token": "long"}),
            FakeResponse({"data": [{"id": "p", "name": "Page"}]}),
        ])
        with pytest.raises(OAuthError) as exc:
            provider.finish({"code": "c"}, {})
        assert "ビジネスアカウント" in str(exc.value)

    def test_no_pages_at_all_explains_the_fix(self):
        provider = self._provider([
            FakeResponse({"access_token": "short"}),
            FakeResponse({"access_token": "long"}),
            FakeResponse({"data": []}),
        ])
        with pytest.raises(OAuthError) as exc:
            provider.finish({"code": "c"}, {})
        assert "ページが見つかりません" in str(exc.value)

    def test_refresh_extends_in_place(self):
        """Instagram has no refresh token; the token is exchanged for a new one."""
        provider = self._provider([
            FakeResponse({"access_token": "extended", "expires_in": 5184000}),
            FakeResponse({"access_token": "newpagetok"}),
        ])
        stored = SocialAccount(
            platform=Platform.INSTAGRAM, external_id="ig99", access_token="oldpage",
            meta={"page_id": "page2", "user_token": "olduser"},
        )
        refreshed = provider.refresh(stored)
        assert refreshed.access_token == "newpagetok"
        assert refreshed.meta["user_token"] == "extended"

    def test_graph_errors_name_the_problem(self):
        provider = self._provider([
            FakeResponse({"error": {"message": "Invalid OAuth code"}}, status=400)
        ])
        with pytest.raises(OAuthError) as exc:
            provider.finish({"code": "bad"}, {})
        assert "Invalid OAuth code" in str(exc.value)


class TestX:
    def test_request_token_step_builds_an_authorize_url(self):
        client = FakeClient([
            FakeResponse(text="oauth_token=rt&oauth_token_secret=rs&oauth_callback_confirmed=true")
        ])
        provider = XOAuth(_settings(X_API_KEY="k", X_API_SECRET="s"), client=client)
        start = provider.start()
        assert "oauth_token=rt" in start.url
        assert start.extra["request_token_secret"] == "rs"

    def test_callback_exchanges_the_verifier(self):
        client = FakeClient([
            FakeResponse(text="oauth_token=at&oauth_token_secret=as&user_id=42&screen_name=demo")
        ])
        provider = XOAuth(_settings(X_API_KEY="k", X_API_SECRET="s"), client=client)
        account = provider.finish(
            {"oauth_token": "rt", "oauth_verifier": "v"},
            {"state": "rt", "request_token_secret": "rs"},
        )
        assert account.external_id == "42"
        assert account.access_token == "at" and account.token_secret == "as"
        # OAuth 1.0a tokens never expire.
        assert account.expires_in is None

    def test_missing_verifier_is_refused(self):
        provider = XOAuth(_settings(X_API_KEY="k", X_API_SECRET="s"))
        with pytest.raises(OAuthError):
            provider.finish({"oauth_token": "rt"}, {"state": "rt"})


class TestCredentialResolution:
    def test_no_credentials_means_no_publishing(self, session):
        service = AccountService(session, Settings(_env_file=None))
        assert service.resolve(Platform.TIKTOK) is None

    def test_environment_is_the_fallback(self, session):
        settings = Settings(_env_file=None, TIKTOK_ACCESS_TOKEN="env-token")
        credentials = AccountService(session, settings).resolve(Platform.TIKTOK)
        assert credentials.access_token == "env-token" and credentials.source == "env"

    def test_a_connected_account_wins_over_the_environment(self, session):
        settings = Settings(_env_file=None, TIKTOK_ACCESS_TOKEN="env-token")
        session.add(SocialAccount(platform=Platform.TIKTOK, external_id="oid",
                                  access_token="connected-token"))
        session.flush()
        credentials = AccountService(session, settings).resolve(Platform.TIKTOK)
        assert credentials.access_token == "connected-token"
        assert credentials.source == "connected"

    def test_a_project_account_wins_over_a_shared_one(self, session, project):
        session.add(SocialAccount(platform=Platform.TIKTOK, external_id="shared",
                                  access_token="shared-token"))
        session.add(SocialAccount(platform=Platform.TIKTOK, external_id="mine",
                                  access_token="project-token", project_id=project.id))
        session.flush()
        credentials = AccountService(session, Settings(_env_file=None)).resolve(
            Platform.TIKTOK, project.id
        )
        assert credentials.access_token == "project-token"

    def test_a_disconnected_account_is_ignored(self, session):
        session.add(SocialAccount(platform=Platform.TIKTOK, external_id="oid",
                                  access_token="tok", is_active=False))
        session.flush()
        assert AccountService(session, Settings(_env_file=None)).resolve(Platform.TIKTOK) is None

    def test_the_adapter_uses_the_connected_token(self, session):
        session.add(SocialAccount(platform=Platform.INSTAGRAM, external_id="ig1",
                                  access_token="connected"))
        session.flush()
        credentials = AccountService(session, Settings(_env_file=None)).resolve(
            Platform.INSTAGRAM
        )
        adapter = get_adapter(Platform.INSTAGRAM, Settings(_env_file=None), credentials)
        assert adapter._token() == "connected"
        assert adapter._user_id() == "ig1"
        from snsauto.platforms import Capability

        assert Capability.PUBLISH in adapter.capabilities()


class TestRefreshScheduling:
    def _account(self, session, hours, platform=Platform.TIKTOK, refresh="rt"):
        account = SocialAccount(
            platform=platform, external_id="a", access_token="t", refresh_token=refresh,
            expires_at=utcnow() + timedelta(hours=hours),
        )
        session.add(account)
        session.flush()
        return account

    def test_a_token_inside_the_margin_is_due(self, session):
        settings = Settings(_env_file=None, SNSAUTO_TOKEN_REFRESH_MARGIN=6)
        account = self._account(session, hours=2)
        assert AccountService(session, settings).needs_refresh(account)

    def test_a_token_outside_the_margin_is_not(self, session):
        settings = Settings(_env_file=None, SNSAUTO_TOKEN_REFRESH_MARGIN=6)
        account = self._account(session, hours=20)
        assert not AccountService(session, settings).needs_refresh(account)

    def test_a_token_without_an_expiry_is_never_due(self, session):
        settings = Settings(_env_file=None)
        account = SocialAccount(platform=Platform.X, external_id="a",
                                access_token="t", expires_at=None)
        session.add(account)
        session.flush()
        assert not AccountService(session, settings).needs_refresh(account)

    def test_x_is_never_scheduled_for_refresh(self, session):
        settings = Settings(_env_file=None)
        account = self._account(session, hours=1, platform=Platform.X)
        assert not AccountService(session, settings).needs_refresh(account)

    def test_expired_tokens_still_count_as_due(self, session):
        settings = Settings(_env_file=None)
        account = self._account(session, hours=-1)
        assert account.is_expired
        assert AccountService(session, settings).needs_refresh(account)

    def test_a_failed_refresh_is_recorded_not_raised_away(self, session):
        settings = Settings(_env_file=None, SNSAUTO_TOKEN_REFRESH_MARGIN=6)
        self._account(session, hours=1)

        class Failing:
            supports_refresh = True

            def refresh(self, account):
                raise OAuthError("refresh token revoked")

        service = AccountService(session, settings, provider_factory=lambda p: Failing())
        results = service.refresh_due()
        assert results and results[0]["status"] == "failed"
        assert session.query(SocialAccount).one().refresh_error

    def test_a_network_error_does_not_escape_as_itself(self, session):
        """The worker publishes in the same tick; one flaky refresh must not
        take the whole tick down."""
        settings = Settings(_env_file=None, SNSAUTO_TOKEN_REFRESH_MARGIN=6)
        self._account(session, hours=1)

        class Exploding:
            supports_refresh = True

            def refresh(self, account):
                raise ConnectionError("proxy refused")

        service = AccountService(session, settings, provider_factory=lambda p: Exploding())
        results = service.refresh_due()
        assert results[0]["status"] == "failed"
        assert "ConnectionError" in session.query(SocialAccount).one().refresh_error

    def test_a_successful_refresh_clears_the_error(self, session):
        from snsauto.platforms.oauth import ConnectedAccount

        settings = Settings(_env_file=None, SNSAUTO_TOKEN_REFRESH_MARGIN=6)
        account = self._account(session, hours=1)
        account.refresh_error = "previous failure"
        session.flush()

        class Working:
            supports_refresh = True

            def refresh(self, stored):
                return ConnectedAccount(
                    platform=stored.platform, external_id=stored.external_id,
                    access_token="fresh", refresh_token="rt2", expires_in=86400,
                )

        service = AccountService(session, settings, provider_factory=lambda p: Working())
        service.refresh(account)
        assert account.access_token == "fresh"
        assert account.refresh_error is None
        assert account.seconds_until_expiry() > 80000
