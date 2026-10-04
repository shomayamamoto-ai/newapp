import pytest

from snsauto.config import Settings
from snsauto.models import Platform
from snsauto.platforms import (
    Capability,
    CapabilityUnavailable,
    CredentialsMissing,
    PublishRequest,
    capability_matrix,
    get_adapter,
)
from snsauto.platforms.tiktok import TikTokAdapter
from snsauto.utils.oauth1 import sign


def _settings(**kw):
    return Settings(_env_file=None, **kw)


class TestCapabilityGating:
    def test_no_credentials_means_no_capabilities(self):
        for platform in Platform:
            assert get_adapter(platform, settings=_settings()).capabilities() == set()

    def test_youtube_api_key_grants_search_but_not_publish(self):
        caps = get_adapter(Platform.YOUTUBE, settings=_settings(YOUTUBE_API_KEY="k")).capabilities()
        assert Capability.SEARCH in caps
        assert Capability.PUBLISH not in caps

    def test_youtube_oauth_grants_publish(self):
        caps = get_adapter(
            Platform.YOUTUBE,
            settings=_settings(
                YOUTUBE_CLIENT_ID="a", YOUTUBE_CLIENT_SECRET="b", YOUTUBE_REFRESH_TOKEN="c"
            ),
        ).capabilities()
        assert Capability.PUBLISH in caps

    def test_x_bearer_grants_search_only(self):
        caps = get_adapter(Platform.X, settings=_settings(X_BEARER_TOKEN="t")).capabilities()
        assert caps == {Capability.SEARCH, Capability.INSIGHTS}

    def test_search_without_credentials_raises(self):
        with pytest.raises(CredentialsMissing):
            get_adapter(Platform.YOUTUBE, settings=_settings()).search("k")

    def test_capability_matrix_covers_every_platform(self):
        matrix = capability_matrix(_settings())
        assert set(matrix) == {p.value for p in Platform}


class TestTikTok:
    def test_search_always_refuses(self):
        """There is no public TikTok search API; the adapter must not pretend."""
        adapter = TikTokAdapter(settings=_settings(TIKTOK_ACCESS_TOKEN="t"))
        with pytest.raises(CapabilityUnavailable) as exc:
            adapter.search("keyword")
        assert "Research API" in str(exc.value)

    def test_search_never_advertised_even_with_token(self):
        adapter = TikTokAdapter(settings=_settings(TIKTOK_ACCESS_TOKEN="t"))
        assert Capability.SEARCH not in adapter.capabilities()

    def test_ingest_manual_reads_csv(self, tmp_path):
        csv = tmp_path / "posts.csv"
        csv.write_text(
            "external_id,title,views,likes,duration_sec\n"
            "a1,タイトル,\"12,000\",900,23\n",
            encoding="utf-8",
        )
        records = TikTokAdapter.ingest_manual(csv)
        assert len(records) == 1
        assert records[0].title == "タイトル"
        assert records[0].views == 12000  # thousands separator tolerated
        assert records[0].duration_sec == 23.0

    def test_ingest_manual_tolerates_missing_columns(self, tmp_path):
        csv = tmp_path / "posts.csv"
        csv.write_text("external_id\nonly-id\n", encoding="utf-8")
        records = TikTokAdapter.ingest_manual(csv)
        assert records[0].views == 0 and records[0].title is None


class TestPublishRequest:
    def test_appends_hashtags(self):
        req = PublishRequest(video_path="v.mp4", caption="本文", hashtags=["副業", "#節約"])
        assert req.full_caption() == "本文\n\n#副業 #節約"

    def test_truncates_to_limit(self):
        req = PublishRequest(video_path="v.mp4", caption="x" * 400)
        assert len(req.full_caption(limit=280)) == 280

    def test_no_hashtags_leaves_caption_clean(self):
        assert PublishRequest(video_path="v.mp4", caption="本文").full_caption() == "本文"


def test_oauth1_matches_published_reference_vector():
    """Twitter's own documented example - proves the signature base string."""
    header = sign(
        "POST", "https://api.twitter.com/1.1/statuses/update.json",
        consumer_key="xvz1evFS4wEEPTGEFPHBog",
        consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        params={
            "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
            "include_entities": "true",
        },
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        timestamp="1318622958",
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header
