"""What each adapter actually puts on the wire.

No live API call has ever been made from this codebase - there are no app
credentials - so every request it builds is, strictly, unverified. These tests
close as much of that gap as is closeable without a key: the request goes
through the real httpx stack (URL joining, query encoding, header assembly)
into a transport that captures it, and the captured request is asserted
against each platform's documented contract.

That catches the failure mode that would otherwise only appear on the very
first live call and cost an OAuth round trip to diagnose: a misspelled
parameter, a metric name the API does not know, a header on the wrong request,
a percentage stored where a fraction belongs.

What this cannot catch, and what `snsauto verify` exists for: whether the
credentials are valid, whether the app has the grant, and whether the platform
still returns the shape recorded here.
"""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from snsauto.config import Settings
from snsauto.models import Platform
from snsauto.platforms import SearchOptions
from snsauto.platforms.instagram import InstagramAdapter
from snsauto.platforms.tiktok import TikTokAdapter
from snsauto.platforms.x import XAdapter
from snsauto.platforms.youtube import YouTubeAdapter


class Recorder:
    """Captures requests while replying with canned bodies."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            path = urlparse(str(request.url)).path
            for fragment, body in self.routes.items():
                if fragment in path:
                    return httpx.Response(200, json=body)
            return httpx.Response(404, json={"error": f"no route for {path}"})

        return httpx.Client(transport=httpx.MockTransport(handle))

    def params(self, index: int = 0) -> dict:
        return {
            k: v[0] for k, v in
            parse_qs(urlparse(str(self.requests[index].url)).query).items()
        }

    def paths(self) -> list[str]:
        return [urlparse(str(r.url)).path for r in self.requests]


def settings(**kwargs) -> Settings:
    base = {
        "_env_file": None,
        "YOUTUBE_API_KEY": "yt-key",
        "X_BEARER_TOKEN": "x-bearer",
        "IG_USER_ID": "1784",
        "IG_ACCESS_TOKEN": "ig-token",
        "TIKTOK_ACCESS_TOKEN": "tt-token",
    }
    base.update(kwargs)
    return Settings(**base)


# --------------------------------------------------------------- YouTube


class TestYouTubeSearch:
    ROUTES = {
        "/search": {"items": [{"id": {"videoId": "abc"}}]},
        "/videos": {"items": [{
            "id": "abc",
            "snippet": {"title": "t", "description": "d", "channelTitle": "c",
                        "publishedAt": "2026-09-01T00:00:00Z"},
            "statistics": {"viewCount": "1000", "likeCount": "50", "commentCount": "5"},
            "contentDetails": {"duration": "PT1M30S"},
        }]},
    }

    def _search(self, **option_kwargs):
        rec = Recorder(self.ROUTES)
        adapter = YouTubeAdapter(settings=settings(), client=rec.client())
        records = adapter.search("ダイエット", limit=10,
                                 options=SearchOptions(**option_kwargs))
        return rec, records

    def test_search_then_hydrate_is_two_calls(self):
        # search.list carries no statistics or duration, so a second call is
        # mandatory; one call would silently yield zero-view records.
        rec, records = self._search()
        assert rec.paths() == ["/youtube/v3/search", "/youtube/v3/videos"]
        assert records[0].views == 1000
        assert records[0].duration_sec == 90.0

    def test_the_search_request_matches_the_documented_parameters(self):
        rec, _ = self._search()
        params = rec.params(0)
        assert params["part"] == "id"
        assert params["type"] == "video"
        assert params["q"] == "ダイエット"
        assert params["key"] == "yt-key"
        assert int(params["maxResults"]) <= 50   # the API's hard page cap

    def test_a_date_window_is_sent_as_rfc3339_utc(self):
        rec, _ = self._search(published_within_days=30)
        published_after = rec.params(0)["publishedAfter"]
        # Google rejects anything but this exact shape.
        assert published_after.endswith("Z") and "T" in published_after
        assert len(published_after) == 20

    def test_duration_and_order_use_the_api_s_own_vocabulary(self):
        rec, _ = self._search(video_duration="short", order="views")
        params = rec.params(0)
        assert params["videoDuration"] == "short"
        # "views" is our word; the API's is viewCount.
        assert params["order"] == "viewCount"

    def test_hydration_requests_every_part_the_record_needs(self):
        rec, _ = self._search()
        parts = rec.params(1)["part"].split(",")
        assert set(parts) == {"snippet", "statistics", "contentDetails"}


class TestYouTubeRetention:
    """Retention comes from a different API, with a different auth style."""

    ROUTES = {
        "/videos": {"items": [{"id": "abc", "statistics": {"viewCount": "900"}}]},
        "/v2/reports": {
            "columnHeaders": [
                {"name": "views"}, {"name": "estimatedMinutesWatched"},
                {"name": "averageViewDuration"}, {"name": "averageViewPercentage"},
            ],
            "rows": [[900, 150, 22, 45.5]],
        },
    }

    def _fetch(self):
        rec = Recorder(self.ROUTES)

        class Creds:
            access_token = "owner-token"
            external_id = "UC123"

        adapter = YouTubeAdapter(settings=settings(), client=rec.client(),
                                 credentials=Creds())
        return rec, adapter.fetch_metrics("abc")

    def test_it_calls_the_analytics_host_not_the_data_api(self):
        rec, _ = self._fetch()
        hosts = {urlparse(str(r.url)).netloc for r in rec.requests}
        assert "youtubeanalytics.googleapis.com" in hosts

    def test_the_analytics_request_is_scoped_to_the_owning_channel(self):
        rec, _ = self._fetch()
        params = rec.params(1)
        assert params["ids"] == "channel==MINE"
        assert params["filters"] == "video==abc"
        assert "averageViewPercentage" in params["metrics"]

    def test_a_date_range_is_always_sent(self):
        # The endpoint rejects a request without one.
        rec, _ = self._fetch()
        params = rec.params(1)
        assert params["startDate"] and params["endDate"]

    def test_analytics_is_bearer_authorised_and_the_data_api_is_key_authorised(self):
        rec, _ = self._fetch()
        assert "Authorization" not in rec.requests[0].headers
        assert rec.requests[1].headers["Authorization"] == "Bearer owner-token"

    def test_a_percentage_is_stored_as_a_fraction(self):
        # 45.5 from the API must never reach a template as 4550%.
        _, record = self._fetch()
        assert record.retention_rate == pytest.approx(0.455)
        assert record.avg_watch_sec == 22.0
        assert record.watch_time_sec == pytest.approx(9000.0)   # 150 minutes

    def test_a_missing_grant_loses_retention_and_keeps_the_counts(self):
        """403 here is normal: the account may predate the analytics scope."""
        rec = Recorder({"/videos": {"items": [
            {"id": "abc", "statistics": {"viewCount": "900", "likeCount": "10"}}
        ]}})

        class Creds:
            access_token = "owner-token"
            external_id = "UC123"

        adapter = YouTubeAdapter(settings=settings(), client=rec.client(),
                                 credentials=Creds())
        record = adapter.fetch_metrics("abc")
        assert record.views == 900 and record.likes == 10
        assert record.retention_rate is None      # not 0.0


# ------------------------------------------------------------- Instagram


class TestInstagram:
    def test_hashtag_search_resolves_the_tag_before_reading_media(self):
        rec = Recorder({
            "/ig_hashtag_search": {"data": [{"id": "17843"}]},
            "/top_media": {"data": [{
                "id": "m1", "caption": "c", "permalink": "https://ig/p/1",
                "like_count": 10, "comments_count": 2,
                "timestamp": "2026-09-01T00:00:00+0000",
            }]},
        })
        adapter = InstagramAdapter(settings=settings(), client=rec.client())
        adapter.search("#ダイエット", limit=10)

        assert "/ig_hashtag_search" in rec.paths()[0]
        # Both calls need user_id: the endpoint attributes the query to an
        # account and refuses without it.
        assert rec.params(0)["user_id"] == "1784"
        assert rec.params(1)["user_id"] == "1784"
        # The tag must be sent without the leading hash.
        assert rec.params(0)["q"] == "ダイエット"

    def test_media_url_is_requested_so_frames_can_be_read(self):
        rec = Recorder({
            "/ig_hashtag_search": {"data": [{"id": "17843"}]},
            "/top_media": {"data": []},
        })
        InstagramAdapter(settings=settings(), client=rec.client()).search("x", limit=5)
        assert "media_url" in rec.params(1)["fields"]

    def test_insights_asks_for_the_retention_metrics(self):
        rec = Recorder({
            "/insights": {"data": [
                {"name": "views", "values": [{"value": 5000}]},
                {"name": "reach", "values": [{"value": 4200}]},
                {"name": "saved", "values": [{"value": 310}]},
                {"name": "shares", "values": [{"value": 44}]},
                {"name": "ig_reels_avg_watch_time", "values": [{"value": 8200}]},
                {"name": "reels_skip_rate", "values": [{"value": 34.0}]},
            ]},
            "/17841": {"like_count": 800, "comments_count": 60},
        })
        adapter = InstagramAdapter(settings=settings(), client=rec.client())
        record = adapter.fetch_metrics("17841000")

        metrics = rec.params(1)["metric"].split(",")
        assert "ig_reels_avg_watch_time" in metrics
        assert "saved" in metrics
        # Milliseconds in, seconds out.
        assert record.avg_watch_sec == pytest.approx(8.2)
        # Percent in, fraction out.
        assert record.skip_rate == pytest.approx(0.34)
        assert record.saves == 310

    def test_business_discovery_nests_the_media_query_in_one_field_string(self):
        rec = Recorder({"/1784": {"business_discovery": {
            "followers_count": 52000, "media_count": 310, "username": "rival",
            "media": {"data": []},
        }}})
        adapter = InstagramAdapter(settings=settings(), client=rec.client())
        profile, _ = adapter.fetch_account("@rival", limit=10)

        fields = rec.params(0)["fields"]
        assert fields.startswith("business_discovery.username(rival)")
        assert "followers_count" in fields and "media" in fields
        assert profile.followers == 52000


# ------------------------------------------------------------------- X


class TestX:
    def test_search_excludes_retweets_and_asks_for_metrics(self):
        rec = Recorder({"/tweets/search/recent": {"data": [], "meta": {}}})
        XAdapter(settings=settings(), client=rec.client()).search("副業", limit=10)

        params = rec.params(0)
        assert "-is:retweet" in params["query"]
        assert "public_metrics" in params["tweet.fields"]
        # Without the expansion the author is an opaque id and every URL we
        # build points at /i/status instead of the account.
        assert params["expansions"] == "author_id"

    def test_a_window_longer_than_the_tier_allows_is_clamped_not_sent(self):
        """recent search covers 7 days; a 90-day request is rejected outright."""
        rec = Recorder({"/tweets/search/recent": {"data": [], "meta": {}}})
        XAdapter(settings=settings(), client=rec.client()).search(
            "x", limit=10, options=SearchOptions(published_within_days=90)
        )
        start = rec.params(0)["start_time"]
        from datetime import datetime, timezone

        parsed = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        assert (datetime.now(timezone.utc) - parsed).days < 7

    def test_replies_are_fetched_by_conversation_id(self):
        rec = Recorder({"/tweets/search/recent": {"data": [], "includes": {}}})
        XAdapter(settings=settings(), client=rec.client()).fetch_comments("1234")
        assert "conversation_id:1234" in rec.params(0)["query"]
        assert "is:reply" in rec.params(0)["query"]

    def test_bookmarks_are_read_as_saves(self):
        rec = Recorder({"/tweets": {"data": [{"public_metrics": {
            "impression_count": 9000, "like_count": 120, "reply_count": 8,
            "retweet_count": 20, "bookmark_count": 55,
        }}]}})
        record = XAdapter(settings=settings(), client=rec.client()).fetch_metrics("1")
        assert record.saves == 55 and record.views == 9000

    def test_the_bearer_token_is_sent(self):
        rec = Recorder({"/tweets/search/recent": {"data": [], "meta": {}}})
        XAdapter(settings=settings(), client=rec.client()).search("x", limit=10)
        assert rec.requests[0].headers["Authorization"] == "Bearer x-bearer"


# -------------------------------------------------------------- TikTok


class TestTikTok:
    def test_metrics_are_posted_with_fields_in_the_query_string(self):
        """TikTok splits it: filters in the body, fields in the query."""
        rec = Recorder({"/video/query/": {"data": {"videos": [{
            "id": "v1", "view_count": 1000, "like_count": 90,
            "comment_count": 5, "share_count": 3,
        }]}}})
        record = TikTokAdapter(settings=settings(), client=rec.client()).fetch_metrics("v1")

        request = rec.requests[0]
        assert request.method == "POST"
        assert "fields" in rec.params(0)
        assert b"video_ids" in request.content
        assert record.views == 1000

    def test_search_refuses_and_names_the_alternative(self):
        from snsauto.platforms.base import CapabilityUnavailable

        adapter = TikTokAdapter(settings=settings(), client=Recorder({}).client())
        with pytest.raises(CapabilityUnavailable) as exc:
            adapter.search("x")
        assert "ingest_manual" in str(exc.value)

    def test_retention_is_not_claimed(self):
        """The Display API carries no watch-time field, so none is invented."""
        rec = Recorder({"/video/query/": {"data": {"videos": [
            {"id": "v1", "view_count": 1000}
        ]}}})
        record = TikTokAdapter(settings=settings(), client=rec.client()).fetch_metrics("v1")
        assert record.retention_rate is None
        assert record.avg_watch_sec is None


class TestEveryAdapterAgreesOnPlatform:
    @pytest.mark.parametrize("adapter_cls,platform", [
        (YouTubeAdapter, Platform.YOUTUBE), (XAdapter, Platform.X),
        (InstagramAdapter, Platform.INSTAGRAM), (TikTokAdapter, Platform.TIKTOK),
    ])
    def test_records_carry_the_adapter_s_own_platform(self, adapter_cls, platform):
        assert adapter_cls(settings=settings()).platform == platform
