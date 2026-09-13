"""Posts that succeed and are invisible.

Two platforms do this, for the same reason, and neither reports it as a
failure: YouTube locks every API upload to private until the Cloud project
passes its compliance audit, and TikTok forces SELF_ONLY until the app passes
its own. Both return success with an id. Without an explicit comparison of
what we asked for against what came back, an operator can publish for weeks
into a void and see nothing but green.
"""

import httpx
import pytest

from snsauto.config import Settings
from snsauto.platforms.base import PublishRequest, PublishResult
from snsauto.platforms.tiktok import TikTokAdapter
from snsauto.platforms.youtube import YouTubeAdapter


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"\x00" * 2048)
    return str(path)


class TestTheComparisonItself:
    def test_matching_visibility_is_not_a_downgrade(self):
        result = PublishResult("a", visibility="public", requested_visibility="public")
        assert not result.visibility_downgraded

    def test_a_different_visibility_is(self):
        result = PublishResult("a", visibility="private", requested_visibility="public")
        assert result.visibility_downgraded

    def test_an_unreported_visibility_is_not_an_accusation(self):
        """Platforms that say nothing must not be reported as downgrading."""
        assert not PublishResult("a", requested_visibility="public").visibility_downgraded
        assert not PublishResult("a", visibility="public").visibility_downgraded


def youtube(handler):
    return YouTubeAdapter(
        settings=Settings(_env_file=None, YOUTUBE_API_KEY="k",
                          YOUTUBE_REFRESH_TOKEN="r", YOUTUBE_CLIENT_ID="c",
                          YOUTUBE_CLIENT_SECRET="s"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def youtube_upload(privacy_returned):
    """Answers the token, init and upload calls of a real upload."""

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth2" in url or "token" in url:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if "upload" in url and request.method == "POST":
            return httpx.Response(
                200, json={}, headers={"Location": "https://upload.example/session"},
            )
        return httpx.Response(200, json={
            "id": "vid123", "status": {"privacyStatus": privacy_returned},
        })

    return handle


class TestYouTube:
    def test_an_upload_locked_to_private_is_reported(self, video):
        adapter = youtube(youtube_upload("private"))
        result = adapter.publish(PublishRequest(video_path=video, privacy="public"))

        assert result.external_id == "vid123"
        assert result.visibility_downgraded
        assert result.warnings
        # Naming the audit is the whole point: "private" alone looks like a
        # setting somebody chose.
        assert "監査" in result.warnings[0]

    def test_an_upload_that_went_public_says_nothing(self, video):
        adapter = youtube(youtube_upload("public"))
        result = adapter.publish(PublishRequest(video_path=video, privacy="public"))
        assert result.warnings == []
        assert result.visibility == "public"

    def test_a_scheduled_post_is_private_on_purpose(self, video):
        """Scheduling *requires* private plus publishAt. Reporting that as a
        downgrade would cry wolf on every scheduled post."""
        from datetime import datetime, timedelta, timezone

        adapter = youtube(youtube_upload("private"))
        result = adapter.publish(PublishRequest(
            video_path=video, privacy="public",
            scheduled_for=datetime.now(timezone.utc) + timedelta(days=1),
        ))
        assert result.warnings == []
        assert result.status == "scheduled"


def tiktok(handler):
    return TikTokAdapter(
        settings=Settings(_env_file=None, TIKTOK_ACCESS_TOKEN="t"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def tiktok_publish(privacy_options):
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "creator_info" in url:
            if privacy_options is None:
                return httpx.Response(403, json={"error": {"code": "denied"}})
            return httpx.Response(
                200, json={"data": {"privacy_level_options": privacy_options}}
            )
        if "video/init" in url:
            return httpx.Response(200, json={"data": {
                "upload_url": "https://upload.tiktok/x", "publish_id": "pub1"}})
        return httpx.Response(200, json={})

    return handle, calls


class TestTikTok:
    def test_an_unaudited_app_is_caught_before_the_upload(self, video):
        """The check has to precede the bytes: TikTok accepts the upload and
        files it as SELF_ONLY either way."""
        handle, calls = tiktok_publish(["SELF_ONLY"])
        result = tiktok(handle).publish(PublishRequest(video_path=video))

        assert result.warnings
        assert "自分のみ表示" in result.warnings[0]
        assert calls[0].find("creator_info") > 0
        assert any("video/init" in c for c in calls)
        # The order is the point.
        assert calls.index(next(c for c in calls if "creator_info" in c)) < \
               calls.index(next(c for c in calls if "video/init" in c))

    def test_a_public_capable_account_says_nothing(self, video):
        handle, _ = tiktok_publish(["PUBLIC_TO_EVERYONE", "SELF_ONLY"])
        result = tiktok(handle).publish(PublishRequest(video_path=video))
        assert result.warnings == []
        assert result.visibility == "PUBLIC_TO_EVERYONE"

    def test_not_being_able_to_ask_does_not_block_publishing(self, video):
        """A refused creator_info is not evidence of a restriction, and
        guessing one would stop posts that would have worked."""
        handle, _ = tiktok_publish(None)
        result = tiktok(handle).publish(PublishRequest(video_path=video))
        assert result.warnings == []
        assert result.external_id == "pub1"
