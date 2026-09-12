"""Audio-bed classification and competitor video acquisition."""

from pathlib import Path

import pytest

from snsauto.models import CompetitorPost, Platform
from snsauto.research.audio import AUDIO_STYLE_JA, classify_audio
from snsauto.research.fetch import (
    FetchError,
    VideoFetcher,
    provenance_note,
)


class Settings:
    def __init__(self, tmp_path, cmd=None):
        self.workspace = tmp_path
        self.video_fetch_cmd = cmd
        self.video_fetch_timeout = 5.0


def post(raw=None, url=None, external_id="abc"):
    return CompetitorPost(
        id=1, run_id=1, external_id=external_id, platform=Platform.INSTAGRAM,
        rank=1, url=url, raw=raw or {},
    )


class TestAudioClassification:
    @pytest.mark.parametrize("band,quiet,expected", [
        (0.90, 0.10, "narration-led"),
        (0.20, 0.05, "music-led"),
        (0.60, 0.05, "mixed"),
        (0.90, 0.95, "silent"),
        (None, 0.10, "unknown"),
    ])
    def test_labels(self, band, quiet, expected):
        assert classify_audio(band, quiet) == expected

    def test_a_near_silent_track_is_silent_whatever_the_band_says(self):
        # Band ratio on near-silence is measuring noise floor, not content.
        assert classify_audio(0.99, 0.99) == "silent"

    def test_every_label_has_japanese_copy(self):
        for band, quiet in [(0.9, 0.1), (0.2, 0.1), (0.6, 0.1), (0.9, 0.95), (None, 0.1)]:
            assert classify_audio(band, quiet) in AUDIO_STYLE_JA


class TestProvenance:
    def test_external_downloads_are_labelled_as_the_operator_s_decision(self):
        note = provenance_note("external")
        assert "利用規約" in note and "運用者" in note

    def test_api_sourced_media_is_labelled_as_sanctioned(self):
        assert "公式API" in provenance_note("api")

    def test_an_unknown_source_is_never_silently_blessed(self):
        assert "不明" in provenance_note("mystery")


class TestApiMediaUrl:
    def test_instagram_media_url_is_used_when_present(self, tmp_path):
        fetcher = VideoFetcher(Settings(tmp_path))
        assert fetcher.api_media_url(
            post({"media_url": "https://cdn.example/v.mp4"})
        ) == "https://cdn.example/v.mp4"

    def test_a_non_http_value_is_ignored(self, tmp_path):
        fetcher = VideoFetcher(Settings(tmp_path))
        assert fetcher.api_media_url(post({"media_url": "n/a"})) is None

    def test_a_post_with_no_media_field_yields_nothing(self, tmp_path):
        assert VideoFetcher(Settings(tmp_path)).api_media_url(post()) is None


class TestFetch:
    def test_a_cached_file_is_returned_without_a_download(self, tmp_path):
        fetcher = VideoFetcher(Settings(tmp_path))
        target = fetcher.cached_path(post())
        target.write_bytes(b"video-bytes")

        result = fetcher.fetch(post())
        assert result.source == "cache"
        assert result.bytes == len(b"video-bytes")

    def test_no_media_url_and_no_downloader_gives_up_quietly(self, tmp_path):
        # A missing video must degrade the analysis, never fail the run.
        assert VideoFetcher(Settings(tmp_path)).fetch(post()) is None

    def test_the_cache_key_separates_platforms(self, tmp_path):
        fetcher = VideoFetcher(Settings(tmp_path))
        a = post(external_id="same")
        b = post(external_id="same")
        b.platform = Platform.YOUTUBE
        assert fetcher.cached_path(a) != fetcher.cached_path(b)

    def test_a_malformed_downloader_template_says_what_to_write(self, tmp_path):
        fetcher = VideoFetcher(Settings(tmp_path, cmd="yt-dlp -o {outfile} {url}"))
        with pytest.raises(FetchError) as exc:
            fetcher._external(post(url="https://example/v"), tmp_path / "out.mp4")
        assert "{url}" in str(exc.value) and "{out}" in str(exc.value)

    def test_the_downloader_needs_a_url_to_work_with(self, tmp_path):
        fetcher = VideoFetcher(Settings(tmp_path, cmd="yt-dlp -o {out} {url}"))
        with pytest.raises(FetchError, match="no URL"):
            fetcher._external(post(url=None), tmp_path / "out.mp4")

    def test_the_command_comes_from_configuration_not_from_the_post(self, tmp_path):
        # A caption containing shell metacharacters must not become argv.
        fetcher = VideoFetcher(Settings(tmp_path, cmd="/bin/echo {url}"))
        target = tmp_path / "out.mp4"
        evil = post(url="https://example/v; rm -rf /")
        with pytest.raises(FetchError):
            # echo succeeds but writes no file, so this still fails - the point
            # is that it fails without a shell ever seeing the URL.
            fetcher._external(evil, target)
        assert Path("/").exists()
