"""Visual sourcing across the three modes."""

import pytest

from snsauto.config import Settings
from snsauto.creative.footage import FootageLibrary, tokenize
from snsauto.creative.imagegen import PlaceholderProvider, ImageGenerator
from snsauto.creative.script import ScriptService
from snsauto.creative.storyboard import StoryboardService
from snsauto.creative.videogen import (
    JOB_ID_KEYS, VIDEO_URL_KEYS, VideoGenError, _find,
    build_video_provider,
)
from snsauto.creative.visuals import VisualSourcer
from snsauto.models import ClipAsset, Platform, VisualMode


@pytest.fixture
def board(session, project):
    script = ScriptService(session).generate(project, "副業", Platform.TIKTOK, 12.0)
    return StoryboardService(session).generate(script)


@pytest.fixture
def sourcer(session):
    return VisualSourcer(
        session,
        settings=Settings(_env_file=None),
        images=ImageGenerator(provider=PlaceholderProvider(), settings=Settings(_env_file=None)),
    )


def _clip(session, name, keywords, duration=8.0, w=1080, h=1920):
    clip = ClipAsset(path=f"/tmp/{name}.mp4", label=name, keywords=keywords,
                     duration_sec=duration, width=w, height=h)
    session.add(clip)
    session.flush()
    return clip


class TestTokenize:
    def test_drops_boilerplate_style_words(self):
        tokens = tokenize("vertical shot of a desk. clean modern, soft natural light, no text")
        assert "desk" in tokens
        assert "vertical" not in tokens and "text" not in tokens

    def test_keeps_japanese(self):
        assert tokenize("副業 デスクワーク") == ["副業", "デスクワーク"]


class TestModeResolution:
    def test_still_is_always_available(self, sourcer):
        assert VisualMode.STILL in sourcer.available_modes()

    def test_animate_unavailable_without_a_provider(self, sourcer):
        assert VisualMode.ANIMATE not in sourcer.available_modes()
        assert sourcer.resolve_mode("animate") is VisualMode.STILL

    def test_footage_available_once_clips_exist(self, session, sourcer):
        assert VisualMode.FOOTAGE not in sourcer.available_modes()
        _clip(session, "a", ["desk"])
        assert VisualMode.FOOTAGE in sourcer.available_modes()

    def test_auto_picks_the_best_configured_mode(self, session, sourcer):
        assert sourcer.resolve_mode("auto") is VisualMode.STILL
        _clip(session, "a", ["desk"])
        assert sourcer.resolve_mode("auto") is VisualMode.FOOTAGE

    def test_unknown_mode_falls_back(self, sourcer):
        assert sourcer.resolve_mode("hologram") is VisualMode.STILL


class TestFootageMatching:
    def test_keyword_overlap_wins(self, session):
        library = FootageLibrary(session, Settings(_env_file=None))
        _clip(session, "desk", ["desk", "laptop"])
        coins = _clip(session, "coins", ["money", "coins"])
        match = library.match("saving money with coins", 4.0)
        assert match.clip.id == coins.id

    def test_prefers_vertical(self, session):
        library = FootageLibrary(session, Settings(_env_file=None))
        _clip(session, "wide", ["desk"], w=1920, h=1080)
        tall = _clip(session, "tall", ["desk"], w=1080, h=1920)
        assert library.match("desk", 4.0, prefer_vertical=True).clip.id == tall.id

    def test_penalises_clips_shorter_than_the_shot(self, session):
        library = FootageLibrary(session, Settings(_env_file=None))
        _clip(session, "short", ["desk"], duration=1.0)
        long_clip = _clip(session, "long", ["desk"], duration=9.0)
        assert library.match("desk", 8.0).clip.id == long_clip.id

    def test_excluded_clips_are_skipped(self, session):
        library = FootageLibrary(session, Settings(_env_file=None))
        first = _clip(session, "a", ["desk"])
        second = _clip(session, "b", ["desk"])
        assert library.match("desk", 3.0, exclude_ids={first.id}).clip.id == second.id

    def test_reuse_avoids_the_previous_cut(self, session):
        """A repeat across a cut reads as a glitch; a later repeat does not."""
        library = FootageLibrary(session, Settings(_env_file=None))
        first = _clip(session, "a", ["desk"])
        second = _clip(session, "b", ["desk"])
        match = library.match(
            "desk", 3.0, exclude_ids={first.id, second.id}, avoid_id=second.id
        )
        assert match.clip.id == first.id

    def test_no_clips_returns_none(self, session):
        assert FootageLibrary(session, Settings(_env_file=None)).match("x", 3.0) is None


class TestFootageMode:
    def test_assigns_clips_and_never_repeats_consecutively(self, session, sourcer, board, tmp_path):
        for name in ("desk", "coins", "city"):
            _clip(session, name, [name])
        result = sourcer.produce(board, tmp_path, 540, 960, mode="footage")

        assert result.counts.get("footage") == len(board.shots)
        paths = [s.clip_path for s in board.shots]
        assert all(paths)
        assert not any(a == b for a, b in zip(paths, paths[1:]))

    def test_degrades_to_still_when_nothing_matches(self, session, sourcer, board, tmp_path):
        result = sourcer.produce(board, tmp_path, 540, 960, mode="footage")
        # No clips indexed, so the mode is not even available.
        assert result.counts.get("still") == len(board.shots)
        assert all(s.image_path for s in board.shots)


class TestAnimateMode:
    def test_a_failed_clip_degrades_that_shot_only(self, session, board, tmp_path):
        calls = {"n": 0}

        class FlakyVideo:
            def generate(self, request, out_path):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise VideoGenError("provider rejected the prompt")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"fake-mp4")
                return out_path

        settings = Settings(_env_file=None)
        sourcer = VisualSourcer(
            session, settings,
            images=ImageGenerator(provider=PlaceholderProvider(), settings=settings),
            video=FlakyVideo(),
        )
        result = sourcer.produce(board, tmp_path, 540, 960, mode="animate")

        assert len(result.degraded) == 1
        assert result.degraded[0].degraded_from is VisualMode.ANIMATE
        # Every other shot still animated - one failure is not a lost render.
        assert result.counts.get("animate") == len(board.shots) - 1
        assert "visuals" not in result.summary() or result.summary()["degraded"]


class TestVideoGenResponseShapes:
    @pytest.mark.parametrize("payload,expected", [
        ({"video_url": "https://x/v.mp4"}, "https://x/v.mp4"),
        ({"data": {"output": {"url": "https://y/v.mp4"}}}, "https://y/v.mp4"),
        ({"results": [{"output_url": "https://z/v.mp4"}]}, "https://z/v.mp4"),
    ])
    def test_finds_the_video_url_at_any_depth(self, payload, expected):
        assert _find(payload, VIDEO_URL_KEYS) == expected

    def test_finds_job_id_under_vendor_specific_names(self):
        assert _find({"result": {"task_id": "abc"}}, JOB_ID_KEYS) == "abc"

    def test_missing_key_returns_none(self):
        assert _find({"nothing": 1}, VIDEO_URL_KEYS) is None

    def test_provider_is_none_when_unconfigured(self):
        assert build_video_provider(Settings(_env_file=None)) is None

    def test_provider_is_none_without_an_endpoint(self):
        settings = Settings(_env_file=None, VIDEOGEN_PROVIDER="runway")
        assert build_video_provider(settings) is None
